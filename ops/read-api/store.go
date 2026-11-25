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

// documentRow は資料の一覧の 1 行（app/api/documents.py の _LIST_SQL）。
type documentRow struct {
	ID            int64
	Source        string
	Title         *string
	Collection    *string
	Target        *string
	DocType       *string
	Revision      *string
	Status        string
	FailureReason *string
	Pages         *int32
	UpdatedAt     *time.Time
	ChunkCount    int64
	EmbeddedCount int64
	HeroImage     bool
}

// documentDetailRow は 1 件の資料（app/api/documents.py の _DOCUMENT_SQL）。content_hash と、取り込み側が記録した PDF の確認（original_verified_at）つき。
type documentDetailRow struct {
	documentRow
	ContentHash      *string
	OriginalVerified bool
}

// chunkRow は資料の本文の 1 チャンク（_CHUNKS_SQL）。
type chunkRow struct {
	ChunkIndex     int64
	Page           *int32
	PageEnd        *int32
	Kind           *string
	PageLabel      *string
	PageEndLabel   *string
	Heading        *string
	Content        string
	Embedded       bool
	EmbeddingModel *string
}

// heroRow は承認済みのヒーロー画像（_HERO_SQL）。
type heroRow struct {
	Collection *string
	MimeType   *string
	R2Key      *string
}

// threadRow は会話の一覧の 1 行。Title が nil なのは、保存時の題名（checkpoints.metadata の title）が無い会話（この機能より前に保存した行）。
type threadRow struct {
	ThreadID  string
	CreatedAt *time.Time
	UpdatedAt *time.Time
	Title     *string
}

// store は DB の読み取りの口。テストでは偽物に差し替える。allowed が nil なら全グループ。
type store interface {
	collections(ctx context.Context, allowed []string, limit int) ([]collectionRow, error)
	classifications(ctx context.Context, kind classificationKind, allowed []string, limit int) ([]classificationRow, error)
	documents(ctx context.Context, allowed []string, limit int) ([]documentRow, error)
	// 資料 1 件と、その本文（資料が無ければ nil）。
	documentWithChunks(ctx context.Context, id int64, chunkLimit int) (*documentDetailRow, []chunkRow, error)
	document(ctx context.Context, id int64) (*documentDetailRow, error)
	hero(ctx context.Context, id int64) (*heroRow, error)
	threads(ctx context.Context, prefix string, limit int) ([]threadRow, error)
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

// 資料の一覧。チャンクの数と、埋め込み済みの数（embedding_model がある行）、承認済みのヒーロー画像の有無つき
const documentsSQL = `SELECT d.id AS id, d.source AS source, d.title AS title, d.collection AS collection,
  d.target AS target, d.doc_type AS doc_type, d.revision AS revision, d.status AS status,
  d.failure_reason AS failure_reason, d.pages AS pages, d.updated_at AS updated_at,
  count(c.id) AS chunk_count, count(c.embedding_model) AS embedded_count,
  EXISTS (SELECT 1 FROM document_images i WHERE i.document_id = d.id AND i.kind = 'hero' AND i.status = 'approved') AS hero_image
FROM documents d LEFT JOIN chunks c ON c.document_id = d.id
WHERE ($1::text[] IS NULL OR d.collection = ANY($1::text[]))
GROUP BY d.id ORDER BY d.source, d.id LIMIT $2`

// 1 件の資料（original_verified_at が NULL でないか＝取り込み側が PDF を確かめて記録済みか、も一緒に）
const documentSQL = `SELECT d.id AS id, d.source AS source, d.title AS title, d.collection AS collection,
  d.target AS target, d.doc_type AS doc_type, d.revision AS revision, d.status AS status,
  d.failure_reason AS failure_reason, d.pages AS pages, d.updated_at AS updated_at,
  d.content_hash AS content_hash, d.original_verified_at IS NOT NULL AS original_verified
FROM documents d WHERE d.id = $1::bigint`

const chunksSQL = `SELECT c.chunk_index AS chunk_index, c.page AS page, c.page_end AS page_end, c.kind AS kind,
  c.page_label AS page_label, c.page_end_label AS page_end_label,
  c.heading AS heading, c.content AS content, c.embedding_model IS NOT NULL AS embedded,
  c.embedding_model AS embedding_model
FROM chunks c WHERE c.document_id = $1::bigint ORDER BY c.chunk_index LIMIT $2`

// 承認済みのヒーロー画像。資料と同じ行から引く（資料の可視性の判定を、PDF の配信と同じ行に対して行うため）
const heroSQL = `SELECT d.collection AS collection, i.mime_type AS mime_type, i.r2_key AS r2_key
FROM documents d JOIN document_images i ON i.document_id = d.id
WHERE d.id = $1::bigint AND i.kind = 'hero' AND i.status = 'approved'`

// 会話の一覧（app/api/threads.py の _LIST_SQL）。会話ごとの最初と最後のチェックポイントの時刻と、題名（保存時に metadata の title へ入れた値）を 1 回で取る。
// チェックポイント本体（msgpack）は取らない。Go は復号しない（題名が無い行は、題名なしとして返す。app.history_admin の題名の補完を先に流す）。
// 最新 = checkpoint_id の最大（主キーなので 1 行に決まる）。外側の ORDER BY は必須（CTE の並びは JOIN のあとに保証されない）。
// prefix は `<利用者 ID>:`（利用者 ID は [A-Za-z0-9_-] だけ）。LIKE のワイルドカードを含まないので、先頭一致に使える
const threadsSQL = `WITH recent AS (
  SELECT thread_id, min(created_at) AS created_at, max(created_at) AS updated_at, max(checkpoint_id) AS latest_id
  FROM checkpoints WHERE substr(thread_id, 1, length($1::text)) = $1::text AND checkpoint_ns = ''
  GROUP BY thread_id ORDER BY max(created_at) DESC, thread_id LIMIT $2)
SELECT r.thread_id AS thread_id, r.created_at AS created_at, r.updated_at AS updated_at, c.metadata->>'title' AS title
FROM recent r JOIN checkpoints c
  ON c.thread_id = r.thread_id AND c.checkpoint_ns = '' AND c.checkpoint_id = r.latest_id
ORDER BY r.updated_at DESC, r.thread_id`

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

func (s *pgStore) documents(ctx context.Context, allowed []string, limit int) ([]documentRow, error) {
	rows, err := s.pool.Query(ctx, documentsSQL, allowedArg(allowed), limit)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (documentRow, error) {
		var r documentRow
		err := row.Scan(&r.ID, &r.Source, &r.Title, &r.Collection, &r.Target, &r.DocType, &r.Revision, &r.Status,
			&r.FailureReason, &r.Pages, &r.UpdatedAt, &r.ChunkCount, &r.EmbeddedCount, &r.HeroImage)
		return r, err
	})
}

