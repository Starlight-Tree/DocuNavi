-- 資料グループ（documents.collection の値）の表示用の情報。表示名・説明・並び順を持つ。
-- 検索の絞り込みに使う値は、documents.collection の名前そのもの。この表は、画面に見せる情報を足すだけ（検索はこの表を読まない）。
--
-- 外部キーは張らない。documents.collection には、この表に無い名前も入る（storage/pdf/ や R2 に新しいフォルダを置いて取り込むだけで
-- 資料群になる）。外部キーにすると、未登録のフォルダの取り込みが失敗する。未登録の名前は、API が表示名に名前そのものを使って返す。
-- 名前（name）は変えない（documents.collection や、会話に保存した絞り込みの値と食い違うため）。DB のトリガーで UPDATE を拒否する。
-- 説明（description）は空でもよい。
--
-- 状態: 未適用（本番にも、ローカルにも）。適用は承認後。
-- 適用の順序: この SQL → 検証の SELECT → コードを配る（API が表を読むので、逆だと GET /collections が 503 になる）。
-- 権限: 作成者（MCP の apply_migration では postgres）が所有者になる。API の chatbot_app が読めるよう SELECT を付ける。
--   anon / authenticated には付けない。行レベルのセキュリティ（RLS）は有効にしない
--   （有効にすると、所有者でない chatbot_app は、ポリシーが無いので 0 行しか読めない）。
-- 再実行しても安全（create ... if not exists、create or replace、on conflict do nothing、drop trigger if exists）。
-- 書き込み（Django の管理画面）は、この表だけを更新できる別のロールで行う予定（未作成。S4 で決める）。

create table if not exists chatbot.document_collections (
  name text primary key
    constraint document_collections_name_format check (name ~ '^[A-Za-z0-9_-]{1,40}$'),
  label text not null
    constraint document_collections_label_length check (char_length(btrim(label)) between 1 and 60),
  description text not null default ''
    constraint document_collections_description_length check (char_length(description) <= 300),
  sort_order integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create or replace function chatbot.document_collections_guard() returns trigger
language plpgsql as $$
begin
  if new.name is distinct from old.name then
    raise exception 'document_collections.name は変更できません';
  end if;
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists document_collections_guard on chatbot.document_collections;
create trigger document_collections_guard
  before update on chatbot.document_collections
  for each row execute function chatbot.document_collections_guard();

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'chatbot_app') then
    grant select on chatbot.document_collections to chatbot_app;
  end if;
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on chatbot.document_collections from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on chatbot.document_collections from authenticated;
  end if;
end
$$;

-- 既存の 3 グループの初期値（表示名は frontend-next/src/lib/collections.ts と同じ。説明はファイル名からの仮の文）
insert into chatbot.document_collections (name, label, description, sort_order) values
  ('zahard', '設定資料', 'ザーハードに関する設定資料です。', 10),
  ('cafe', '店舗案内', '店舗の案内や利用方法に関する資料です。', 20),
  ('guides', '規程・取扱説明書', '規程や機器の取扱説明書です。', 30)
on conflict (name) do nothing;

-- 検証（期待: 3 行 zahard / cafe / guides。検索に使う documents.collection と突き合わせて、未登録の名前が無いこと）
--   select name, label, sort_order from chatbot.document_collections order by sort_order, name;
--   select d.collection, count(*) from chatbot.documents d
--     left join chatbot.document_collections c on c.name = d.collection
--     where d.collection is not null and c.name is null group by 1;   -- 期待: 0 行
--   select has_table_privilege('chatbot_app', 'chatbot.document_collections', 'SELECT');   -- 期待: true

-- 元に戻す手順（先にコードを前の版へ戻す。API が表を読まなくなってから）
--   drop table chatbot.document_collections;
--   drop function chatbot.document_collections_guard();
