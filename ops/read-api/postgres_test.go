package main

// 実 Postgres を使う検査。READ_API_TEST_DATABASE_URL が無ければ飛ばす（CI の go ジョブには DB が無い）。
// DB は testdata/setup_db.py で作った使い捨てのもの（testdata/seed.sql の固定データ）。読むだけで、書かない。
//   READ_API_TEST_DATABASE_URL=postgresql://…/readapi_cmp go test -count=1 -run Postgres ./...

import (
	"context"
	"os"
	"reflect"
	"sort"
	"testing"
	"time"
)

func openTestStore(t *testing.T) *pgStore {
	t.Helper()
	url := os.Getenv("READ_API_TEST_DATABASE_URL")
	if url == "" {
		t.Skip("READ_API_TEST_DATABASE_URL が無いので、実 DB の検査は飛ばします")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	db, err := openStore(ctx, url, 2)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.close)
	if err := db.ping(ctx); err != nil {
		t.Fatalf("ping: %v", err)
	}
	return db
}

func TestPostgresCollections(t *testing.T) {
	db := openTestStore(t)
	ctx := context.Background()
	rows, err := db.collections(ctx, nil, maxListItems)
	if err != nil {
		t.Fatal(err)
	}
	// 登録済みは sort_order の順、未登録は後ろに名前順（NULLS LAST）。グループの無い資料（collection が NULL）は出ない
	var names []string
	for _, r := range rows {
		names = append(names, r.Name)
	}
	if want := []string{"cafe", "guides", "zahard", "newgroup"}; !reflect.DeepEqual(names, want) {
		t.Fatalf("names=%v want %v", names, want)
	}
	if cafe := rows[0]; cafe.DocumentCount != 4 || cafe.ReadyCount != 2 || !cafe.Registered || cafe.SortOrder == nil || *cafe.SortOrder != 1 {
		t.Fatalf("cafe=%+v", cafe)
	}
	if nw := rows[3]; nw.Registered || nw.Label != nil || nw.SortOrder != nil || nw.DocumentCount != 2 || nw.ReadyCount != 1 {
		t.Fatalf("newgroup=%+v", nw)
	}
	// 範囲を絞ると、その中だけ。空の範囲（nil でない空の slice）は何も返さない（漏れる方向には倒れない）
	rows, err = db.collections(ctx, []string{"guides", "newgroup"}, maxListItems)
	if err != nil || len(rows) != 2 || rows[0].Name != "guides" || rows[1].Name != "newgroup" {
		t.Fatalf("narrowed: %+v %v", rows, err)
	}
	if rows, err = db.collections(ctx, []string{}, maxListItems); err != nil || len(rows) != 0 {
		t.Fatalf("an empty scope must return nothing: %+v %v", rows, err)
	}
	if rows, err = db.collections(ctx, nil, 1); err != nil || len(rows) != 1 {
		t.Fatalf("limit: %+v %v", rows, err)
	}
}

func TestPostgresClassificationsCountOnlyVisibleDocuments(t *testing.T) {
	db := openTestStore(t)
	ctx := context.Background()
	all, err := db.classifications(ctx, kindTargets, nil, maxListItems)
	if err != nil {
		t.Fatal(err)
	}
	counts := map[string][2]int64{}
	for _, r := range all {
		counts[r.Name] = [2]int64{r.DocumentCount, r.ReadyCount}
	}
	// 見える資料が 1 件も無い分類は、行ごと出ない（minato-coffee・school・zahard・lune は資料がある）
	want := map[string][2]int64{"minato-coffee": {3, 2}, "school": {2, 2}, "zahard": {2, 1}, "lune": {2, 1}}
	if !reflect.DeepEqual(counts, want) {
		t.Fatalf("targets=%v want %v", counts, want)
	}
	// cafe だけ見える範囲: school・zahard（グループが guides・zahard・NULL の資料だけ）・lune は出ない
	narrow, err := db.classifications(ctx, kindTargets, []string{"cafe"}, maxListItems)
	if err != nil || len(narrow) != 1 || narrow[0].Name != "minato-coffee" || narrow[0].DocumentCount != 3 {
		t.Fatalf("narrow: %+v %v", narrow, err)
	}
	types, err := db.classifications(ctx, kindDocTypes, []string{"guides"}, maxListItems)
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, r := range types {
		got = append(got, r.Name)
	}
	sort.Strings(got)
	if !reflect.DeepEqual(got, []string{"manual", "regulation"}) {
		t.Fatalf("doc types in guides: %v", got)
	}
	if none, err := db.classifications(ctx, kindDocTypes, []string{}, maxListItems); err != nil || len(none) != 0 {
		t.Fatalf("an empty scope must return nothing: %+v %v", none, err)
	}
}

