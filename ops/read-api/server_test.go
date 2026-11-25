package main

import (
	"context"
	"net/http/httptest"
	"strings"
	"testing"
)

func str(s string) *string { return &s }
func i32(n int32) *int32   { return &n }

func baseConfig() config {
	return config{
		Port: "8080", PoolMaxSize: 2, AuthMode: modeOff, AccessMode: modeOff, MembershipMode: modeOff,
		CacheTTL: defaultCacheTTL, Audience: "docunavi", CORSOrigins: []string{"https://app.example.test"},
	}
}

func newTestServer(t *testing.T, cfg config, db store, verifier *tokenVerifier) *server {
	t.Helper()
	logf := quietLog(t)
	var dbStore store
	if db != nil {
		dbStore = db
	}
	return &server{cfg: cfg, db: dbStore, verifier: verifier, logf: logf, checker: newAccessChecker(cfg, dbStore, nowFunc(), logf)}
}

func do(s *server, method, path string, headers map[string]string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, nil)
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	rec := httptest.NewRecorder()
	s.ServeHTTP(rec, req)
	return rec
}

func sampleStore() *fakeStore {
	return &fakeStore{
		collectionRows: []collectionRow{
			{Name: "cafe", Label: str(" カフェ "), Description: str("喫茶の資料"), SortOrder: i32(1), Registered: true, DocumentCount: 5, ReadyCount: 4},
			{Name: "new-group", Registered: false, DocumentCount: 1, ReadyCount: 0},
			{Name: "blank", Label: str("   "), Description: str(""), SortOrder: i32(2), Registered: true},
		},
		targetRows: []classificationRow{{Name: "dorm", Label: "寮", Description: "入寮規程", SortOrder: 1, DocumentCount: 3, ReadyCount: 3}},
		typeRows:   []classificationRow{{Name: "rule", Label: "規程", Description: "", SortOrder: 0, DocumentCount: 2, ReadyCount: 1}},
	}
}

func TestHealth(t *testing.T) {
	rec := do(newTestServer(t, baseConfig(), nil, nil), "GET", "/health", nil)
	if rec.Code != 200 || rec.Body.String() != `{"status":"ok"}` || rec.Header().Get("Content-Type") != "application/json" {
		t.Fatalf("%d %q %v", rec.Code, rec.Body.String(), rec.Header())
	}
}

// Python（FastAPI + pydantic）の応答と、項目の名前・順序・値・ヘッダーが同じ（non-ASCII もそのまま）
func TestListsMatchThePythonShape(t *testing.T) {
	s := newTestServer(t, baseConfig(), sampleStore(), nil)
	cases := []struct{ path, body string }{
		{"/collections", `{"configured":true,"collections":[` +
			`{"name":"cafe","label":"カフェ","description":"喫茶の資料","sort_order":1,"registered":true,"document_count":5,"ready_count":4},` +
			`{"name":"new-group","label":"new-group","description":"","sort_order":null,"registered":false,"document_count":1,"ready_count":0},` +
			`{"name":"blank","label":"blank","description":"","sort_order":2,"registered":true,"document_count":0,"ready_count":0}]}`},
		{"/targets", `{"configured":true,"targets":[{"name":"dorm","label":"寮","description":"入寮規程","sort_order":1,"document_count":3,"ready_count":3}]}`},
		{"/doc-types", `{"configured":true,"doc_types":[{"name":"rule","label":"規程","description":"","sort_order":0,"document_count":2,"ready_count":1}]}`},
	}
	for _, tc := range cases {
		rec := do(s, "GET", tc.path, nil)
		if rec.Code != 200 || rec.Body.String() != tc.body {
			t.Fatalf("%s: %d\n got  %s\n want %s", tc.path, rec.Code, rec.Body.String(), tc.body)
		}
		if rec.Header().Get("Cache-Control") != "no-store" || rec.Header().Get("Content-Type") != "application/json" {
			t.Fatalf("%s headers: %v", tc.path, rec.Header())
		}
	}
}

