-- ローカル DB 用の種データ: 対象（target）・資料種別（doc_type）の語彙と、初期 6 資料の分類。
-- 新しい DB に db/migrations/0001_schema.sql を適用したあと、取り込み（python -m app.ingestion）を終えてから、手で 1 回流す。
--   psql "$DATABASE_URL" -f db/seeds/classification.sql
-- 何度流しても安全: 語彙は on conflict do nothing、資料は「未分類（target も doc_type も NULL）の行」だけを更新する。
-- 資料は id ではなく source と collection で照合するので、作り直した DB で id が違っても合う。資料がまだ無い行は何もしない。
-- 本番には使わない（本番は 2026-11-04 に適用済み。運用データの変更は、承認を得て別に行う）。
-- 承認済みの初期分類（2026-11-03）。名前（name）は変えられない（DB のトリガーが拒否）。

begin;

-- 2026-11-19: 画面の「対象」メニューを 4 行（ザーハード・オアシス珈琲・学校・リューネ工房）にするため、対象そのものを粗くした。
--   学校 = アルカ市の 4 校（ほしぞら幼稚園・アルカ第一小学校・アルカ中央中学校・オアシス高等学校）とザーハード大学。リューネ工房 = 浮上ポッドを含む 5 製品。
--   製品・学校ごとの絞り込みは、検索欄で行う。旧い対象（tsubasa-dorm・aoba-aw820・lune-*・hoshizora-kg・arca-*・oasis-high）は、このファイルでは作らない。
--   既存の DB は、資料の target を付け替えてから、旧い対象の行を消す（付け替えの手順は docs/configuration.md の「対象のまとめ」）。
insert into document_targets (name, label, description, sort_order) values
  ('zahard', 'ザーハード', '架空の惑星ザーハードの設定資料の対象です。', 10),
  ('minato-coffee', 'オアシス珈琲', '喫茶店「オアシス珈琲」の店舗案内の対象です。', 20),  -- 2026-11-18 に店名を書き換え（name は変えられない。既存の DB は label を UPDATE する）
  ('school', '学校', 'アルカ市の幼稚園・小学校・中学校・高等学校と、ザーハード大学の資料の対象です。', 30),
  ('lune', 'リューネ工房', 'リューネ工房の浮上ポッド・カート・スクーター・ワゴン・充電係留ステーションの資料の対象です。', 40)
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
    ('aoba_aw820_manual.pdf', 'guides', 'lune', 'manual'),
    ('tsubasa_dorm_rules.pdf', 'guides', 'school', 'regulation'),
    ('ザーハード1.pdf', 'zahard', 'zahard', 'setting'),
    ('ザーハード2.pdf', 'zahard', 'zahard', 'setting'),
    ('ザーハード3.pdf', 'zahard', 'zahard', 'setting')
  ) as v (source, collection, target, doc_type)
 where d.source = v.source and d.collection = v.collection
   and d.target is null and d.doc_type is null;

-- 確認（読むだけ。期待: 6 行とも target / doc_type が入っている）
--   select id, source, collection, target, doc_type from documents order by id;

commit;