func TestPostgresDocuments(t *testing.T) {
	db := openTestStore(t)
	ctx := context.Background()
	rows, err := db.documents(ctx, nil, maxDocuments)
	if err != nil {
		t.Fatal(err)
	}
	var sources []string
	for _, r := range rows {
		sources = append(sources, r.Source)
	}
	// source の名前順。グループの無い資料も、全グループの範囲なら出る
	want := []string{"cafe_a.pdf", "cafe_b.pdf", "cafe_c.pdf", "cafe_d.pdf", "guide_a.pdf", "guide_b.pdf", "new_a.pdf", "new_b.pdf", "nogroup.pdf", "zahard_a.pdf"}
	if !reflect.DeepEqual(sources, want) {
		t.Fatalf("sources=%v", sources)
	}
	a := rows[0]
	if a.ChunkCount != 3 || a.EmbeddedCount != 2 || !a.HeroImage || a.Pages == nil || *a.Pages != 4 || a.UpdatedAt == nil {
		t.Fatalf("cafe_a=%+v", a)
	}
	if b := rows[1]; b.HeroImage { // 下書きの画像は、承認済みではない
		t.Fatalf("a draft image counted as a hero: %+v", b)
	}
	if d := rows[3]; d.FailureReason == nil || d.ChunkCount != 0 || d.EmbeddedCount != 0 {
		t.Fatalf("cafe_d=%+v", d)
	}
	narrow, err := db.documents(ctx, []string{"guides"}, maxDocuments)
	if err != nil || len(narrow) != 2 || narrow[0].Source != "guide_a.pdf" {
		t.Fatalf("narrowed: %+v %v", narrow, err)
	}
	if none, err := db.documents(ctx, []string{}, maxDocuments); err != nil || len(none) != 0 {
		t.Fatalf("an empty scope must return nothing: %+v %v", none, err)
	}
	if one, err := db.documents(ctx, nil, 1); err != nil || len(one) != 1 {
		t.Fatalf("limit: %+v %v", one, err)
	}
}

func TestPostgresAccessQueries(t *testing.T) {
	db := openTestStore(t)
	ctx := context.Background()
	visible := func(user *string) []string {
		got, err := db.visibleCollections(ctx, user)
		if err != nil {
			t.Fatal(err)
		}
		sort.Strings(got)
		return got
	}
	staff, plain, empty := "user_staff0001", "user_plain0001", "user_nogroup01"
	if got := visible(&staff); !reflect.DeepEqual(got, []string{"cafe", "guides", "newgroup"}) {
		t.Fatalf("staff: %v", got)
	}
	// ロールが 1 つも無い利用者と、匿名（NULL）は default ロール
	if got := visible(&plain); !reflect.DeepEqual(got, []string{"cafe"}) {
		t.Fatalf("plain: %v", got)
	}
	if got := visible(nil); !reflect.DeepEqual(got, []string{"cafe"}) {
		t.Fatalf("anonymous: %v", got)
	}
	// ロールはあるが、グループが無い利用者は何も見えない（default には倒れない）
	if got := visible(&empty); len(got) != 0 {
		t.Fatalf("empty role: %v", got)
	}
	for user, want := range map[string]bool{staff: true, empty: true, plain: false, "user_unknown0001": false} {
		if got, err := db.isMember(ctx, user); err != nil || got != want {
			t.Fatalf("isMember(%s)=%v err=%v want %v", user, got, err, want)
		}
	}
	roles, groups, assignments, err := db.probeAccess(ctx)
	if err != nil || roles != 1 || groups != 1 || assignments != 2 {
		t.Fatalf("probe: %d %d %d %v", roles, groups, assignments, err)
	}
	if n, err := db.memberCount(ctx); err != nil || n != 2 {
		t.Fatalf("memberCount: %d %v", n, err)
	}
}

func documentIDBySource(t *testing.T, db *pgStore, source string) int64 {
	t.Helper()
	rows, err := db.documents(context.Background(), nil, maxDocuments)
	if err != nil {
		t.Fatal(err)
	}
	for _, r := range rows {
		if r.Source == source {
			return r.ID
		}
	}
	t.Fatalf("%s is not in the seed", source)
	return 0
}

