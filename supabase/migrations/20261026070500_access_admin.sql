set role postgres; -- Ensure objects are owned by postgres, matching the existing schema
-- ロール別の資料グループ制御の管理画面（Django Admin。ロードマップ ⑧ のフェーズ 4a）が使う、DB 側の用意。
-- 管理画面は、利用者へのロールの割り当て（chatbot.user_roles）を画面で行うための、運用者専用のサービス（ops/access_admin）。
--
--   access_admin         管理画面専用の DB ロール。パスワードはこの SQL では決めない（運用者が別に設定し、Secret Manager に置く）
--   saas スキーマ        Django 自身の表（ログイン・セッション・操作履歴・TOTP）の置き場。Django の migrate が変えるのはここだけ
--   chatbot.admin_users  auth.users のうち id / email / last_sign_in_at だけを見せる読み取り専用のビュー（所有者は postgres）
--
-- 権限（最小）:
--   chatbot.user_roles                SELECT / INSERT / DELETE（割り当てと解除。UPDATE と TRUNCATE は付けない）
--   chatbot.roles / role_collection_access / access_audit / document_collections / admin_users   SELECT だけ
--   chatbot.documents / chunks / チェックポイントの表   何も付けない
--   chatbot スキーマ                  USAGE だけ（CREATE は付けない。Django が誤って chatbot に表を作れない）
--   saas スキーマ                     USAGE と CREATE（所有者は postgres のまま。Django の表の所有者は access_admin になる）
-- chatbot の表の変更は、これまでどおり supabase/migrations/ だけが行う。chatbot_app の権限は変えない。
--
-- 監査: user_roles への変更は、フェーズ 1 のトリガー（access_audit_log）が access_audit に記録する。actor は session_user = access_admin。
--   誰が操作したかは、Django の操作履歴（saas.django_admin_log）に残る。
-- search_path: ロールに saas, chatbot を設定する。接続プーラーが起動時のオプションを無視しても、ロールの設定は効く
--   （管理画面の /readyz が、search_path と ビューを読めることを確かめる）。
--
-- 状態: 未適用（本番にも、ローカルにも）。適用は承認後（ステージ R3）。一時 Postgres 17 での検証はコミット時に済み。
-- 未確認: Supabase のホスト上で、postgres ロールがこの SQL（ロールの作成・ALTER ROLE ... SET・ビューの作成）を実行できること。
-- 再実行しても安全（ロールは無ければ作る、create ... if not exists、create or replace、grant は重ねても同じ）。
--
-- 元に戻す手順（管理画面のサービスを止めてから。saas の中身は、ログイン用の表なので消えてよい。利用者のロールの割り当ては消えない）:
--   drop view chatbot.admin_users;
--   drop schema saas cascade;
--   drop owned by access_admin;   -- 付けた権限を外す（上の drop で表は消えている）
--   drop role access_admin;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'access_admin') then
    create role access_admin login nosuperuser nocreatedb nocreaterole noinherit noreplication nobypassrls;
  end if;
end
$$;

alter role access_admin set search_path = saas, chatbot;

create schema if not exists saas;
grant usage, create on schema saas to access_admin;
grant usage on schema chatbot to access_admin;

-- auth.users の一部の列だけ。ビューの所有者（postgres）の権限で読むので、access_admin に auth スキーマの権限は要らない
create or replace view chatbot.admin_users as
  select id, email, last_sign_in_at
  from auth.users;

do $$
begin
  revoke all on chatbot.admin_users from public;
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on chatbot.admin_users from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on chatbot.admin_users from authenticated;
  end if;
  if exists (select 1 from pg_roles where rolname = 'chatbot_app') then
    revoke all on chatbot.admin_users from chatbot_app;
  end if;
end
$$;

grant select, insert, delete on chatbot.user_roles to access_admin;
grant select on chatbot.roles, chatbot.role_collection_access, chatbot.access_audit, chatbot.document_collections,
  chatbot.admin_users to access_admin;

-- 検証（期待は各行の右）
--   select rolname, rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolinherit
--     from pg_roles where rolname = 'access_admin';                                  -- t / f / f / f / f / f
--   select rolconfig from pg_roles where rolname = 'access_admin';                   -- {"search_path=saas, chatbot"}
--   select nspname, pg_get_userbyid(nspowner) from pg_namespace where nspname = 'saas';   -- saas / postgres
--   select pg_get_userbyid(c.relowner), c.relkind from pg_class c
--     where c.relnamespace = 'chatbot'::regnamespace and c.relname = 'admin_users';  -- postgres / v
--   select table_name, privilege_type from information_schema.role_table_grants
--     where grantee = 'access_admin' order by 1, 2;                                  -- 上の「権限」のとおり（user_roles は DELETE / INSERT / SELECT）
--   select has_schema_privilege('access_admin', 'chatbot', 'CREATE');                -- false
--   select has_table_privilege('access_admin', 'chatbot.documents', 'SELECT');       -- false
--   select has_table_privilege('access_admin', 'chatbot.user_roles', 'UPDATE');      -- false
--   select column_name from information_schema.columns
--     where table_schema = 'chatbot' and table_name = 'admin_users' order by ordinal_position;   -- id / email / last_sign_in_at
