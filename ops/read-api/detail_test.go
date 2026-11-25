package main

import (
	"strings"
	"testing"
	"time"
)

func detailStore() *fakeStore {
	t1 := time.Date(2026, 11, 20, 3, 4, 5, 123_999_000, time.UTC)
	pages := int32(7)
	db := sampleStore()
	db.details = map[int64]*documentDetailRow{
		1: {documentRow: documentRow{ID: 1, Source: "menu.pdf", Title: str(" 題名 "), Collection: str("cafe"), Target: str("minato-coffee"),
			DocType: str("store-guide"), Revision: str("初版"), Status: "ready", Pages: &pages, UpdatedAt: &t1, HeroImage: true,
			ChunkCount: 99, EmbeddedCount: 99}, ContentHash: sum("MENU")},
		2: {documentRow: documentRow{ID: 2, Source: "g.pdf", Collection: str("guides"), Status: "ready"}, ContentHash: sum("G")},
		3: {documentRow: documentRow{ID: 3, Source: "none.pdf", Status: "processing"}},
		4: {documentRow: documentRow{ID: 4, Source: "verified.pdf", Collection: str("cafe"), Status: "ready"}, OriginalVerified: true},
	}
	db.chunkRows = map[int64][]chunkRow{1: {
		{ChunkIndex: 0, Page: i32(1), PageEnd: i32(2), Kind: str("body"), PageLabel: str("表紙"), PageEndLabel: str("i"), Heading: str("見出し"), Content: "本文 <b>&</b>", Embedded: true, EmbeddingModel: str("m")},
		{ChunkIndex: 1, Content: "二つ目"},
	}}
	db.heroRows = map[int64]*heroRow{
		1: {Collection: str("cafe"), MimeType: str("image/png"), R2Key: str("hero/1.png")},
		2: {Collection: str("guides"), MimeType: str("image/png"), R2Key: str("hero/2.png")},
		3: {Collection: str("cafe"), MimeType: str("image/gif"), R2Key: str("hero/3.gif")},
		4: {Collection: str("cafe"), MimeType: str("image/jpeg"), R2Key: str("hero/none.jpg")},
	}
	return db
}

// 資料が見えるのは、PDF が置き場に合う（ハッシュが一致する）ときだけ original_pdf が true
func serverWithPDFs(t *testing.T, cfg config, db *fakeStore, files map[string]string) *server {
	t.Helper()
	cfg.PDFDir = tree(t, files)
	return newTestServer(t, cfg, db, nil)
}

func TestChunksMatchThePythonShape(t *testing.T) {
	s := serverWithPDFs(t, baseConfig(), detailStore(), map[string]string{"cafe/menu.pdf": "MENU"})
	rec := do(s, "GET", "/documents/1/chunks", nil)
	want := `{"document":{"id":1,"source":"menu.pdf","title":"題名","collection":"cafe","target":"minato-coffee","doc_type":"store-guide","revision":"初版","status":"ready","failure_reason":null,"pages":7,"updated_at":"2026-11-20T03:04:05.123000Z","chunk_count":2,"embedded_count":1,"hero_image":false},` +
		`"chunks":[{"chunk_index":0,"page":1,"page_end":2,"page_label":"表紙","page_end_label":"i","kind":"body","heading":"見出し","content":"本文 <b>&</b>","embedded":true,"embedding_model":"m"},` +
		`{"chunk_index":1,"page":null,"page_end":null,"page_label":null,"page_end_label":null,"kind":null,"heading":null,"content":"二つ目","embedded":false,"embedding_model":null}],` +
		`"original_pdf":true}`
	if rec.Code != 200 || rec.Body.String() != want || rec.Header().Get("Cache-Control") != "no-store" || rec.Header().Get("Content-Type") != "application/json" {
		t.Fatalf("%d %v\n got  %s\n want %s", rec.Code, rec.Header(), rec.Body.String(), want)
	}
}

func TestChunksOriginalPDF(t *testing.T) {
	// 置き場に無い・中身が違う → false。記録済み（original_verified_at）なら置き場を見ずに true
	for name, files := range map[string]map[string]string{"missing": nil, "different content": {"cafe/menu.pdf": "OTHER"}} {
		s := serverWithPDFs(t, baseConfig(), detailStore(), files)
		if body := do(s, "GET", "/documents/1/chunks", nil).Body.String(); !strings.HasSuffix(body, `"original_pdf":false}`) {
			t.Errorf("%s: %s", name, body)
		}
	}
	s := serverWithPDFs(t, baseConfig(), detailStore(), nil)
	if body := do(s, "GET", "/documents/4/chunks", nil).Body.String(); !strings.HasSuffix(body, `"original_pdf":true}`) {
		t.Errorf("a recorded check must answer without the store: %s", body)
	}
}

