package main

// Cloudflare R2（S3 互換）の読み取り。標準ライブラリだけ（SigV4 を自前で署名する。ADR 0017 の未決 5 の続き）。
// app/r2_store.py の読む側（collections / get）と同じ規則:
//   - バケットは private のまま。パススタイル `https://<ACCOUNT>.r2.cloudflarestorage.com/<bucket>/<key>`、region は auto、service は s3
//   - 認証エラー（401/403）が返ったら、鍵を読み直し、**鍵の ID が変わっていれば**新しい鍵で 1 回だけやり直す（再配備は要らない）。
//     ID が同じなら（本当に権限が無い）やり直さずに元のエラーを返す
//   - 一時的な失敗（接続・429・5xx）は、合計 3 回まで試す（botocore の standard と同じ回数）
//   - 設定・鍵・例外の文字列を、応答・ログに出さない（型の名前と HTTP ステータスだけ）

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	r2Region         = "auto"
	r2Service        = "s3"
	r2MaxAttempts    = 3
	r2RequestTimeout = 15 * time.Second
	// 本文のない GET の本文のハッシュ（空の sha256。botocore の署名と同じ値）
	emptyPayloadHash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)

// 資料群ではないプレフィックス。ヒーロー画像（DB の document_images.r2_key）が同じバケットの hero/ に入る（app/r2_store.py の NON_COLLECTION_PREFIXES）
var nonCollectionPrefixes = map[string]bool{"hero": true}

var (
	errR2Config      = errors.New("R2 の設定が足りません")
	errR2Auth        = errors.New("R2 の認証に失敗しました")
	errR2NotFound    = errors.New("R2 にオブジェクトがありません")
	errR2TooLarge    = errors.New("R2 のオブジェクトが大きすぎます")
	errR2Unavailable = errors.New("R2 を読めませんでした")
)

type r2Credentials struct {
	KeyID  string
	Secret string
}

// r2Settings は環境変数から作る設定。鍵は呼ぶたびに読む（Secret のボリュームの入れ替えに追従する。app/r2_store.py の config_from_env と同じ）。
type r2Settings struct {
	get func(string) string
}

// load は、アカウント ID・バケット・鍵を返す。足りなければ errR2Config（メッセージに変数の名前だけを入れる）。
func (s r2Settings) load() (account, bucket string, creds r2Credentials, err error) {
	account = strings.TrimSpace(s.get("R2_ACCOUNT_ID"))
	bucket = strings.TrimSpace(s.get("R2_BUCKET"))
	raw := strings.TrimSpace(s.get("R2_CREDENTIALS_JSON"))
	if raw == "" {
		if path := strings.TrimSpace(s.get("R2_CREDENTIALS_FILE")); path != "" {
			data, readErr := os.ReadFile(path)
			if readErr != nil {
				return "", "", creds, fmt.Errorf("%w: R2_CREDENTIALS_FILE を読めません", errR2Config)
			}
			raw = strings.TrimSpace(string(data))
		}
	}
	keyID, secret := strings.TrimSpace(s.get("R2_ACCESS_KEY_ID")), strings.TrimSpace(s.get("R2_SECRET_ACCESS_KEY"))
	if raw == "" && keyID != "" && secret != "" { // 鍵を 2 つの変数で渡す経路（JSON・ファイルが先）
		creds = r2Credentials{KeyID: keyID, Secret: secret}
	} else if raw != "" {
		var parsed struct {
			AccessKeyID     string `json:"access_key_id"`
			SecretAccessKey string `json:"secret_access_key"`
		}
		if json.Unmarshal([]byte(raw), &parsed) != nil {
			return "", "", creds, fmt.Errorf("%w: R2_CREDENTIALS_JSON が JSON ではありません", errR2Config)
		}
		if parsed.AccessKeyID == "" || parsed.SecretAccessKey == "" {
			return "", "", creds, fmt.Errorf("%w: R2_CREDENTIALS_JSON に access_key_id と secret_access_key が要ります", errR2Config)
		}
		creds = r2Credentials{KeyID: parsed.AccessKeyID, Secret: parsed.SecretAccessKey}
	}
	var missing []string
	if account == "" {
		missing = append(missing, "R2_ACCOUNT_ID")
	}
	if bucket == "" {
		missing = append(missing, "R2_BUCKET")
	}
	if creds.KeyID == "" {
		missing = append(missing, "R2_CREDENTIALS_JSON")
	}
	if len(missing) > 0 {
		return "", "", r2Credentials{}, fmt.Errorf("%w: %s", errR2Config, strings.Join(missing, ", "))
	}
	return account, bucket, creds, nil
}

