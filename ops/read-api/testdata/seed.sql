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