func TestChunksR2FallbackIsCachedAndRecordedChecksSkipR2(t *testing.T) {
	f := newFakeR2(t, map[string][]byte{"cafe/menu.pdf": []byte("MENU")})
	cfg := baseConfig()
	cfg.PDFStore = pdfStoreR2
	env := r2Env("key1", "secret1")
	cfg.get = func(k string) string { return env[k] }
	s := newTestServer(t, cfg, detailStore(), nil)
	s.pdfs.r2.endpoint = f.srv.URL
	if !strings.HasSuffix(do(s, "GET", "/documents/1/chunks", nil).Body.String(), `"original_pdf":true}`) {
		t.Fatal("unrecorded rows are checked against R2")
	}
	calls := f.requests.Load()
	do(s, "GET", "/documents/1/chunks", nil)
	if f.requests.Load() != calls {
		t.Fatalf("the second answer must come from the in-process cache: %d -> %d", calls, f.requests.Load())
	}
	do(s, "GET", "/documents/4/chunks", nil)
	if f.requests.Load() != calls {
		t.Fatal("a recorded check must not ask R2")
	}
}

func TestChunksErrors(t *testing.T) {
	db := detailStore()
	s := serverWithPDFs(t, baseConfig(), db, nil)
	notFound := `{"detail":"指定された資料が見つかりません。"}`
	if rec := do(s, "GET", "/documents/99/chunks", nil); rec.Code != 404 || rec.Body.String() != notFound || rec.Header().Get("Cache-Control") != "" {
		t.Fatalf("%d %s %v", rec.Code, rec.Body.String(), rec.Header())
	}
	if rec := do(newTestServer(t, baseConfig(), nil, nil), "GET", "/documents/1/chunks", nil); rec.Code != 503 || rec.Body.String() != `{"detail":"資料のデータベースが設定されていません。"}` {
		t.Fatalf("no DB: %d %s", rec.Code, rec.Body.String())
	}
	db.err = errBoom
	rec := do(s, "GET", "/documents/1/chunks", nil)
	if rec.Code != 503 || rec.Body.String() != `{"detail":"`+detailDocumentsUnavailable+`"}` || rec.Header().Get("Cache-Control") != "" || strings.Contains(rec.Body.String(), "secret") {
		t.Fatalf("%d %s %v", rec.Code, rec.Body.String(), rec.Header())
	}
}

func TestChunksEnforceHidesOtherGroupsAsNotFound(t *testing.T) {
	cfg := baseConfig()
	cfg.AccessMode = modeEnforce
	cfg.CacheTTL = 0 // 見えるグループの控えを使わない（下で見える範囲を空にする）
	db := detailStore()
	db.visible = []string{"cafe"}
	s := serverWithPDFs(t, cfg, db, map[string]string{"cafe/menu.pdf": "MENU", "guides/g.pdf": "G"})
	if rec := do(s, "GET", "/documents/1/chunks", nil); rec.Code != 200 {
		t.Fatalf("visible: %d", rec.Code)
	}
	for _, path := range []string{"/documents/2/chunks", "/documents/3/chunks", "/documents/99/chunks"} { // 別グループ・グループなし・無い資料は、同じ 404
		if rec := do(s, "GET", path, nil); rec.Code != 404 || rec.Body.String() != `{"detail":"指定された資料が見つかりません。"}` {
			t.Fatalf("%s: %d %s", path, rec.Code, rec.Body.String())
		}
	}
	db.visible = nil
	if rec := do(s, "GET", "/documents/1/chunks", nil); rec.Code != 403 || !strings.Contains(rec.Body.String(), detailNoCollections) {
		t.Fatalf("nothing visible: %d %s", rec.Code, rec.Body.String())
	}
	// 見えるグループが 0 件でも、{id} が不正なら 422 が先（FastAPI は依存 → 引数の検査 → 本文の順）
	if rec := do(s, "GET", "/documents/abc/chunks", nil); rec.Code != 422 {
		t.Fatalf("validation precedes require_any: %d", rec.Code)
	}
}