func TestWithoutDBReturnsConfiguredFalse(t *testing.T) {
	s := newTestServer(t, baseConfig(), nil, nil)
	for path, want := range map[string]string{
		"/collections": `{"configured":false,"collections":[]}`,
		"/targets":     `{"configured":false,"targets":[]}`,
		"/doc-types":   `{"configured":false,"doc_types":[]}`,
	} {
		if rec := do(s, "GET", path, nil); rec.Code != 200 || rec.Body.String() != want {
			t.Fatalf("%s: %d %s", path, rec.Code, rec.Body.String())
		}
	}
}

func TestDBFailureIsAFixedMessageWithNoStore(t *testing.T) {
	db := sampleStore()
	db.err = errBoom
	s := newTestServer(t, baseConfig(), db, nil)
	for path, detail := range map[string]string{"/collections": detailCollectionsError, "/targets": detailClassifyError, "/doc-types": detailClassifyError} {
		rec := do(s, "GET", path, nil)
		if rec.Code != 503 || rec.Header().Get("Cache-Control") != "no-store" || !strings.Contains(rec.Body.String(), detail) {
			t.Fatalf("%s: %d %s %v", path, rec.Code, rec.Body.String(), rec.Header())
		}
		if strings.Contains(rec.Body.String(), "secret") {
			t.Fatalf("the error text leaked: %s", rec.Body.String())
		}
	}
}

func TestEnforceFiltersByTheVisibleGroups(t *testing.T) {
	cfg := baseConfig()
	cfg.AccessMode = modeEnforce
	db := sampleStore()
	db.visible = []string{"new-group", "cafe"}
	s := newTestServer(t, cfg, db, nil)
	rec := do(s, "GET", "/collections", nil)
	if rec.Code != 200 || strings.Contains(rec.Body.String(), `"blank"`) {
		t.Fatalf("a group outside the scope was returned: %d %s", rec.Code, rec.Body.String())
	}
	if got := strings.Join(db.lastAllowed, ","); got != "cafe,new-group" || !db.lastAllowedOK {
		t.Fatalf("the SQL was not narrowed (sorted names): %q", got)
	}
	do(s, "GET", "/targets", nil)
	if got := strings.Join(db.lastAllowed, ","); got != "cafe,new-group" {
		t.Fatalf("/targets was not narrowed: %q", got)
	}
}

func TestEnforceWithNothingVisibleIs403WithoutNoStore(t *testing.T) {
	cfg := baseConfig()
	cfg.AccessMode = modeEnforce
	db := sampleStore()
	db.visible = []string{}
	s := newTestServer(t, cfg, db, nil)
	for _, path := range []string{"/collections", "/targets", "/doc-types"} {
		rec := do(s, "GET", path, nil)
		if rec.Code != 403 || rec.Body.String() != `{"detail":"利用できる資料がありません。"}` || rec.Header().Get("Cache-Control") != "" {
			t.Fatalf("%s: %d %s %v", path, rec.Code, rec.Body.String(), rec.Header())
		}
	}
}

func TestEnforceFailsClosedAndShadowFailsOpen(t *testing.T) {
	cfg := baseConfig()
	cfg.AccessMode = modeEnforce
	db := sampleStore()
	db.err = errBoom
	rec := do(newTestServer(t, cfg, db, nil), "GET", "/collections", nil)
	if rec.Code != 503 || !strings.Contains(rec.Body.String(), detailAccessUnavailable) {
		t.Fatalf("enforce: %d %s", rec.Code, rec.Body.String())
	}
	cfg.AccessMode = modeShadow
	db2 := sampleStore()
	db2.err = errBoom // 権限を引けない。shadow は制限なしで続ける（が、一覧の問い合わせも同じ DB なので失敗する）
	db2.collectionRows = nil
	rec = do(newTestServer(t, cfg, db2, nil), "GET", "/collections", nil)
	if db2.lastAllowedOK {
		t.Fatal("shadow must not narrow the query")
	}
}

func TestShadowDoesNotRestrict(t *testing.T) {
	cfg := baseConfig()
	cfg.AccessMode = modeShadow
	db := sampleStore()
	db.visible = []string{"cafe"}
	s := newTestServer(t, cfg, db, nil)
	rec := do(s, "GET", "/collections", nil)
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), `"blank"`) || db.lastAllowedOK {
		t.Fatalf("shadow restricted the result: %d allowed=%v", rec.Code, db.lastAllowed)
	}
}

