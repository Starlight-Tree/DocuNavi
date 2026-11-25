-- 読み取り API の検査用の固定データ（使い捨てのローカル DB 専用。本番・開発用の DB には流さない）。
-- 前提: db/migrations/*.sql と db/seeds/classification.sql を適用した、空の DB。
-- Python の API と Go の API に同じ DB を読ませて応答を比べる（compare.py）ときと、Go の統合テスト（postgres_test.go）が使う。
--
--   cafe（登録済み。資料 4 件のうち ready 2）/ guides（登録済み。資料 2 件・ready 2）/ zahard（登録済み。表示名が空白だけ。資料 1 件・failed）/
--   newgroup（未登録。資料 2 件）/ グループの無い資料 1 件
--   default ロール = cafe。staff ロール = cafe・guides・newgroup。empty ロール = 何も無い
--   利用者: user_plain0001（ロール無し = default）/ user_staff0001（staff）/ user_nogroup01（empty）。資格（app_members）は staff と nogroup のみ

INSERT INTO document_collections (name, label, description, sort_order) VALUES
  ('cafe', 'カフェ', '喫茶の資料', 1),
  ('guides', 'ガイド', '', 2),
  ('zahard', '   ', '惑星の資料', 3);

INSERT INTO documents (source, content_hash, status, collection, target, doc_type) VALUES
  ('cafe_a.pdf', 'h1', 'ready', 'cafe', 'minato-coffee', 'store-guide'),
  ('cafe_b.pdf', 'h2', 'ready', 'cafe', 'minato-coffee', 'store-guide'),
  ('cafe_c.pdf', 'h3', 'processing', 'cafe', 'minato-coffee', NULL),
  ('cafe_d.pdf', 'h4', 'failed', 'cafe', NULL, NULL),
  ('guide_a.pdf', 'h5', 'ready', 'guides', 'school', 'regulation'),
  ('guide_b.pdf', 'h6', 'ready', 'guides', 'school', 'manual'),
  ('zahard_a.pdf', 'h7', 'failed', 'zahard', 'zahard', 'setting'),
  ('new_a.pdf', 'h8', 'ready', 'newgroup', 'lune', 'manual'),
  ('new_b.pdf', 'h9', 'processing', 'newgroup', 'lune', 'manual'),
  ('nogroup.pdf', 'h10', 'ready', NULL, 'zahard', 'setting');

INSERT INTO roles (name, label) VALUES ('default', '既定'), ('staff', '職員'), ('empty', '空');
INSERT INTO role_collection_access (role, collection) VALUES
  ('default', 'cafe'), ('staff', 'cafe'), ('staff', 'guides'), ('staff', 'newgroup');

INSERT INTO "user" (id, name, email) VALUES
  ('user_plain0001', '一般', 'plain@example.test'),
  ('user_staff0001', '職員', 'staff@example.test'),
  ('user_nogroup01', 'なし', 'nogroup@example.test');
INSERT INTO user_roles (user_id, role) VALUES ('user_staff0001', 'staff'), ('user_nogroup01', 'empty');
INSERT INTO app_members (user_id) VALUES ('user_staff0001'), ('user_nogroup01');

-- 資料の一覧（/documents）用: 題名・版・ページ数・失敗理由・チャンク・ヒーロー画像・更新時刻。
-- 更新時刻は、ミリ秒の境目に当たらない値（.123456 秒）にそろえる。Python はミリ秒の整数を経由するので、境目だと 1 ms ずれうる。
UPDATE documents SET updated_at = timestamptz '2026-11-20 03:04:05.123456+00' + id * interval '1 second';
UPDATE documents SET updated_at = '2026-11-20 03:04:05+00' WHERE source = 'guide_a.pdf'; -- 秒未満が 0
UPDATE documents SET title = '  カフェ案内  ', pages = 4, revision = ' 初版 ' WHERE source = 'cafe_a.pdf';
UPDATE documents SET title = '   ' WHERE source = 'cafe_b.pdf';
UPDATE documents SET revision = '   ' WHERE source = 'guide_a.pdf';
UPDATE documents SET failure_reason = '読めないページ' WHERE source = 'cafe_d.pdf';
INSERT INTO chunks (document_id, chunk_index, page, kind, content, content_hash, embedding_model)
  SELECT d.id, g, 1, 'text', 'x', 'c' || g, CASE WHEN g < 2 THEN 'm1' END
  FROM documents d, generate_series(0, 2) g WHERE d.source = 'cafe_a.pdf';
INSERT INTO chunks (document_id, chunk_index, page, kind, content, content_hash)
  SELECT d.id, 0, 1, 'text', 'y', 'd0' FROM documents d WHERE d.source = 'guide_a.pdf';
INSERT INTO document_images (document_id, status, mime_type, r2_key, byte_size, prompt_sha256, model)
  SELECT d.id, CASE d.source WHEN 'cafe_a.pdf' THEN 'approved' ELSE 'draft' END, 'image/png', 'k/' || d.source || '.png', 10, repeat('a', 64), 'm'
  FROM documents d WHERE d.source IN ('cafe_a.pdf', 'cafe_b.pdf');

-- 資料 1 件（/documents/{id}/chunks）用: ページの範囲・ラベル・見出し・本文に記号と日本語。
UPDATE chunks SET page_end = 2, page_label = '表紙', page_end_label = 'i', heading = '見出し <b>&' WHERE chunk_index = 1 AND document_id = (SELECT id FROM documents WHERE source = 'cafe_a.pdf');
UPDATE chunks SET content = E'本文 <b>&</b>\n二行目 "引用" \\ バックスラッシュ' WHERE chunk_index = 0 AND document_id = (SELECT id FROM documents WHERE source = 'cafe_a.pdf');
-- PDF の確認を取り込み側が記録した資料（/chunks は置き場を見ずに original_pdf = true）
UPDATE documents SET original_verified_at = now() WHERE source = 'guide_a.pdf';

-- 会話の一覧（/threads）用: 利用者ごとの会話。題名は metadata の title（無い行は「（題名なし）」）。時刻はミリ秒の境目を避ける（.123456）。
-- チェックポイント本体（bytea）は、Go が読まないので、中身は何でもよい。
INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata, created_at) VALUES
  ('user_staff0001:t-old',  '', '1ef-0001', 'msgpack', '\x00', '{"title": "古い会話の題名"}', '2026-11-20 01:00:00.123456+00'),
  ('user_staff0001:t-old',  '', '1ef-0002', 'msgpack', '\x00', '{"title": "古い会話の題名"}', '2026-11-20 01:05:00.123456+00'),
  ('user_staff0001:t-new',  '', '1ef-0003', 'msgpack', '\x00', '{"title": "新しい会話 <b>&"}', '2026-11-21 02:00:00.123456+00'),
  ('user_staff0001:t-nul',  '', '1ef-0004', 'msgpack', '\x00', '{}', '2026-11-19 03:00:00.123456+00'),
  ('user_staff0001:t-sub',  'sub', '1ef-0005', 'msgpack', '\x00', '{"title": "サブグラフ（一覧に出ない）"}', '2026-11-22 03:00:00.123456+00'),
  ('user_plain0001:t-mine', '', '1ef-0006', 'msgpack', '\x00', '{"title": "別の人の会話"}', '2026-11-21 05:00:00+00'),
  ('user_staff0001x:t-x',   '', '1ef-0007', 'msgpack', '\x00', '{"title": "接頭辞が似た別の利用者"}', '2026-11-21 06:00:00.123456+00');
