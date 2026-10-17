-- 埋め込みを作ったときの「送った入力全体（前置き・見出し・本文）」のハッシュ。適用は MCP の apply_migration で、同じ内容。
-- Supabase の履歴の版: 20261017185742（UTC 表記。JST では 2026-10-18 03:57）、名前 embedding_input_hash。適用済み。
--
-- 本文が同じでも、見出しや前置きの形が変わったときに、作り直しの対象にするため（app/ingestion/embedding.py の input_hash）。
-- 既存の行は null のまま残り、次の埋め込みの実行で作り直される（適用時点で、埋め込みはまだ 1 件も無かった）。

alter table chatbot.chunks
  add column if not exists embedding_input_hash text;