func TestVisibleGroupsAreCachedForTheTTL(t *testing.T) {
	cfg := baseConfig()
	cfg.AccessMode = modeEnforce
	db := sampleStore()
	db.visible = []string{"cafe"}
	s := newTestServer(t, cfg, db, nil)
	do(s, "GET", "/collections", nil)
	do(s, "GET", "/targets", nil)
	if db.visibleCalls != 1 {
		t.Fatalf("visible groups were read %d times within the TTL", db.visibleCalls)
	}
	cfg.CacheTTL = 0 // 0 で無効
	db2 := sampleStore()
	db2.visible = []string{"cafe"}
	s2 := newTestServer(t, cfg, db2, nil)
	do(s2, "GET", "/collections", nil)
	do(s2, "GET", "/targets", nil)
	if db2.visibleCalls != 2 {
		t.Fatalf("TTL 0 must not cache: %d", db2.visibleCalls)
	}
}

func authedServer(t *testing.T, mutate func(*config, *fakeStore)) (*server, *testKeys, *fakeStore) {
	t.Helper()
	k := newTestKeys(t)
	v, _ := newTestVerifier(t, k)
	cfg := baseConfig()
	cfg.AuthMode = authRequired
	db := sampleStore()
	if mutate != nil {
		mutate(&cfg, db)
	}
	return newTestServer(t, cfg, db, v), k, db
}

func bearer(tok string) map[string]string { return map[string]string{"Authorization": "Bearer " + tok} }

func TestAuthRequired(t *testing.T) {
	s, k, _ := authedServer(t, nil)
	for name, headers := range map[string]map[string]string{
		"no header":    nil,
		"basic scheme": {"Authorization": "Basic abc"},
		"empty bearer": {"Authorization": "Bearer "},
		"garbage":      bearer("abc"),
		"bad aud":      bearer(k.mint(t, algES256, "k1", goodClaims(map[string]any{"aud": "x"}))),
	} {
		rec := do(s, "GET", "/collections", headers)
		if rec.Code != 401 || rec.Header().Get("WWW-Authenticate") != "Bearer" || rec.Body.String() != `{"detail":"ログインが必要です。ログインし直してください。"}` {
			t.Fatalf("%s: %d %s %v", name, rec.Code, rec.Body.String(), rec.Header())
		}
	}
	if rec := do(s, "GET", "/collections", bearer(k.mint(t, algES256, "k1", goodClaims(nil)))); rec.Code != 200 {
		t.Fatalf("valid token: %d %s", rec.Code, rec.Body.String())
	}
	// /health は認証なし（起動の確認用）
	if rec := do(s, "GET", "/health", nil); rec.Code != 200 {
		t.Fatalf("health: %d", rec.Code)
	}
}

func TestAuthKeysUnavailableIs503(t *testing.T) {
	k := newTestKeys(t)
	cache := newJWKSCache("u", func(context.Context, string) ([]byte, error) { return nil, errBoom }, nowFunc(), quietLog(t))
	v := &tokenVerifier{issuer: "https://auth.example.test", audience: "docunavi", keys: cache, now: nowFunc()}
	cfg := baseConfig()
	cfg.AuthMode = authRequired
	s := newTestServer(t, cfg, sampleStore(), v)
	rec := do(s, "GET", "/collections", bearer(k.mint(t, algES256, "k1", goodClaims(nil))))
	if rec.Code != 503 || !strings.Contains(rec.Body.String(), detailAuthUnavailable) {
		t.Fatalf("%d %s", rec.Code, rec.Body.String())
	}
}