// r2Client はバケット 1 つの読み取り。endpoint が空なら設定（R2_ACCOUNT_ID）から決める。テストでは偽のサーバーの URL を渡す。
type r2Client struct {
	settings r2Settings
	endpoint string // 空なら https://<ACCOUNT>.r2.cloudflarestorage.com
	http     *http.Client
	now      func() time.Time
	sleep    func(time.Duration)

	mu    sync.Mutex
	creds *r2Credentials // 使っている鍵（認証エラーのとき、読み直した鍵の ID と比べる）
}

func newR2Client(settings r2Settings) *r2Client {
	return &r2Client{
		settings: settings,
		http:     &http.Client{Timeout: r2RequestTimeout, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }},
		now:      time.Now,
		sleep:    time.Sleep,
	}
}

// currentCreds は、いまの鍵。まだ無ければ読む（読めなければ設定のエラー。控えない）。
func (c *r2Client) currentCreds() (account, bucket string, creds r2Credentials, err error) {
	account, bucket, fresh, err := c.settings.load()
	if err != nil {
		return "", "", r2Credentials{}, err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.creds == nil {
		c.creds = &fresh
	}
	return account, bucket, *c.creds, nil
}

// reload は、認証エラーのあとに、鍵を読み直す。ID が変わっていれば新しい鍵を使う（true）。同じなら何も変えない（false）。
func (c *r2Client) reload(old r2Credentials) bool {
	_, _, fresh, err := c.settings.load()
	if err != nil || fresh.KeyID == old.KeyID {
		return false
	}
	c.mu.Lock()
	c.creds = &fresh
	c.mu.Unlock()
	return true
}

// uriEncode は SigV4 の URI エンコード（RFC 3986 の予約されない文字以外を %XX にする）。encodeSlash が false なら / は残す。
func uriEncode(s string, encodeSlash bool) string {
	var b strings.Builder
	for _, c := range []byte(s) {
		switch {
		case c >= 'A' && c <= 'Z', c >= 'a' && c <= 'z', c >= '0' && c <= '9', c == '-', c == '_', c == '.', c == '~':
			b.WriteByte(c)
		case c == '/' && !encodeSlash:
			b.WriteByte(c)
		default:
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

func hmacSHA256(key []byte, data string) []byte {
	m := hmac.New(sha256.New, key)
	m.Write([]byte(data))
	return m.Sum(nil)
}

// signV4 は、GET（本文なし）の Authorization ヘッダーを作る。署名するヘッダーは host・x-amz-content-sha256・x-amz-date。
func signV4(creds r2Credentials, host, canonicalURI, canonicalQuery string, at time.Time) (amzDate, authorization string) {
	at = at.UTC()
	amzDate = at.Format("20060102T150405Z")
	day := at.Format("20060102")
	signedHeaders := "host;x-amz-content-sha256;x-amz-date"
	canonicalHeaders := "host:" + host + "\nx-amz-content-sha256:" + emptyPayloadHash + "\nx-amz-date:" + amzDate + "\n"
	canonicalRequest := strings.Join([]string{"GET", canonicalURI, canonicalQuery, canonicalHeaders, signedHeaders, emptyPayloadHash}, "\n")
	scope := day + "/" + r2Region + "/" + r2Service + "/aws4_request"
	hash := sha256.Sum256([]byte(canonicalRequest))
	stringToSign := "AWS4-HMAC-SHA256\n" + amzDate + "\n" + scope + "\n" + hex.EncodeToString(hash[:])
	key := hmacSHA256([]byte("AWS4"+creds.Secret), day)
	key = hmacSHA256(key, r2Region)
	key = hmacSHA256(key, r2Service)
	key = hmacSHA256(key, "aws4_request")
	signature := hex.EncodeToString(hmacSHA256(key, stringToSign))
	authorization = "AWS4-HMAC-SHA256 Credential=" + creds.KeyID + "/" + scope + ", SignedHeaders=" + signedHeaders + ", Signature=" + signature
	return amzDate, authorization
}

func canonicalQueryOf(params map[string]string) string {
	keys := make([]string, 0, len(params))
	for k := range params {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, uriEncode(k, true)+"="+uriEncode(params[k], true))
	}
	return strings.Join(parts, "&")
}

// do は 1 回の GET。status と、（200 のとき）読み込み用の本文を返す。呼び出し側が本文を閉じる。
func (c *r2Client) do(ctx context.Context, key string, params map[string]string) (*http.Response, error) {
	for reloaded := false; ; {
		account, bucket, creds, err := c.currentCreds()
		if err != nil {
			return nil, err
		}
		base := c.endpoint
		if base == "" {
			base = "https://" + account + ".r2.cloudflarestorage.com"
		}
		u, err := url.Parse(base)
		if err != nil {
			return nil, errR2Config
		}
		path := "/" + bucket
		if key != "" {
			path += "/" + key
		}
		canonicalURI := uriEncode(path, false)
		query := canonicalQueryOf(params)
		var resp *http.Response
		for attempt := 1; ; attempt++ {
			amzDate, authorization := signV4(creds, u.Host, canonicalURI, query, c.now())
			target := u.Scheme + "://" + u.Host + canonicalURI
			if query != "" {
				target += "?" + query
			}
			req, reqErr := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
			if reqErr != nil {
				return nil, errR2Unavailable
			}
			req.Header.Set("X-Amz-Date", amzDate)
			req.Header.Set("X-Amz-Content-Sha256", emptyPayloadHash)
			req.Header.Set("Authorization", authorization)
			resp, err = c.http.Do(req)
			retryable := err != nil && ctx.Err() == nil || err == nil && (resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500)
			if !retryable || attempt >= r2MaxAttempts {
				break
			}
			if resp != nil {
				_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<16))
				resp.Body.Close()
			}
			c.sleep(time.Duration(attempt*attempt) * 100 * time.Millisecond)
		}
		if err != nil {
			return nil, errR2Unavailable
		}
		if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
			resp.Body.Close()
			if !reloaded && c.reload(creds) {
				reloaded = true
				continue
			}
			return nil, errR2Auth
		}
		return resp, nil
	}
}

