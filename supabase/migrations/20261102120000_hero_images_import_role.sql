set role postgres; -- Ensure objects are owned by postgres, matching the existing schema
-- ヒーロー画像の取り込み（python -m app.hero_images の list / import）専用のロール hero_images_import。
-- 取り込みのたびに owner（postgres）の資格情報を使わずに済ませるため（2026-11-02、ユーザーが承認）。
--
-- 付与するもの（app/hero_images/__init__.py の SQL から導いた最小の権限）:
--   chatbot スキーマの USAGE
--   chatbot.documents の id・content_hash の SELECT（import が content_hash を読む）
--   chatbot.document_images の全 8 列の SELECT（list の octet_length(data)、INSERT ... RETURNING id）
--   chatbot.document_images の 10 列の INSERT（import）
-- 付与しないもの: UPDATE・DELETE。approve / reject と draft 行の削除は、owner での操作（操作ごとに承認）。
--
-- 注意:
--   ・現行の import は status 列を明示して INSERT するので、列権限では draft に固定できない。
--     このロールで approved の行を直接挿入すると、その画像が公開される。import 以外の SQL を流さない運用にする。
--     固定するには、INSERT 文から status を外し、INSERT(status) の権限を外す（コードの変更。別の承認）。
--   ・id は identity 列。INSERT 権限だけで足りる見込み（PostgreSQL の仕様。実測は適用後）。
--   ・ロールはクラスター全体のもの。パスワードはここに書かない（適用後に psql の \password で設定する）。
--   ・接続は Session pooler（5432）。ユーザー名は hero_images_import.<プロジェクト ID>。接続情報は ~/.pg/pgpass（権限 600）に置く。
-- 再実行しても安全（ロールは無ければ作る。GRANT は重ねても同じ）。

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'hero_images_import') then
    create role hero_images_import login nosuperuser nocreatedb nocreaterole
      noinherit noreplication nobypassrls connection limit 2;
  end if;
end
$$;

grant usage on schema chatbot to hero_images_import;
grant select (id, content_hash) on chatbot.documents to hero_images_import;
grant select (id, document_id, kind, status, mime_type, data, width, height, created_at)
  on chatbot.document_images to hero_images_import;
grant insert (document_id, kind, status, mime_type, data, width, height,
              document_hash, prompt_sha256, model)
  on chatbot.document_images to hero_images_import;

-- 検証（期待値は右）
--   select has_column_privilege('hero_images_import', 'chatbot.document_images', 'data', 'SELECT');    -- true
--   select has_table_privilege('hero_images_import', 'chatbot.document_images', 'INSERT');            -- false（列単位だけ）
--   select has_column_privilege('hero_images_import', 'chatbot.document_images', 'status', 'INSERT'); -- true
--   select has_table_privilege('hero_images_import', 'chatbot.document_images', 'UPDATE');            -- false
--   select has_table_privilege('hero_images_import', 'chatbot.document_images', 'DELETE');            -- false

-- 元に戻す手順（owner で。先に pgpass の行を消す。pgpass の行を消すだけでは DB 側は失効しない）
--   alter role hero_images_import nologin;                                   -- 止めるだけ
--   drop owned by hero_images_import; drop role hero_images_import;          -- 消す