func scanDocumentDetail(row pgx.CollectableRow) (documentDetailRow, error) {
	var r documentDetailRow
	err := row.Scan(&r.ID, &r.Source, &r.Title, &r.Collection, &r.Target, &r.DocType, &r.Revision, &r.Status,
		&r.FailureReason, &r.Pages, &r.UpdatedAt, &r.ContentHash, &r.OriginalVerified)
	return r, err
}

func (s *pgStore) document(ctx context.Context, id int64) (*documentDetailRow, error) {
	rows, err := s.pool.Query(ctx, documentSQL, id)
	if err != nil {
		return nil, err
	}
	found, err := pgx.CollectRows(rows, scanDocumentDetail)
	if err != nil || len(found) == 0 {
		return nil, err
	}
	return &found[0], nil
}

// documentWithChunks は、資料 1 件と本文。資料が無ければ nil（本文は引かない）。同じ接続で 2 本を続けて流す（Python と同じ）。
func (s *pgStore) documentWithChunks(ctx context.Context, id int64, chunkLimit int) (*documentDetailRow, []chunkRow, error) {
	conn, err := s.pool.Acquire(ctx)
	if err != nil {
		return nil, nil, err
	}
	defer conn.Release()
	rows, err := conn.Query(ctx, documentSQL, id)
	if err != nil {
		return nil, nil, err
	}
	found, err := pgx.CollectRows(rows, scanDocumentDetail)
	if err != nil || len(found) == 0 {
		return nil, nil, err
	}
	rows, err = conn.Query(ctx, chunksSQL, id, chunkLimit)
	if err != nil {
		return nil, nil, err
	}
	chunks, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (chunkRow, error) {
		var c chunkRow
		err := row.Scan(&c.ChunkIndex, &c.Page, &c.PageEnd, &c.Kind, &c.PageLabel, &c.PageEndLabel, &c.Heading, &c.Content,
			&c.Embedded, &c.EmbeddingModel)
		return c, err
	})
	if err != nil {
		return nil, nil, err
	}
	return &found[0], chunks, nil
}

func (s *pgStore) hero(ctx context.Context, id int64) (*heroRow, error) {
	rows, err := s.pool.Query(ctx, heroSQL, id)
	if err != nil {
		return nil, err
	}
	found, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (heroRow, error) {
		var h heroRow
		err := row.Scan(&h.Collection, &h.MimeType, &h.R2Key)
		return h, err
	})
	if err != nil || len(found) == 0 {
		return nil, err
	}
	return &found[0], nil
}

func (s *pgStore) threads(ctx context.Context, prefix string, limit int) ([]threadRow, error) {
	rows, err := s.pool.Query(ctx, threadsSQL, prefix, limit)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (threadRow, error) {
		var t threadRow
		err := row.Scan(&t.ThreadID, &t.CreatedAt, &t.UpdatedAt, &t.Title)
		return t, err
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