func TestMembership(t *testing.T) {
	s, k, db := authedServer(t, func(cfg *config, db *fakeStore) {
		cfg.MembershipMode = modeEnforce
		db.members = map[string]bool{"user_abcdefgh1234": true}
	})
	if rec := do(s, "GET", "/collections", bearer(k.mint(t, algES256, "k1", goodClaims(nil)))); rec.Code != 200 {
		t.Fatalf("member: %d %s", rec.Code, rec.Body.String())
	}
	other := bearer(k.mint(t, algES256, "k1", goodClaims(map[string]any{"sub": "user_zzzzzzzz9999"})))
	rec := do(s, "GET", "/collections", other)
	if rec.Code != 403 || rec.Body.String() != `{"detail":"このアカウントには利用許可がありません。","code":"APP_MEMBERSHIP_REQUIRED"}` {
		t.Fatalf("non-member: %d %s", rec.Code, rec.Body.String())
	}
	do(s, "GET", "/targets", other)
	if db.memberCalls != 2 { // 資格のある人 1 回、ない人 1 回。2 回目の /targets は控えから
		t.Fatalf("membership lookups: %d", db.memberCalls)
	}
	db.err = errBoom // 引けない: enforce は拒否ではなく 503（許可には倒れない）
	s2, k2, db2 := authedServer(t, func(cfg *config, db *fakeStore) { cfg.MembershipMode = modeEnforce })
	db2.err = errBoom
	rec = do(s2, "GET", "/collections", bearer(k2.mint(t, algES256, "k1", goodClaims(nil))))
	if rec.Code != 503 || !strings.Contains(rec.Body.String(), detailMembershipUnavailable) {
		t.Fatalf("lookup failure: %d %s", rec.Code, rec.Body.String())
	}
}

func TestMembershipShadowNeverRejects(t *testing.T) {
	s, k, _ := authedServer(t, func(cfg *config, db *fakeStore) { cfg.MembershipMode = modeShadow })
	if rec := do(s, "GET", "/collections", bearer(k.mint(t, algES256, "k1", goodClaims(nil)))); rec.Code != 200 {
		t.Fatalf("shadow rejected: %d", rec.Code)
	}
}