type listResult struct {
	IsTruncated           bool     `xml:"IsTruncated"`
	NextContinuationToken string   `xml:"NextContinuationToken"`
	Prefixes              []string `xml:"CommonPrefixes>Prefix"`
}

const maxListBytes = 4 << 20

// collections は、バケット直下の「資料群」（`cafe/` のような最上位のプレフィックス）を名前順に。画像の置き場（hero/）は含めない。
func (c *r2Client) collections(ctx context.Context) ([]string, error) {
	var names []string
	token := ""
	for {
		params := map[string]string{"list-type": "2", "delimiter": "/"}
		if token != "" {
			params["continuation-token"] = token
		}
		resp, err := c.do(ctx, "", params)
		if err != nil {
			return nil, err
		}
		body, readErr := io.ReadAll(io.LimitReader(resp.Body, maxListBytes+1))
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK || readErr != nil || len(body) > maxListBytes {
			return nil, errR2Unavailable
		}
		var page listResult
		if xml.Unmarshal(body, &page) != nil {
			return nil, errR2Unavailable
		}
		for _, p := range page.Prefixes {
			if name := strings.TrimRight(p, "/"); name != "" && !nonCollectionPrefixes[name] {
				names = append(names, name)
			}
		}
		if !page.IsTruncated || page.NextContinuationToken == "" {
			break
		}
		token = page.NextContinuationToken
	}
	sort.Strings(names)
	return names, nil
}

// get は、オブジェクトの中身。無い（404）・大きすぎるなら (nil, false, nil)。それ以外の失敗（権限・通信）はエラー。
func (c *r2Client) get(ctx context.Context, key string, maxBytes int64) ([]byte, bool, error) {
	resp, err := c.do(ctx, key, nil)
	if err != nil {
		return nil, false, err
	}
	defer resp.Body.Close()
	switch {
	case resp.StatusCode == http.StatusNotFound:
		return nil, false, nil
	case resp.StatusCode != http.StatusOK:
		return nil, false, errR2Unavailable
	}
	if resp.ContentLength > maxBytes {
		return nil, false, nil
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxBytes+1))
	if err != nil {
		return nil, false, errR2Unavailable
	}
	if int64(len(data)) > maxBytes {
		return nil, false, nil
	}
	return data, true, nil
}
