-- pgvector を有効にし、チャンクごとの埋め込みの列を足す（Supabase。適用は MCP の apply_migration で、同じ内容）。
-- Supabase の履歴の版: 20261017184901（UTC 表記。JST では 2026-10-18 03:49）、名前 pgvector_embeddings。
--
-- ・専用ロール（chatbot_app）の search_path は chatbot だけなので、型は extensions.vector と、スキーマ付きで書く。
-- ・768 次元は gemini-embedding-2 の出力次元（推奨値の 1 つ）。pgvector の索引は vector 型で 2000 次元までなので、
--   3072 のままでは索引が作れない。件数が少ないうちは索引を作らない。
-- ・embedding_model / embedding_dimensions は、モデルを変えたときにどの行が古いかを見分けるため。
-- ・app/ingestion/repository.py の _SETUP_SQL には入れない（CI の素の Postgres には pgvector が無く、起動で落ちる）。
-- ・既存の行は embedding が null のまま残る。ロールバックは、影響を確かめてから別に承認を得る（このファイルには含めない）。

create extension if not exists vector with schema extensions;
grant usage on schema extensions to chatbot_app;

alter table chatbot.chunks
  add column if not exists embedding extensions.vector(768),
  add column if not exists embedding_model text,
  add column if not exists embedding_dimensions integer;