func TestCORS(t *testing.T) {
	s := newTestServer(t, baseConfig(), sampleStore(), nil)
	origin := "https://app.example.test"
	rec := do(s, "OPTIONS", "/collections", map[string]string{
		"Origin": origin, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization",
	})
	h := rec.Header()
	if rec.Code != 204 || h.Get("Access-Control-Allow-Origin") != origin || h.Get("Access-Control-Allow-Headers") != "authorization" ||
		h.Get("Access-Control-Max-Age") != corsMaxAge || h.Get("Access-Control-Allow-Credentials") != "" {
		t.Fatalf("preflight: %d %v", rec.Code, h)
	}
	rec = do(s, "OPTIONS", "/collections", map[string]string{"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
	if rec.Code != 400 || rec.Header().Get("Access-Control-Allow-Origin") != "" {
		t.Fatalf("disallowed preflight: %d %v", rec.Code, rec.Header())
	}
	if rec = do(s, "GET", "/collections", map[string]string{"Origin": origin}); rec.Header().Get("Access-Control-Allow-Origin") != origin {
		t.Fatalf("simple request: %v", rec.Header())
	}
	if rec = do(s, "GET", "/collections", map[string]string{"Origin": "https://evil.example"}); rec.Header().Get("Access-Control-Allow-Origin") != "" {
		t.Fatalf("a foreign origin was allowed: %v", rec.Header())
	}
	// CORS の設定が無ければ、どのオリジンも許さない
	cfg := baseConfig()
	cfg.CORSOrigins = nil
	rec = do(newTestServer(t, cfg, sampleStore(), nil), "GET", "/collections", map[string]string{"Origin": origin})
	if rec.Header().Get("Access-Control-Allow-Origin") != "" {
		t.Fatalf("no CORS_ORIGINS must allow nobody: %v", rec.Header())
	}
}

func TestRoutingErrorsAreFastApiShaped(t *testing.T) {
	s := newTestServer(t, baseConfig(), sampleStore(), nil)
	if rec := do(s, "GET", "/nope", nil); rec.Code != 404 || rec.Body.String() != `{"detail":"Not Found"}` {
		t.Fatalf("404: %d %s", rec.Code, rec.Body.String())
	}
	for _, method := range []string{"POST", "DELETE", "PUT", "HEAD"} {
		rec := do(s, method, "/collections", nil)
		if rec.Code != 405 || rec.Header().Get("Allow") != "GET" || (method != "HEAD" && rec.Body.String() != `{"detail":"Method Not Allowed"}`) {
			t.Fatalf("%s: %d %s %v", method, rec.Code, rec.Body.String(), rec.Header())
		}
	}
	if rec := do(s, "GET", "/collections/", nil); rec.Code != 404 {
		t.Fatalf("trailing slash: %d", rec.Code)
	}
}

func TestVerifyAccessConfig(t *testing.T) {
	logf := quietLog(t)
	ctx := t.Context()
	cfg := baseConfig()
	cfg.AccessMode = modeEnforce
	if err := verifyAccessConfig(ctx, cfg, &fakeStore{roles: 0}, logf); err == nil {
		t.Fatal("a missing default role must fail the startup")
	}
	if err := verifyAccessConfig(ctx, cfg, &fakeStore{roles: 1, groups: 1}, logf); err != nil {
		t.Fatal(err)
	}
	if err := verifyAccessConfig(ctx, cfg, &fakeStore{err: errBoom}, logf); err == nil || strings.Contains(err.Error(), "secret") {
		t.Fatalf("a probe failure must fail without leaking: %v", err)
	}
	if err := verifyAccessConfig(ctx, cfg, nil, logf); err == nil {
		t.Fatal("enforce without a DB must fail")
	}
	cfg.AccessMode = modeOff
	if err := verifyAccessConfig(ctx, cfg, nil, logf); err != nil {
		t.Fatalf("off reads nothing: %v", err)
	}
	cfg.MembershipMode = modeEnforce
	if err := verifyAccessConfig(ctx, cfg, &fakeStore{err: errBoom}, logf); err == nil {
		t.Fatal("an unreadable app_members must fail the startup")
	}
}

func TestLoadConfig(t *testing.T) {
	env := func(m map[string]string) func(string) string { return func(k string) string { return m[k] } }
	if c, err := loadConfig(env(nil)); err != nil || c.Port != "8080" || c.PoolMaxSize != 2 || c.AuthMode != "off" || c.CacheTTL != defaultCacheTTL {
		t.Fatalf("defaults: %+v %v", c, err)
	}
	bad := map[string]map[string]string{
		"unknown ACCESS_MODE":       {"ACCESS_MODE": "strict"},
		"unknown AUTH_MODE":         {"AUTH_MODE": "optional"},
		"membership needs auth":     {"MEMBERSHIP_MODE": "enforce", "DATABASE_URL": "postgres://x"},
		"enforce needs a DB":        {"ACCESS_MODE": "enforce"},
		"required needs a base URL": {"AUTH_MODE": "required"},
		"http base url":             {"AUTH_MODE": "required", "AUTH_BASE_URL": "http://auth.example.test"},
		"base url with a path":      {"AUTH_MODE": "required", "AUTH_BASE_URL": "https://auth.example.test/api"},
		"negative ttl":              {"ACCESS_CACHE_TTL_SECONDS": "-1"},
		"nan ttl":                   {"ACCESS_CACHE_TTL_SECONDS": "NaN"},
		"zero pool":                 {"DATABASE_POOL_MAX_SIZE": "0"},
		"wildcard cors":             {"CORS_ORIGINS": "https://a.example.test, *"},
	}
	for name, m := range bad {
		if _, err := loadConfig(env(m)); err == nil {
			t.Errorf("%s: want an error", name)
		}
	}
	good := map[string]string{"AUTH_MODE": "required", "AUTH_BASE_URL": "http://127.0.0.1:8787/", "AUTH_AUDIENCE": "aud", "ACCESS_CACHE_TTL_SECONDS": "0", "CORS_ORIGINS": " https://a.example.test , https://b.example.test "}
	c, err := loadConfig(env(good))
	if err != nil || c.AuthBaseURL != "http://127.0.0.1:8787" || c.JWKSURL != "http://127.0.0.1:8787/api/auth/jwks" || c.Audience != "aud" || c.CacheTTL != 0 || len(c.CORSOrigins) != 2 {
		t.Fatalf("good: %+v %v", c, err)
	}
}
