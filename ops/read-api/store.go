package main

// DB（Postgres）への読み取り。接続は pgx。アプリは本番で PgBouncer（トランザクションモード）越しにつなぐので、
// サーバー側の準備済み文を使わない QueryExecModeExec にする（Python の prepare_threshold=None と同じ理由）。
// SQL は app/api/collections.py・classifications.py・access.py を Postgres の書き方に直したもの（json_each の代わりに text[] と ANY）。

import (
	"context"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type collectionRow struct {
	Name          string
	Label         *string
	Description   *string
	SortOrder     *int32
	Registered    bool
	DocumentCount int64
	ReadyCount    int64
}

type classificationRow struct {
	Name          string
	Label         string
	Description   string
	SortOrder     int32
	DocumentCount int64
	ReadyCount    int64
}

// store は DB の読み取りの口。テストでは偽物に差し替える。allowed が nil なら全グループ。
type store interface {
	collections(ctx context.Context, allowed []string, limit int) ([]collectionRow, error)
	classifications(ctx context.Context, kind classificationKind, allowed []string, limit int) ([]classificationRow, error)
	visibleCollections(ctx context.Context, userID *string) ([]string, error)
	isMember(ctx context.Context, userID string) (bool, error)
	probeAccess(ctx context.Context) (defaultRoles, defaultCollections, assignments int64, err error)
	memberCount(ctx context.Context) (int64, error)
	ping(ctx context.Context) error
	close()
}

type classificationKind int

const (
	kindTargets classificationKind = iota
	kindDocTypes
)

const collectionsSQL = `SELECT coalesce(c.name, d.collection) AS name, c.label AS label, c.description AS description,
  c.sort_order AS sort_order, (c.name IS NOT NULL) AS registered,
  count(d.id) AS document_count, count(d.id) FILTER (WHERE d.status = 'ready') AS ready_count
FROM document_collections c FULL OUTER JOIN documents d ON d.collection = c.name
WHERE coalesce(c.name, d.collection) IS NOT NULL
  AND ($1::text[] IS NULL OR coalesce(c.name, d.collection) = ANY($1::text[]))
GROUP BY c.name, c.label, c.description, c.sort_order, d.collection
ORDER BY c.sort_order NULLS LAST, coalesce(c.name, d.collection) LIMIT $2`

// 内側結合: 見える資料が 1 件も無い分類は、行ごと出ない。件数は、見える資料だけ。表と列の名前は定数（利用者の入力ではない）
func classificationsSQL(kind classificationKind) string {
	vocabulary, column := "document_targets", "target"
	if kind == kindDocTypes {
		vocabulary, column = "document_types", "doc_type"
	}
	return fmt.Sprintf(`SELECT v.name AS name, v.label AS label, v.description AS description, v.sort_order AS sort_order,
  count(d.id) AS document_count, count(d.id) FILTER (WHERE d.status = 'ready') AS ready_count
FROM %s v JOIN documents d ON d.%s = v.name
WHERE ($1::text[] IS NULL OR d.collection = ANY($1::text[]))
GROUP BY v.name, v.label, v.description, v.sort_order
ORDER BY v.sort_order, v.name LIMIT $2`, vocabulary, column)
}

var (
	targetsSQL = classificationsSQL(kindTargets)
	typesSQL   = classificationsSQL(kindDocTypes)
)

// 利用者が見えるグループ。user_roles が 1 件も無い利用者（user_id が NULL の匿名も）は default ロール
const visibleSQL = `SELECT DISTINCT a.collection AS collection FROM role_collection_access a
WHERE a.role IN (SELECT r.role FROM user_roles r WHERE r.user_id = $1::text)
   OR (a.role = $2::text AND NOT EXISTS (SELECT 1 FROM user_roles r WHERE r.user_id = $1::text))`

const (
	memberSQL = `SELECT 1 FROM app_members WHERE user_id = $1::text`
	probeSQL  = `SELECT (SELECT count(*) FROM roles WHERE name = $1::text) AS default_roles,
  (SELECT count(*) FROM role_collection_access WHERE role = $1::text) AS default_collections,
  (SELECT count(*) FROM user_roles) AS assignments`
	memberCountSQL = `SELECT count(*) FROM app_members`
)

type pgStore struct{ pool *pgxpool.Pool }

// openStore は接続プールを作る。maxConns は ADR 0011 の接続枠（台数 × この値）に収める。
func openStore(ctx context.Context, databaseURL string, maxConns int) (*pgStore, error) {
	cfg, err := pgxpool.ParseConfig(databaseURL)
	if err != nil {
		return nil, fmt.Errorf("DATABASE_URL を読めません (%T)", err) // 例外の文字列には接続先が入りうる。型だけ
	}
	cfg.ConnConfig.DefaultQueryExecMode = pgx.QueryExecModeExec
	cfg.ConnConfig.ConnectTimeout = 15 * time.Second
	cfg.MaxConns = int32(maxConns)
	cfg.MinConns = 1 // 1 本は温めておく（アイドルの後の最初の要求で、接続の確立を待たない）
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("DB の接続プールを作れませんでした (%T)", err)
	}
	return &pgStore{pool: pool}, nil
}

func (s *pgStore) ping(ctx context.Context) error { return s.pool.Ping(ctx) }
func (s *pgStore) close()                         { s.pool.Close() }

func (s *pgStore) collections(ctx context.Context, allowed []string, limit int) ([]collectionRow, error) {
	rows, err := s.pool.Query(ctx, collectionsSQL, allowedArg(allowed), limit)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (collectionRow, error) {
		var r collectionRow
		err := row.Scan(&r.Name, &r.Label, &r.Description, &r.SortOrder, &r.Registered, &r.DocumentCount, &r.ReadyCount)
		return r, err
	})
}

func (s *pgStore) classifications(ctx context.Context, kind classificationKind, allowed []string, limit int) ([]classificationRow, error) {
	sql := targetsSQL
	if kind == kindDocTypes {
		sql = typesSQL
	}
	rows, err := s.pool.Query(ctx, sql, allowedArg(allowed), limit)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (classificationRow, error) {
		var r classificationRow
		err := row.Scan(&r.Name, &r.Label, &r.Description, &r.SortOrder, &r.DocumentCount, &r.ReadyCount)
		return r, err
	})
}

func (s *pgStore) visibleCollections(ctx context.Context, userID *string) ([]string, error) {
	rows, err := s.pool.Query(ctx, visibleSQL, userID, defaultRole)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, pgx.RowTo[string])
}

func (s *pgStore) isMember(ctx context.Context, userID string) (bool, error) {
	rows, err := s.pool.Query(ctx, memberSQL, userID)
	if err != nil {
		return false, err
	}
	found, err := pgx.CollectRows(rows, pgx.RowTo[int32])
	return len(found) > 0, err
}

func (s *pgStore) probeAccess(ctx context.Context) (int64, int64, int64, error) {
	var a, b, c int64
	err := s.pool.QueryRow(ctx, probeSQL, defaultRole).Scan(&a, &b, &c)
	return a, b, c, err
}

func (s *pgStore) memberCount(ctx context.Context) (int64, error) {
	var n int64
	err := s.pool.QueryRow(ctx, memberCountSQL).Scan(&n)
	return n, err
}

// allowedArg は SQL の $1。nil は全グループ（SQL の NULL）、空でも nil でない slice は何も見えない。
func allowedArg(allowed []string) any {
	if allowed == nil {
		return nil
	}
	return allowed
}
