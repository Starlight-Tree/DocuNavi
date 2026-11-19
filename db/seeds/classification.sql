-- ローカル DB 用の種データ: 対象（target）・資料種別（doc_type）の語彙と、初期 6 資料の分類。
-- 新しい DB に db/migrations/0001_schema.sql を適用したあと、取り込み（python -m app.ingestion）を終えてから、手で 1 回流す。
--   psql "$DATABASE_URL" -f db/seeds/classification.sql
-- 何度流しても安全: 語彙は on conflict do nothing、資料は「未分類（target も doc_type も NULL）の行」だけを更新する。
-- 資料は id ではなく source と collection で照合するので、作り直した DB で id が違っても合う。資料がまだ無い行は何もしない。
-- 本番には使わない（本番は 2026-11-04 に適用済み。運用データの変更は、承認を得て別に行う）。
-- 承認済みの初期分類（2026-11-03）。名前（name）は変えられない（DB のトリガーが拒否）。

begin;

insert into document_targets (name, label, description, sort_order) values
  ('zahard', 'ザーハード', '架空の惑星ザーハードの設定資料の対象です。', 10),
  ('minato-coffee', 'オアシス珈琲', '喫茶店「オアシス珈琲」の店舗案内の対象です。', 20),  -- 2026-11-18 に店名を書き換え（name は変えられない。既存の DB は label を UPDATE する）
  ('tsubasa-dorm', 'ザーハード大学', 'ザーハード大学（旧称つばさ大学）の学生寮の規程などの対象です。', 30),  -- 2026-11-19 に、つばさ大学学生寮から改称（name は変えられない。既存の DB は label と description を UPDATE する）
  ('aoba-aw820', 'リューネ工房 FP-820', 'リューネ工房の家庭用二人乗り浮上ポッド FP-820 の資料の対象です。', 40),  -- 2026-11-19 に、洗濯乾燥機から書き換え（name は変えられない。既存の DB は label と description を UPDATE する）
  ('lune-fc410', 'リューネ工房 FC-410', 'リューネ工房の荷運び浮上カート FC-410 の資料の対象です。', 50),  -- 2026-11-19 に追加（リューネ工房の新製品。合成テスト資料。既存の DB は、このファイルを流せば足される）
  ('lune-fs250', 'リューネ工房 FS-250', 'リューネ工房のひとり乗り浮上スクーター FS-250 の資料の対象です。', 60),
  ('lune-fw1500', 'リューネ工房 FW-1500', 'リューネ工房の家族用六人乗り浮上ワゴン FW-1500 の資料の対象です。', 70),
  ('lune-fd300', 'リューネ工房 FD-300', 'リューネ工房の充電係留ステーション FD-300 の資料の対象です。', 80),
  ('hoshizora-kg', 'ほしぞら幼稚園', 'アルカ市のほしぞら幼稚園の資料の対象です。', 90),  -- 2026-11-19 に追加（アルカ市の学校 4 校。合成テスト資料）
  ('arca-elem', 'アルカ第一小学校', 'アルカ市のアルカ第一小学校の資料の対象です。', 100),
  ('arca-junior', 'アルカ中央中学校', 'アルカ市のアルカ中央中学校の資料の対象です。', 110),
  ('oasis-high', 'オアシス高等学校', 'アルカ市のオアシス高等学校の資料の対象です。', 120)
on conflict (name) do nothing;

insert into document_types (name, label, description, sort_order) values
  ('setting', '設定資料', '世界観や設定をまとめた資料です。', 10),
  ('store-guide', '店舗案内', '店舗の案内や利用方法の資料です。', 20),
  ('regulation', '規程', '規則や決まりを定めた資料です。', 30),
  ('manual', '取扱説明書', '機器の使い方を説明した資料です。', 40)
on conflict (name) do nothing;

update documents d
   set target = v.target, doc_type = v.doc_type
  from (values
    ('cafe_store_guide.pdf', 'cafe', 'minato-coffee', 'store-guide'),
    ('aoba_aw820_manual.pdf', 'guides', 'aoba-aw820', 'manual'),
    ('tsubasa_dorm_rules.pdf', 'guides', 'tsubasa-dorm', 'regulation'),
    ('ザーハード1.pdf', 'zahard', 'zahard', 'setting'),
    ('ザーハード2.pdf', 'zahard', 'zahard', 'setting'),
    ('ザーハード3.pdf', 'zahard', 'zahard', 'setting')
  ) as v (source, collection, target, doc_type)
 where d.source = v.source and d.collection = v.collection
   and d.target is null and d.doc_type is null;

-- 確認（読むだけ。期待: 6 行とも target / doc_type が入っている）
--   select id, source, collection, target, doc_type from documents order by id;

commit;
