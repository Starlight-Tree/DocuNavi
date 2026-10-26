set role postgres; -- Ensure objects are owned by postgres, matching the existing schema
-- ロールごとに、見られる資料グループ（documents.collection の値）を決める表。ロードマップ ⑧ のフェーズ 1（表と初期データだけ）。
-- この SQL は表を作るだけで、API はまだ読まない（読む実装はフェーズ 2）。documents / chunks / document_collections は変えない。
--
--   roles                   ロールの一覧（name が主キー）。利用者に何も割り当てが無いときは default ロールになる（API 側で決める。フェーズ 2）
--   user_roles              利用者（JWT の sub = uuid）とロールの対応。auth.users への外部キーは張らない
--                           （API は Supabase の auth スキーマを読まない。利用者の削除とも切り離す）
--   role_collection_access  ロールが見られる資料グループ。document_collections への外部キーは張らない
--                           （document_collections.name の注意と同じ。未登録のフォルダの取り込みを妨げない）
--   access_audit            上の 3 表の変更の記録（追記だけ。トリガーが書く）
--
-- 初期データ: default ロールに、現行の 3 グループ（zahard / cafe / guides）を割り当てる。user_roles は空（全員が default）。
-- 利用者へのロールの割り当ては、フェーズ 4 の管理画面までは、運用者が SQL で行う。
--
-- 状態: 未適用（本番にも、ローカルにも）。適用は承認後（フェーズ 3）。一時 Postgres 17 での検証はコミット時に済み。
-- 権限: 所有者は postgres（先頭の set role）。API の chatbot_app には SELECT だけを付ける（書き込みは付けない）。
--   anon / authenticated は全権限を外す。行レベルのセキュリティ（RLS）は有効にしない（フェーズ 1 の範囲外。
--   有効にすると、所有者でない chatbot_app は、ポリシーが無いので 0 行しか読めない）。
-- 監査: access_audit.actor は session_user（ログインしたロール）。set role では変わらない。行の内容（row_data）は、
--   INSERT は新しい行、DELETE は古い行、UPDATE は {"old": 古い行, "new": 新しい行}、TRUNCATE は null。
--   列名は table / row ではなく table_name / row_data（table は SQL の予約語で、毎回引用符が要るため）。
-- 再実行しても安全（create ... if not exists、create or replace、drop trigger if exists、on conflict do nothing）。

create table if not exists chatbot.roles (
  name text primary key
    constraint roles_name_format check (name ~ '^[A-Za-z0-9_-]{1,40}$'),
  label text not null
    constraint roles_label_length check (char_length(btrim(label)) between 1 and 60)
);

create table if not exists chatbot.user_roles (
  user_id uuid not null,
  role text not null references chatbot.roles (name),
  primary key (user_id, role)
);
-- user_id だけの索引は作らない。主キー (user_id, role) の先頭が user_id なので、user_id での検索はその索引で足りる。

create table if not exists chatbot.role_collection_access (
  role text not null references chatbot.roles (name),
  collection text not null,
  primary key (role, collection)
);

create table if not exists chatbot.access_audit (
  id bigint generated always as identity primary key,
  at timestamptz not null default now(),
  actor text not null,
  table_name text not null,
  op text not null,
  row_data jsonb
);

-- 変更の記録。security definer にするのは、変更した人（運用者のロール）が access_audit への書き込み権限を持たなくても、
-- 記録だけは必ず残すため。search_path は固定する。
create or replace function chatbot.access_audit_log() returns trigger
language plpgsql
security definer
set search_path = chatbot, pg_temp
as $$
begin
  insert into chatbot.access_audit (actor, table_name, op, row_data)
  values (
    session_user,
    tg_table_name,
    tg_op,
    case tg_op
      when 'INSERT' then to_jsonb(new)
      when 'DELETE' then to_jsonb(old)
      when 'UPDATE' then jsonb_build_object('old', to_jsonb(old), 'new', to_jsonb(new))
      else null
    end
  );
  return null;
