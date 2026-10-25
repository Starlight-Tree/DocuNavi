-- 本番 DB（Supabase）を読み取り専用で見るためのロール `readonly`（2026-10-21 に作成。適用は supabase-write の execute_sql）。
--
-- ・アプリの移行（supabase/migrations/）ではなく、運用の記録。ロールはクラスター全体のもので、適用の履歴にパスワードを残さないため入れていない。
-- ・見えるのは chatbot.documents と chatbot.chunks だけ。会話履歴（checkpoints など。発言の本文）は読めない。
-- ・default_transaction_read_only は、セッション内で外せる。本当の防壁は「書き込みの権限を付けないこと」
--   （外した状態でも insert / delete / update が permission denied になることを、2026-10-21 に確かめた）。
-- ・既定の権限（alter default privileges）で、今後 chatbot_app が作る表も読める。履歴系の表が増えたら、それも読める点に注意。
--   適用には、実行するロール（postgres）が chatbot_app のメンバーであることが要る（docs/configuration.md の「DB のロール」）。
-- ・パスワードはここに書かない。ロール作成のあと、Studio の SQL Editor で別に設定する:
--     alter role readonly password '…';
-- ・接続は Session pooler（5432）。ユーザー名は `readonly.<プロジェクト ID>`。接続情報は ~/.pgpass（権限 600）に置く。

create role readonly login;

alter role readonly set default_transaction_read_only = on;
alter role readonly set statement_timeout = '30s';
alter role readonly set search_path = chatbot;

grant usage on schema chatbot to readonly;
grant usage on schema extensions to readonly;  -- embedding 列（extensions.vector）を含めて読むため

grant select on chatbot.documents, chatbot.chunks to readonly;

alter default privileges for role chatbot_app in schema chatbot
  grant select on tables to readonly;

-- 会話履歴も読ませたいときだけ（発言の本文が見える）:
-- grant select on all tables in schema chatbot to readonly;
