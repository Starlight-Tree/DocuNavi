package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

var signNow = time.Date(2026, 11, 25, 12, 34, 56, 0, time.UTC)

// SigV4 の署名は、botocore（app/r2_store.py が使う SDK）が同じ要求に付けた値と一致する。
// 期待値は、開発コンテナで botocore.auth.S3SigV4Auth に固定の時刻と鍵（AWS の文書の例の鍵）を渡して作った（2026-11-25）。
func TestSignV4MatchesBotocore(t *testing.T) {
	creds := r2Credentials{KeyID: "AKIDEXAMPLE", Secret: "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"}
	const host = "acct.r2.cloudflarestorage.com"
	cases := []struct {
		name, uri, query, signature string
	}{
		{"object", "/docunavi-documents/cafe/menu.pdf", "", "e71fede864e24070c82413c38c81984f73c6308cf6540e0ac47efe2bffee2319"},
		{"object with Japanese, space, + and ()", "/docunavi-documents/zahard/%E8%A8%AD%E5%AE%9A%20%E8%B3%87%E6%96%99%2B%281%29.pdf", "", "b0a23a7671843eacc67c81eca70acdf147fb8c1a68abf6db06e63215b2a6cad1"},
		{"list", "/docunavi-documents", "delimiter=%2F&list-type=2", "f21e4e8c67a0c486e7e537545813a5bf8be195e0db431404acde70c5c0e8cf52"},
		{"list with token", "/docunavi-documents", "continuation-token=1ueGcxLPRx1Tr%2FXYExHnhbYLgveDs2J%2Fwm36Hy4vbOwM%3D&delimiter=%2F&list-type=2", "0d3a623153f49463f4c032ff2022d0e739d8edd473b10695fa1b0e991cb8b3c3"},
	}
	for _, tc := range cases {
		date, auth := signV4(creds, host, tc.uri, tc.query, signNow)
		want := "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261125/auto/s3/aws4_request, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature=" + tc.signature
		if date != "20261125T123456Z" || auth != want {
			t.Errorf("%s:\n got  %s\n want %s", tc.name, auth, want)
		}
	}
	// 同じ組み立て（URI・問い合わせのエンコード）を、実際の要求の経路でも使う
	if got := uriEncode("/docunavi-documents/zahard/設定 資料+(1).pdf", false); got != cases[1].uri {
		t.Errorf("uriEncode: %s", got)
	}
	if got := canonicalQueryOf(map[string]string{"list-type": "2", "delimiter": "/"}); got != cases[2].query {
		t.Errorf("canonicalQueryOf: %s", got)
	}
}

// fakeR2 は、パススタイルの S3 互換サーバー（署名は、同じ関数で作り直した値と一致するかを確かめる）。
type fakeR2 struct {
	srv       *httptest.Server
	mu        sync.Mutex
	objects   map[string][]byte // "<資料群>/<名前>" → 中身
	prefixes  []string
	acceptKey string // 受け付ける鍵の ID（空なら何でも）
	secret    string
	requests  atomic.Int64
	fail      atomic.Int64 // 先頭の n 回は 503 を返す
	pageSize  int
}

func newFakeR2(t *testing.T, objects map[string][]byte) *fakeR2 {
	t.Helper()
	f := &fakeR2{objects: objects, secret: "secret1", pageSize: 100}
	f.srv = httptest.NewServer(http.HandlerFunc(f.handle))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeR2) handle(w http.ResponseWriter, r *http.Request) {
	f.requests.Add(1)
	if f.fail.Add(-1) >= 0 {
		http.Error(w, "boom", http.StatusServiceUnavailable)
		return
	}
	auth := r.Header.Get("Authorization")
	f.mu.Lock()
	acceptKey := f.acceptKey
	f.mu.Unlock()
	if acceptKey != "" && !strings.Contains(auth, "Credential="+acceptKey+"/") {
		http.Error(w, "denied", http.StatusForbidden)
		return
	}
	// 署名を、受け取った要求から作り直して照合する
	creds := r2Credentials{KeyID: acceptKey, Secret: f.secret}
	if acceptKey == "" {
		creds.KeyID = strings.TrimPrefix(strings.SplitN(auth, "/", 2)[0], "AWS4-HMAC-SHA256 Credential=")
	}
	at, err := time.Parse("20060102T150405Z", r.Header.Get("X-Amz-Date"))
	if err != nil {
		http.Error(w, "bad date", http.StatusBadRequest)
		return
	}
	date, want := signV4(creds, r.Host, r.URL.EscapedPath(), canonicalQueryOf(flatten(r.URL.Query())), at)
	if date != r.Header.Get("X-Amz-Date") || want != auth || r.Header.Get("X-Amz-Content-Sha256") != emptyPayloadHash {
		http.Error(w, "signature mismatch", http.StatusForbidden)
		return
	}
	path := strings.TrimPrefix(r.URL.Path, "/bucket")
	if path == "" || path == "/" {
		f.list(w, r)
		return
	}
	data, ok := f.objects[strings.TrimPrefix(path, "/")]
	if !ok {
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, `<Error><Code>NoSuchKey</Code></Error>`)
		return
	}
	w.Write(data)
}