// FastAPI（pydantic v2）の 422 と同じ本文。期待値は、Python の API の実際の応答から取った（2026-11-25）
func TestDocumentIDValidation(t *testing.T) {
	s := newTestServer(t, baseConfig(), detailStore(), nil)
	cases := map[string]string{
		"abc":                 `{"detail":[{"type":"int_parsing","loc":["path","document_id"],"msg":"Input should be a valid integer, unable to parse string as an integer","input":"abc"}]}`,
		"1.5":                 `{"detail":[{"type":"int_parsing","loc":["path","document_id"],"msg":"Input should be a valid integer, unable to parse string as an integer","input":"1.5"}]}`,
		"0":                   `{"detail":[{"type":"greater_than_equal","loc":["path","document_id"],"msg":"Input should be greater than or equal to 1","input":"0","ctx":{"ge":1}}]}`,
		"-5":                  `{"detail":[{"type":"greater_than_equal","loc":["path","document_id"],"msg":"Input should be greater than or equal to 1","input":"-5","ctx":{"ge":1}}]}`,
		"9223372036854775808": `{"detail":[{"type":"less_than_equal","loc":["path","document_id"],"msg":"Input should be less than or equal to 9223372036854775807","input":"9223372036854775808","ctx":{"le":9223372036854775807}}]}`,
		"%20":                 `{"detail":[{"type":"int_parsing","loc":["path","document_id"],"msg":"Input should be a valid integer, unable to parse string as an integer","input":" "}]}`,
	}
	for id, want := range cases {
		for _, leaf := range []string{"chunks", "file", "hero"} {
			rec := do(s, "GET", "/documents/"+id+"/"+leaf, nil)
			if rec.Code != 422 || rec.Body.String() != want || rec.Header().Get("Content-Type") != "application/json" {
				t.Errorf("%s/%s: %d %s", id, leaf, rec.Code, rec.Body.String())
			}
		}
	}
	// 通る形（pydantic の lax な読み方）: 1.0・+3・01・1_0・前後の空白・改行
	for _, id := range []string{"1.0", "1.00", "+1", "01", "0_1", "%201%20", "%0A1", "%C2%A01"} {
		if rec := do(s, "GET", "/documents/"+id+"/chunks", nil); rec.Code != 200 {
			t.Errorf("%q must be accepted as 1: %d %s", id, rec.Code, rec.Body.String())
		}
	}
	for _, id := range []string{"1_", "_1", "1__0", "1.", ".0", "1e2", "0x10", "%EF%BC%91", "1.50", "+%201", "++1"} { // 全角の 1 なども不可
		if rec := do(s, "GET", "/documents/"+id+"/chunks", nil); rec.Code != 422 {
			t.Errorf("%q must be rejected: %d", id, rec.Code)
		}
	}
}

func TestFileServesOnlyAVerifiedPDF(t *testing.T) {
	cfg := baseConfig()
	s := serverWithPDFs(t, cfg, detailStore(), map[string]string{"cafe/menu.pdf": "MENU-BYTES"})
	// ハッシュが一致しないので 409（この資料の PDF は DB の値とは別物）
	db := detailStore()
	db.details[1].ContentHash = sum("MENU-BYTES")
	s = serverWithPDFs(t, cfg, db, map[string]string{"cafe/menu.pdf": "MENU-BYTES"})
	rec := do(s, "GET", "/documents/1/file", nil)
	if rec.Code != 200 || rec.Body.String() != "MENU-BYTES" || rec.Header().Get("Content-Type") != "application/pdf" ||
		rec.Header().Get("Cache-Control") != "no-store" || rec.Header().Get("X-Content-Type-Options") != "nosniff" ||
		rec.Header().Get("Content-Disposition") != `inline; filename="document-1.pdf"; filename*=UTF-8''menu.pdf` {
		t.Fatalf("%d %v %q", rec.Code, rec.Header(), rec.Body.String())
	}
	for name, want := range map[string]struct {
		files  map[string]string
		status int
		body   string
	}{
		"mismatch": {map[string]string{"cafe/menu.pdf": "TAMPERED"}, 409, `{"detail":"PDF が、DB に保存された資料と一致しません。"}`},
		"missing":  {nil, 404, `{"detail":"PDF は利用できません。"}`},
	} {
		rec := do(serverWithPDFs(t, cfg, db, want.files), "GET", "/documents/1/file", nil)
		if rec.Code != want.status || rec.Body.String() != want.body || rec.Header().Get("Cache-Control") != "" || rec.Header().Get("Content-Disposition") != "" {
			t.Errorf("%s: %d %s %v", name, rec.Code, rec.Body.String(), rec.Header())
		}
	}
}