end;
$$;

revoke all on function chatbot.access_audit_log() from public;

drop trigger if exists roles_audit on chatbot.roles;
create trigger roles_audit
  after insert or update or delete on chatbot.roles
  for each row execute function chatbot.access_audit_log();
drop trigger if exists roles_audit_truncate on chatbot.roles;
create trigger roles_audit_truncate
  after truncate on chatbot.roles
  for each statement execute function chatbot.access_audit_log();

drop trigger if exists user_roles_audit on chatbot.user_roles;
create trigger user_roles_audit
  after insert or update or delete on chatbot.user_roles
  for each row execute function chatbot.access_audit_log();
drop trigger if exists user_roles_audit_truncate on chatbot.user_roles;
create trigger user_roles_audit_truncate
  after truncate on chatbot.user_roles
  for each statement execute function chatbot.access_audit_log();

drop trigger if exists role_collection_access_audit on chatbot.role_collection_access;
create trigger role_collection_access_audit
  after insert or update or delete on chatbot.role_collection_access
  for each row execute function chatbot.access_audit_log();
drop trigger if exists role_collection_access_audit_truncate on chatbot.role_collection_access;
create trigger role_collection_access_audit_truncate
  after truncate on chatbot.role_collection_access
  for each statement execute function chatbot.access_audit_log();

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'chatbot_app') then
    grant select on chatbot.roles, chatbot.user_roles, chatbot.role_collection_access, chatbot.access_audit to chatbot_app;
  end if;
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on chatbot.roles, chatbot.user_roles, chatbot.role_collection_access, chatbot.access_audit from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on chatbot.roles, chatbot.user_roles, chatbot.role_collection_access, chatbot.access_audit from authenticated;
  end if;
end
$$;

-- 初期データ（現行の 3 グループは、すべて default ロールで見られる）
insert into chatbot.roles (name, label) values
  ('default', '標準')
on conflict (name) do nothing;

insert into chatbot.role_collection_access (role, collection) values
  ('default', 'zahard'),
  ('default', 'cafe'),
  ('default', 'guides')
on conflict (role, collection) do nothing;

-- 検証（期待は各行の右）
--   select name, label from chatbot.roles;                                       -- default / 標準 の 1 行
--   select role, collection from chatbot.role_collection_access order by 2;     -- default × cafe / guides / zahard の 3 行
--   select count(*) from chatbot.user_roles;                                    -- 0
--   select table_name, op from chatbot.access_audit order by id;                -- roles INSERT、role_collection_access INSERT × 3（初回の適用のとき）
--   select has_table_privilege('chatbot_app', t, 'SELECT'), has_table_privilege('chatbot_app', t, 'INSERT')
--     from unnest(array['chatbot.roles','chatbot.user_roles','chatbot.role_collection_access','chatbot.access_audit']) t;   -- すべて true / false
--   select has_table_privilege('anon', t, 'SELECT') from unnest(array[…同じ 4 表…]) t;   -- anon / authenticated ともすべて false（ロールがある環境で）
--   select c.relname, c.relrowsecurity, pg_get_userbyid(c.relowner)
--     from pg_class c where c.relnamespace = 'chatbot'::regnamespace and c.relname in ('roles','user_roles','role_collection_access','access_audit');
--                                                                                -- relrowsecurity はすべて false、所有者は postgres
--   select a.collection from chatbot.role_collection_access a
--     left join (select distinct collection from chatbot.documents) d on d.collection = a.collection
--     where d.collection is null;                                                -- 資料が 1 件も無い割り当て（あれば確認）

-- 元に戻す手順（フェーズ 2 のコードを配る前なら、これだけ。コードを配った後は、先にコードを前の版へ戻す）
--   drop table chatbot.role_collection_access, chatbot.user_roles, chatbot.roles, chatbot.access_audit;
--   drop function chatbot.access_audit_log();