func flatten(v url.Values) map[string]string {
	out := map[string]string{}
	for k, vs := range v {
		out[k] = vs[0]
	}
	return out
}

func (f *fakeR2) list(w http.ResponseWriter, r *http.Request) {
	seen := map[string]bool{}
	var names []string
	for key := range f.objects {
		if head, _, ok := strings.Cut(key, "/"); ok && !seen[head] {
			seen[head] = true
			names = append(names, head)
		}
	}
	for _, p := range f.prefixes {
		if !seen[p] {
			seen[p] = true
			names = append(names, p)
		}
	}
	sortStrings(names)
	start := 0
	if token := r.URL.Query().Get("continuation-token"); token != "" {
		fmt.Sscanf(token, "t%d", &start)
	}
	end := start + f.pageSize
	truncated := end < len(names)
	if end > len(names) {
		end = len(names)
	}
	var b strings.Builder
	b.WriteString(`<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">`)
	fmt.Fprintf(&b, "<IsTruncated>%v</IsTruncated>", truncated)
	if truncated {
		fmt.Fprintf(&b, "<NextContinuationToken>t%d</NextContinuationToken>", end)
	}
	for _, n := range names[start:end] {
		fmt.Fprintf(&b, "<CommonPrefixes><Prefix>%s/</Prefix></CommonPrefixes>", n)
	}
	b.WriteString("</ListBucketResult>")
	w.Header().Set("Content-Type", "application/xml")
	fmt.Fprint(w, b.String())
}

func sortStrings(s []string) {
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && s[j] < s[j-1]; j-- {
			s[j], s[j-1] = s[j-1], s[j]
		}
	}
}

func (f *fakeR2) client(env map[string]string) *r2Client {
	c := newR2Client(r2Settings{get: func(k string) string { return env[k] }})
	c.endpoint = f.srv.URL
	c.sleep = func(time.Duration) {}
	return c
}

func r2Env(keyID, secret string) map[string]string {
	creds, _ := json.Marshal(map[string]string{"access_key_id": keyID, "secret_access_key": secret})
	return map[string]string{"R2_ACCOUNT_ID": "acct", "R2_BUCKET": "bucket", "R2_CREDENTIALS_JSON": string(creds)}
}

func TestR2CollectionsAreListedWithPagingAndWithoutHero(t *testing.T) {
	f := newFakeR2(t, map[string][]byte{"cafe/a.pdf": nil, "guides/b.pdf": nil, "zahard/c.pdf": nil, "hero/1.png": nil})
	f.pageSize = 2
	got, err := f.client(r2Env("key1", "secret1")).collections(t.Context())
	if err != nil || strings.Join(got, ",") != "cafe,guides,zahard" {
		t.Fatalf("%v %v", got, err)
	}
	if f.requests.Load() != 2 { // 4 つの接頭辞（hero を含む）を 2 件ずつ
		t.Fatalf("requests: %d", f.requests.Load())
	}
}

func TestR2GetObject(t *testing.T) {
	f := newFakeR2(t, map[string][]byte{"cafe/設定 資料+(1).pdf": []byte("PDF-1"), "cafe/big.pdf": make([]byte, 100)})
	c := f.client(r2Env("key1", "secret1"))
	if data, found, err := c.get(t.Context(), "cafe/設定 資料+(1).pdf", 1000); err != nil || !found || string(data) != "PDF-1" {
		t.Fatalf("%q %v %v", data, found, err)
	}
	if _, found, err := c.get(t.Context(), "cafe/none.pdf", 1000); err != nil || found {
		t.Fatalf("missing: %v %v", found, err)
	}
	if _, found, err := c.get(t.Context(), "cafe/big.pdf", 50); err != nil || found {
		t.Fatalf("too large must read as not found: %v %v", found, err)
	}
}