func TestFileContentDispositionEncodesTheNameLikePython(t *testing.T) {
	got := contentDisposition(7, "資料 (1)+あ/b.pdf")
	want := `inline; filename="document-7.pdf"; filename*=UTF-8''%E8%B3%87%E6%96%99%20%281%29%2B%E3%81%82%2Fb.pdf`
	if got != want {
		t.Fatalf("\n got  %s\n want %s", got, want)
	}
}

func TestFileR2FailureIs503AndOtherGroupsAre404(t *testing.T) {
	cfg := baseConfig()
	cfg.PDFStore = pdfStoreR2 // 設定が無い → 読めなかった（ローカルへ切り替えない）
	db := detailStore()
	rec := do(newTestServer(t, cfg, db, nil), "GET", "/documents/1/file", nil)
	if rec.Code != 503 || rec.Body.String() != `{"detail":"PDF を取得できませんでした。しばらく待ってから再試行してください。"}` {
		t.Fatalf("%d %s", rec.Code, rec.Body.String())
	}
	cfg = baseConfig()
	cfg.AccessMode = modeEnforce
	db.visible = []string{"cafe"}
	if rec := do(newTestServer(t, cfg, db, nil), "GET", "/documents/2/file", nil); rec.Code != 404 || rec.Body.String() != `{"detail":"指定された資料が見つかりません。"}` {
		t.Fatalf("hidden: %d %s", rec.Code, rec.Body.String())
	}
}

func TestHero(t *testing.T) {
	f := newFakeR2(t, map[string][]byte{"hero/1.png": []byte("PNG-BYTES"), "hero/2.png": []byte("G")})
	cfg := baseConfig()
	env := r2Env("key1", "secret1")
	cfg.get = func(k string) string { return env[k] }
	cfg.AccessMode = modeEnforce
	db := detailStore()
	db.visible = []string{"cafe"}
	s := newTestServer(t, cfg, db, nil)
	s.pdfs.r2.endpoint = f.srv.URL
	rec := do(s, "GET", "/documents/1/hero", nil)
	if rec.Code != 200 || rec.Body.String() != "PNG-BYTES" || rec.Header().Get("Content-Type") != "image/png" ||
		rec.Header().Get("Cache-Control") != "no-store" || rec.Header().Get("X-Content-Type-Options") != "nosniff" {
		t.Fatalf("%d %v %q", rec.Code, rec.Header(), rec.Body.String())
	}
	none := `{"detail":"この資料の画像はありません。"}`
	for _, id := range []string{"2", "3", "4", "99"} { // 見えないグループ・許可しない MIME・R2 に無い・画像なしは、同じ 404
		if rec := do(s, "GET", "/documents/"+id+"/hero", nil); rec.Code != 404 || rec.Body.String() != none {
			t.Errorf("%s: %d %s", id, rec.Code, rec.Body.String())
		}
	}
	// R2 の設定が無い → 503（画像は、PDF_STORE=local でも R2 から読む）
	cfg2 := baseConfig()
	rec = do(newTestServer(t, cfg2, detailStore(), nil), "GET", "/documents/1/hero", nil)
	if rec.Code != 503 || rec.Body.String() != `{"detail":"`+detailDocumentsUnavailable+`"}` {
		t.Fatalf("no R2: %d %s", rec.Code, rec.Body.String())
	}
}

func TestDocumentSubPathsOtherThanTheThreeAre404AndMethodsAreGetOnly(t *testing.T) {
	s := newTestServer(t, baseConfig(), detailStore(), nil)
	for _, path := range []string{"/documents/", "/documents/1", "/documents/1/", "/documents/1/graph", "/documents/1/impact", "/documents//chunks", "/documents/1/chunks/x", "/threads/x/messages", "/threads/"} {
		if rec := do(s, "GET", path, nil); rec.Code != 404 {
			t.Errorf("%s: %d", path, rec.Code)
		}
	}
	for _, path := range []string{"/documents/1/chunks", "/documents/abc/file", "/documents/1/hero", "/threads"} {
		rec := do(s, "POST", path, nil)
		if rec.Code != 405 || rec.Header().Get("Allow") != "GET" {
			t.Errorf("POST %s: %d %v", path, rec.Code, rec.Header())
		}
	}
}
