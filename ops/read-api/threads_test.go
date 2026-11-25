package main

import (
	"strings"
	"testing"
	"time"
)

func threadStore() *fakeStore {
	db := sampleStore()
	t0 := time.Date(2026, 11, 20, 3, 4, 5, 0, time.UTC)
	t1 := time.Date(2026, 11, 21, 3, 4, 5, 123_999_000, time.UTC)
	db.threadRows = []threadRow{
		{ThreadID: "user_abcdefgh1234:thread-b", CreatedAt: &t0, UpdatedAt: &t1, Title: str("最初の質問 <b>&")},
		{ThreadID: "user_abcdefgh1234:thread-a", Title: nil},
	}
	return db
}

func TestThreadsListMatchesThePythonShape(t *testing.T) {
	s, k, db := authedServer(t, nil)
	db.threadRows = threadStore().threadRows
	rec := do(s, "GET", "/threads", bearer(k.mint(t, algES256, "k1", goodClaims(nil))))
	want := `{"threads":[` +
		`{"thread_id":"thread-b","title":"最初の質問 <b>&","created_at":"2026-11-20T03:04:05Z","updated_at":"2026-11-21T03:04:05.123000Z"},` +
		`{"thread_id":"thread-a","title":"（題名なし）","created_at":null,"updated_at":null}]}`
	if rec.Code != 200 || rec.Body.String() != want || rec.Header().Get("Content-Type") != "application/json" {
		t.Fatalf("%d %v\n got  %s\n want %s", rec.Code, rec.Header(), rec.Body.String(), want)
	}
	if rec.Header().Get("Cache-Control") != "" { // Python の /threads は no-store を付けない
		t.Fatalf("Cache-Control: %v", rec.Header())
	}
	if db.lastPrefix != "user_abcdefgh1234:" { // 利用者 ID は、検証したトークンの sub だけから作る
		t.Fatalf("prefix: %q", db.lastPrefix)
	}
}

func TestThreadsAreNotAvailableWithoutLogin(t *testing.T) {
	// 匿名（AUTH_MODE=off）の会話には持ち主がなく、一覧にすると他人の会話が見える
	rec := do(newTestServer(t, baseConfig(), threadStore(), nil), "GET", "/threads", nil)
	if rec.Code != 404 || rec.Body.String() != `{"detail":"会話の一覧は、ログイン（AUTH_MODE=required）のときだけ使えます。"}` {
		t.Fatalf("%d %s", rec.Code, rec.Body.String())
	}
	s, _, _ := authedServer(t, nil)
	if rec := do(s, "GET", "/threads", nil); rec.Code != 401 || rec.Header().Get("WWW-Authenticate") != "Bearer" {
		t.Fatalf("no token: %d", rec.Code)
	}
}

func TestThreadsCheckMembershipButNotTheDocumentScope(t *testing.T) {
	s, k, db := authedServer(t, func(cfg *config, db *fakeStore) {
		cfg.MembershipMode = modeEnforce
		cfg.AccessMode = modeEnforce
		db.members = map[string]bool{"user_abcdefgh1234": true}
		db.visible = nil // 見えるグループが 0 件でも、会話の一覧は 403 にしない（Python の current_member と同じ）
	})
	rec := do(s, "GET", "/threads", bearer(k.mint(t, algES256, "k1", goodClaims(nil))))
	if rec.Code != 200 || db.visibleCalls != 0 {
		t.Fatalf("%d %s (visible lookups: %d)", rec.Code, rec.Body.String(), db.visibleCalls)
	}
	other := bearer(k.mint(t, algES256, "k1", goodClaims(map[string]any{"sub": "user_zzzzzzzz9999"})))
	rec = do(s, "GET", "/threads", other)
	if rec.Code != 403 || !strings.Contains(rec.Body.String(), membershipRequiredCode) {
		t.Fatalf("non-member: %d %s", rec.Code, rec.Body.String())
	}
}

func TestThreadsDBFailureIsAFixedMessageAndNoDBIsEmpty(t *testing.T) {
	s, k, db := authedServer(t, nil)
	db.err = errBoom
	rec := do(s, "GET", "/threads", bearer(k.mint(t, algES256, "k1", goodClaims(nil))))
	if rec.Code != 503 || rec.Body.String() != `{"detail":"会話履歴を操作できませんでした。しばらく待ってから再試行してください。"}` || strings.Contains(rec.Body.String(), "secret") {
		t.Fatalf("%d %s", rec.Code, rec.Body.String())
	}
}
