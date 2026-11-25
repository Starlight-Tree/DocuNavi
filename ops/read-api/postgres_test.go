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
