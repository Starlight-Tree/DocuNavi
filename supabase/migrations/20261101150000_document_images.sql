set role postgres; -- Ensure objects are owned by postgres, matching the existing schema
-- 資料ごとの画像（ヒーローカルーセルなどに出す、生成した画像）。画像のバイト列を DB に持つ（docs/image-generation-design.md の D2）。
--
--   status    draft（取り込んだだけ。出さない）/ approved（人が見て承認した。出す）/ rejected（出さない）
--   kind      いまは hero だけ。他の画面（参照資料カードなど）に広げるときに足す
--   資料ごと・kind ごとに approved は 1 枚だけ（部分一意索引）。承認し直すときは、古い approved を rejected にしてから新しい方を approved にする
--   document_hash  画像を作ったときの documents.content_hash（資料が更新されたか、plan が見分けるため）
--   prompt_sha256  送ったプロンプトのハッシュ。プロンプトの本文は保存しない（本文は plan.json にある）
--   model          生成に使ったモデルの名前
--
-- 外部キーは documents(id)。資料を消すと画像も消える（on delete cascade）。
-- 見える範囲: 画像の配信は、資料の可視性と同じ判定（資料グループ）を API が通す。この表は資料グループを持たない（documents から引く）。
--
-- 状態: 未適用（本番にも、ローカルにも）。適用は承認後。
-- 適用の順序: この SQL → 検証の SELECT → コードを配る（API の一覧が document_images を読むので、逆だと GET /documents が 503 になる）。
-- 権限: 所有者は postgres（先頭の set role）。API の chatbot_app には SELECT だけを付ける（書き込みは付けない）。
--   画像の取り込み・承認（python -m app.hero_images）は、書き込める接続（運用者の DATABASE_URL）で行う。どのロールで行うかは未確認。
--   anon / authenticated は全権限を外す。行レベルのセキュリティ（RLS）は有効にしない
--   （有効にすると、所有者でない chatbot_app は、ポリシーが無いので 0 行しか読めない）。
-- 再実行しても安全（create ... if not exists）。

create table if not exists chatbot.document_images (
  id bigint generated always as identity primary key,
  document_id bigint not null references chatbot.documents (id) on delete cascade,
  kind text not null default 'hero'
    constraint document_images_kind check (kind in ('hero')),
  status text not null default 'draft'
    constraint document_images_status check (status in ('draft', 'approved', 'rejected')),
  mime_type text not null
    constraint document_images_mime check (mime_type in ('image/png', 'image/jpeg', 'image/webp')),
  data bytea not null
    constraint document_images_size check (octet_length(data) between 1 and 5242880),
  width integer
    constraint document_images_width check (width is null or width > 0),
  height integer
    constraint document_images_height check (height is null or height > 0),
  document_hash text,
  prompt_sha256 text not null
    constraint document_images_prompt_hash check (prompt_sha256 ~ '^[0-9a-f]{64}$'),
  model text not null
    constraint document_images_model check (char_length(btrim(model)) between 1 and 200),
  created_at timestamptz not null default now(),
  approved_at timestamptz
);

create unique index if not exists document_images_one_approved
  on chatbot.document_images (document_id, kind) where status = 'approved';
create index if not exists document_images_document on chatbot.document_images (document_id);

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'chatbot_app') then
    grant select on chatbot.document_images to chatbot_app;
  end if;
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on chatbot.document_images from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on chatbot.document_images from authenticated;
  end if;
end
$$;

-- 検証（期待: 0 行。has_table_privilege が true / anon・authenticated が false）
--   select count(*) from chatbot.document_images;
--   select has_table_privilege('chatbot_app', 'chatbot.document_images', 'SELECT');   -- 期待: true
--   select has_table_privilege('chatbot_app', 'chatbot.document_images', 'INSERT');   -- 期待: false

-- 元に戻す手順（先にコードを前の版へ戻す。API が表を読まなくなってから）
--   drop table chatbot.document_images;