func TestR2RetriesTemporaryFailuresThreeTimesAtMost(t *testing.T) {
	f := newFakeR2(t, map[string][]byte{"cafe/a.pdf": []byte("x")})
	f.fail.Store(2)
	if data, found, err := f.client(r2Env("key1", "secret1")).get(t.Context(), "cafe/a.pdf", 10); err != nil || !found || string(data) != "x" {
		t.Fatalf("two 503s then 200 must succeed: %q %v %v", data, found, err)
	}
	f2 := newFakeR2(t, map[string][]byte{"cafe/a.pdf": []byte("x")})
	f2.fail.Store(100)
	if _, _, err := f2.client(r2Env("key1", "secret1")).get(t.Context(), "cafe/a.pdf", 10); err == nil || f2.requests.Load() != 3 {
		t.Fatalf("persistent 503: err=%v requests=%d", err, f2.requests.Load())
	}
}

func TestR2ReloadsRotatedKeysOnceAndOnlyWhenTheKeyIDChanged(t *testing.T) {
	dir := t.TempDir()
	credsFile := filepath.Join(dir, "credentials.json")
	write := func(id string) {
		data, _ := json.Marshal(map[string]string{"access_key_id": id, "secret_access_key": "secret1"})
		if err := os.WriteFile(credsFile, data, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write("old")
	f := newFakeR2(t, map[string][]byte{"cafe/a.pdf": []byte("x")})
	f.acceptKey = "old"
	c := f.client(map[string]string{"R2_ACCOUNT_ID": "acct", "R2_BUCKET": "bucket", "R2_CREDENTIALS_FILE": credsFile})
	if _, found, err := c.get(t.Context(), "cafe/a.pdf", 10); err != nil || !found {
		t.Fatalf("before rotation: %v %v", found, err)
	}
	// 鍵が入れ替わった: R2 は新しい鍵だけを受け付け、ファイルも新しい鍵になる → 1 回だけ読み直してやり直す（再配備は要らない）
	f.mu.Lock()
	f.acceptKey = "new"
	f.mu.Unlock()
	write("new")
	if data, found, err := c.get(t.Context(), "cafe/a.pdf", 10); err != nil || !found || string(data) != "x" {
		t.Fatalf("after rotation: %q %v %v", data, found, err)
	}
	// 権限が無いだけ（鍵の ID は同じ）: やり直さずに、認証のエラー
	f.mu.Lock()
	f.acceptKey = "someone-else"
	f.mu.Unlock()
	before := f.requests.Load()
	if _, _, err := c.get(t.Context(), "cafe/a.pdf", 10); err != errR2Auth {
		t.Fatalf("want errR2Auth, got %v", err)
	}
	if n := f.requests.Load() - before; n != 1 {
		t.Fatalf("the same key ID must not be retried: %d requests", n)
	}
}

func TestR2SettingsErrorsNameOnlyTheVariables(t *testing.T) {
	_, _, _, err := r2Settings{get: func(string) string { return "" }}.load()
	if err == nil || !strings.Contains(err.Error(), "R2_ACCOUNT_ID, R2_BUCKET, R2_CREDENTIALS_JSON") {
		t.Fatalf("%v", err)
	}
	env := r2Env("k", "s")
	env["R2_CREDENTIALS_JSON"] = `{"access_key_id":"k"}`
	if _, _, _, err := (r2Settings{get: func(k string) string { return env[k] }}).load(); err == nil || strings.Contains(err.Error(), `"k"`) {
		t.Fatalf("incomplete credentials must be rejected without echoing them: %v", err)
	}
	// 鍵を 2 つの変数で渡す経路（JSON・ファイルが先）
	pair := map[string]string{"R2_ACCOUNT_ID": "a", "R2_BUCKET": "b", "R2_ACCESS_KEY_ID": "id", "R2_SECRET_ACCESS_KEY": "sec"}
	if _, _, creds, err := (r2Settings{get: func(k string) string { return pair[k] }}).load(); err != nil || creds.KeyID != "id" || creds.Secret != "sec" {
		t.Fatalf("%v %v", creds, err)
	}
}
