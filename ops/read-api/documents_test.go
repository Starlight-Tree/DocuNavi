package main

import (
	"strings"
	"testing"
	"time"
)

func docRows() []documentRow {
	t0 := time.Date(2026, 11, 20, 3, 4, 5, 0, time.UTC)
	t1 := time.Date(2026, 11, 20, 3, 4, 5, 123_999_000, time.UTC) // ミリ秒未満は切り捨て（Python は UNIX ミリ秒の整数を経由する）
	pages := int32(7)
	return []documentRow{
		{ID: 1, Source: "a.pdf", Title: str("  題名  "), Collection: str("cafe"), Target: str("minato-coffee"), DocType: str("store-guide"),
			Revision: str(" 初版 "), Status: "ready", Pages: &pages, UpdatedAt: &t1, ChunkCount: 3, EmbeddedCount: 2, HeroImage: true},
		{ID: 2, Source: "b.pdf", Title: str("   "), Collection: str("bad name"), Target: str("Bad_Target"), DocType: str("-x"),
			Revision: str(strings.Repeat("版", 41)), Status: "failed", FailureReason: str("読めない"), UpdatedAt: &t0},
		{ID: 3, Source: "c.pdf", Status: "processing"},
	}
}

func TestDocumentsMatchThePythonShape(t *testing.T) {
	db := sampleStore()
	db.documentRows = docRows()
	rec := do(newTestServer(t, baseConfig(), db, nil), "GET", "/documents", nil)
	want := `{"configured":true,"documents":[` +
		`{"id":1,"source":"a.pdf","title":"題名","collection":"cafe","target":"minato-coffee","doc_type":"store-guide","revision":"初版","status":"ready","failure_reason":null,"pages":7,"updated_at":"2026-11-20T03:04:05.123000Z","chunk_count":3,"embedded_count":2,"hero_image":true},` +
		`{"id":2,"source":"b.pdf","title":null,"collection":null,"target":null,"doc_type":null,"revision":null,"status":"failed","failure_reason":"読めない","pages":null,"updated_at":"2026-11-20T03:04:05Z","chunk_count":0,"embedded_count":0,"hero_image":false},` +
		`{"id":3,"source":"c.pdf","title":null,"collection":null,"target":null,"doc_type":null,"revision":null,"status":"processing","failure_reason":null,"pages":null,"updated_at":null,"chunk_count":0,"embedded_count":0,"hero_image":false}]}`
	if rec.Code != 200 || rec.Body.String() != want || rec.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("%d %v\n got  %s\n want %s", rec.Code, rec.Header(), rec.Body.String(), want)
	}
}

func TestDocumentsWithoutDBAndOnFailure(t *testing.T) {
	if rec := do(newTestServer(t, baseConfig(), nil, nil), "GET", "/documents", nil); rec.Body.String() != `{"configured":false,"documents":[]}` {
		t.Fatalf("no DB: %d %s", rec.Code, rec.Body.String())
	}
	db := sampleStore()
	db.err = errBoom
	rec := do(newTestServer(t, baseConfig(), db, nil), "GET", "/documents", nil)
	// Python と同じく、この 503 には Cache-Control を付けない（HTTPException には、引数の Response のヘッダーが載らない）
	if rec.Code != 503 || rec.Body.String() != `{"detail":"`+detailDocumentsUnavailable+`"}` || rec.Header().Get("Cache-Control") != "" || strings.Contains(rec.Body.String(), "secret") {
		t.Fatalf("%d %s %v", rec.Code, rec.Body.String(), rec.Header())
	}
}

func TestDocumentsEnforceHidesOtherGroupsAndUngroupedDocuments(t *testing.T) {
	cfg := baseConfig()
	cfg.AccessMode = modeEnforce
	db := sampleStore()
	db.visible = []string{"cafe"}
	rows := docRows()
	rows[1].Collection = str("guides") // 範囲の外
	db.documentRows = rows
	s := newTestServer(t, cfg, db, nil)
	rec := do(s, "GET", "/documents", nil)
	body := rec.Body.String()
	if rec.Code != 200 || !strings.Contains(body, `"a.pdf"`) || strings.Contains(body, `"b.pdf"`) || strings.Contains(body, `"c.pdf"`) {
		t.Fatalf("out-of-scope or ungrouped documents were returned: %d %s", rec.Code, body)
	}
	if strings.Join(db.lastAllowed, ",") != "cafe" {
		t.Fatalf("the SQL was not narrowed: %v", db.lastAllowed)
	}
	// 全グループが見えるときだけ、グループの無い資料も出る
	cfg.AccessMode = modeOff
	db2 := sampleStore()
	db2.documentRows = docRows()
	if body := do(newTestServer(t, cfg, db2, nil), "GET", "/documents", nil).Body.String(); !strings.Contains(body, `"c.pdf"`) {
		t.Fatalf("an ungrouped document must be visible to the all scope: %s", body)
	}
}

func TestDocumentSubPathsAreNotServedHere(t *testing.T) {
	s := newTestServer(t, baseConfig(), sampleStore(), nil)
	for _, path := range []string{"/documents/1/chunks", "/documents/1/file", "/documents/1/hero", "/documents/"} {
		if rec := do(s, "GET", path, nil); rec.Code != 404 {
			t.Fatalf("%s: %d", path, rec.Code)
		}
	}
}

func TestRevisionAndTitleNormalisation(t *testing.T) {
	if got := revisionOf(str(strings.Repeat("版", 40))); got == nil {
		t.Fatal("40 characters is allowed")
	}
	if revisionOf(str(strings.Repeat("版", 41))) != nil || revisionOf(str(" ")) != nil || revisionOf(nil) != nil {
		t.Fatal("too long / blank / missing must be nil")
	}
	if titleOf(str(" \t ")) != nil || *titleOf(str(" x ")) != "x" {
		t.Fatal("title trimming")
	}
}