func TestPostgresDocumentDetailAndChunks(t *testing.T) {
	db := openTestStore(t)
	ctx := context.Background()
	id := documentIDBySource(t, db, "cafe_a.pdf")
	row, chunks, err := db.documentWithChunks(ctx, id, maxChunks)
	if err != nil || row == nil {
		t.Fatalf("%+v %v", row, err)
	}
	if row.Source != "cafe_a.pdf" || row.ContentHash == nil || *row.ContentHash != "h1" || row.OriginalVerified || row.Pages == nil || *row.Pages != 4 {
		t.Fatalf("row=%+v", row)
	}
	if len(chunks) != 3 || chunks[0].ChunkIndex != 0 || chunks[2].ChunkIndex != 2 {
		t.Fatalf("chunks=%+v", chunks)
	}
	if c := chunks[1]; c.PageEnd == nil || *c.PageEnd != 2 || c.PageLabel == nil || *c.PageLabel != "表紙" || c.Heading == nil || !c.Embedded || c.EmbeddingModel == nil {
		t.Fatalf("chunk 1: %+v", c)
	}
	if chunks[2].Embedded || chunks[2].EmbeddingModel != nil {
		t.Fatalf("chunk 2 has no embedding: %+v", chunks[2])
	}
	// 無い資料は nil（本文は引かない）。上限は LIMIT に効く
	if row, chunks, err := db.documentWithChunks(ctx, 999999, maxChunks); err != nil || row != nil || chunks != nil {
		t.Fatalf("missing: %+v %v %v", row, chunks, err)
	}
	if _, chunks, err := db.documentWithChunks(ctx, id, 2); err != nil || len(chunks) != 2 {
		t.Fatalf("limit: %d %v", len(chunks), err)
	}
	// 取り込み側が PDF の確認を記録した資料
	if verified, err := db.document(ctx, documentIDBySource(t, db, "guide_a.pdf")); err != nil || verified == nil || !verified.OriginalVerified {
		t.Fatalf("verified: %+v %v", verified, err)
	}
}

func TestPostgresHero(t *testing.T) {
	db := openTestStore(t)
	ctx := context.Background()
	hero, err := db.hero(ctx, documentIDBySource(t, db, "cafe_a.pdf"))
	if err != nil || hero == nil || hero.MimeType == nil || *hero.MimeType != "image/png" || hero.R2Key == nil || *hero.R2Key != "k/cafe_a.pdf.png" || hero.Collection == nil || *hero.Collection != "cafe" {
		t.Fatalf("%+v %v", hero, err)
	}
	if none, err := db.hero(ctx, documentIDBySource(t, db, "cafe_b.pdf")); err != nil || none != nil { // 承認前（draft）の画像は返さない
		t.Fatalf("draft: %+v %v", none, err)
	}
}

func TestPostgresThreads(t *testing.T) {
	db := openTestStore(t)
	ctx := context.Background()
	rows, err := db.threads(ctx, "user_staff0001:", maxThreads)
	if err != nil {
		t.Fatal(err)
	}
	// 新しい順（最後のチェックポイントの時刻）。サブグラフ（checkpoint_ns が空でない）と、接頭辞が似た別の利用者は出ない
	var ids []string
	for _, r := range rows {
		ids = append(ids, r.ThreadID)
	}
	if want := []string{"user_staff0001:t-new", "user_staff0001:t-old", "user_staff0001:t-nul"}; !reflect.DeepEqual(ids, want) {
		t.Fatalf("ids=%v want %v", ids, want)
	}
	if rows[0].Title == nil || *rows[0].Title != "新しい会話 <b>&" || rows[2].Title != nil {
		t.Fatalf("titles: %v / %v", rows[0].Title, rows[2].Title)
	}
	// 最初と最後のチェックポイントの時刻（t-old は 2 つ）
	if old := rows[1]; old.CreatedAt == nil || old.UpdatedAt == nil || !old.CreatedAt.Before(*old.UpdatedAt) {
		t.Fatalf("t-old times: %+v", old)
	}
	if rows, err := db.threads(ctx, "user_staff0001:", 1); err != nil || len(rows) != 1 {
		t.Fatalf("limit: %d %v", len(rows), err)
	}
	if rows, err := db.threads(ctx, "user_nobody0001:", maxThreads); err != nil || len(rows) != 0 {
		t.Fatalf("other user: %d %v", len(rows), err)
	}
	// 接頭辞の LIKE のワイルドカード（% _）は、ただの文字として扱う
	if rows, err := db.threads(ctx, "user_%:", maxThreads); err != nil || len(rows) != 0 {
		t.Fatalf("wildcards: %d %v", len(rows), err)
	}
}
