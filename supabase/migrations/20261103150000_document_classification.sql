set role postgres; -- Ensure objects are owned by postgres, matching the existing schema
-- 資料の分類を 2 つ足す（画面の名前は「対象」「資料種別」）。`documents.collection`（保存・認可の単位）は変えない。
--
--   document_targets  対象（何についての資料か。例: ザーハード / みなとコーヒー）。チャットの質問の範囲になる
--   document_types    資料種別（資料の性格。例: 設定資料 / 店舗案内 / 規程 / 取扱説明書）。一覧の絞り込みだけに使う
--   documents.target / documents.doc_type  資料ごとに 1 つずつ（NULL = 未分類）。それぞれの表の name への外部キー
--
-- 閲覧権限は変わらない（role_collection_access は collection だけを見る）。検索は「権限の範囲 ∩ 対象」で絞る。
-- target を選んでも権限は広がらない。R2 の配置・埋め込み・チャンクは変えない（資料単位のメタデータだけを足す）。
-- documents.collection・created_at・updated_at の意味は変えない（この SQL は updated_at を更新しない。documents に更新のトリガーは無い）。
--
-- 名前（name）は変えない（documents.target / documents.doc_type と、会話に保存した対象の識別子と食い違うため）。
-- DB のトリガーで UPDATE を拒否する（document_collections と同じ）。表示名・説明・並び順は変えてよい。
-- 外部キーを張る（document_collections と違う）: 取り込みは target / doc_type を書かないので、未登録の名前が入る経路が無く、
-- 運用者が SQL で分類を変えるとき、存在しない名前や綴りの誤りを DB が弾く。使用中の行は消せない（on delete restrict）。
--
-- 状態: 未適用（本番にも、ローカルにも）。適用は承認後。
-- 適用の順序: この SQL（M1）→ 検証の SELECT → コードを配る（API が列と表を読むので、逆だと一覧が 503 になる）。
--   分類の値の投入（バックフィル）は、別の SQL（supabase/backfills/20261103150000_classify_initial_documents.sql）を、
--   M1 のあとに、承認を得て 1 回だけ実行する。この SQL は列と初期の語彙を作るだけで、資料の行は更新しない。
-- 権限: 所有者は postgres（先頭の set role）。API の chatbot_app には SELECT だけを付ける（表）。documents の列は、
--   既存の列と同じ（所有者の権限で読める。chatbot_app は documents を読める）。anon / authenticated は全権限を外す。
--   行レベルのセキュリティ（RLS）は有効にしない（有効にすると、所有者でない chatbot_app は、ポリシーが無いので 0 行しか読めない）。
-- 再実行しても安全（create ... if not exists、create or replace、drop trigger if exists、on conflict do nothing）。
-- インデックスは作らない（資料は数件。一覧 API は全件を返し、検索は collection と同じく seq scan で足りる）。

create table if not exists chatbot.document_targets (
  name text primary key
    constraint document_targets_name_format check (name ~ '^[a-z0-9][a-z0-9-]{0,39}$'),
  label text not null
    constraint document_targets_label_length check (char_length(btrim(label)) between 1 and 60),
  description text not null default ''
    constraint document_targets_description_length check (char_length(description) <= 300),
  sort_order integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists chatbot.document_types (
  name text primary key
    constraint document_types_name_format check (name ~ '^[a-z0-9][a-z0-9-]{0,39}$'),
  label text not null
    constraint document_types_label_length check (char_length(btrim(label)) between 1 and 60),
  description text not null default '',
  sort_order integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint document_types_description_length check (char_length(description) <= 300)
);

create or replace function chatbot.document_classification_guard() returns trigger
language plpgsql as $$
begin
  if new.name is distinct from old.name then
    raise exception '%.name は変更できません', tg_table_name;
  end if;
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists document_targets_guard on chatbot.document_targets;
create trigger document_targets_guard
  before update on chatbot.document_targets
  for each row execute function chatbot.document_classification_guard();

drop trigger if exists document_types_guard on chatbot.document_types;
create trigger document_types_guard
  before update on chatbot.document_types
  for each row execute function chatbot.document_classification_guard();

alter table chatbot.documents
  add column if not exists target text
    references chatbot.document_targets (name) on update restrict on delete restrict;
alter table chatbot.documents
  add column if not exists doc_type text
    references chatbot.document_types (name) on update restrict on delete restrict;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'chatbot_app') then
    grant select on chatbot.document_targets, chatbot.document_types to chatbot_app;
  end if;
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on chatbot.document_targets, chatbot.document_types from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on chatbot.document_targets, chatbot.document_types from authenticated;
  end if;
end
$$;

-- 初期の語彙（資料の行は更新しない）。表示名は、利用者が承認した対応表（2026-11-03）のとおり
insert into chatbot.document_targets (name, label, description, sort_order) values
  ('zahard', 'ザーハード', '架空の惑星ザーハードの設定資料の対象です。', 10),
  ('minato-coffee', 'みなとコーヒー', '喫茶店「みなとコーヒー」の店舗案内の対象です。', 20),
  ('tsubasa-dorm', 'つばさ大学学生寮', 'つばさ大学の学生寮の規程の対象です。', 30),
  ('aoba-aw820', 'あおば電機 AW-820', 'あおば電機の機器 AW-820 の取扱説明書の対象です。', 40)
on conflict (name) do nothing;

insert into chatbot.document_types (name, label, description, sort_order) values
  ('setting', '設定資料', '世界観や設定をまとめた資料です。', 10),
  ('store-guide', '店舗案内', '店舗の案内や利用方法の資料です。', 20),
  ('regulation', '規程', '規則や決まりを定めた資料です。', 30),
  ('manual', '取扱説明書', '機器の使い方を説明した資料です。', 40)
on conflict (name) do nothing;

-- 検証（期待: 4 行 / 4 行 / 列 2 つ / documents の分類済み 0 件 = バックフィル前）
--   select name, label, sort_order from chatbot.document_targets order by sort_order;
--   select name, label, sort_order from chatbot.document_types order by sort_order;
--   select column_name from information_schema.columns
--     where table_schema = 'chatbot' and table_name = 'documents' and column_name in ('target', 'doc_type') order by 1;
--   select count(*) from chatbot.documents where target is not null or doc_type is not null;   -- 期待: 0（M1 の直後）
--   select has_table_privilege('chatbot_app', 'chatbot.document_targets', 'SELECT');           -- 期待: true

-- 元に戻す手順（先にコードを、target / doc_type を読まない版へ戻す。会話の注意は docs/configuration.md の「対象と資料種別」のロールバック）
--   alter table chatbot.documents drop column target;      -- 外部キーも一緒に消える
--   alter table chatbot.documents drop column doc_type;
--   drop table chatbot.document_targets;
--   drop table chatbot.document_types;
--   drop function chatbot.document_classification_guard();
