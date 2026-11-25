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
