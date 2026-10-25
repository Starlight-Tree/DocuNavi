-- pgroonga（日本語対応の全文検索）を有効にし、チャンク本文に全文検索の索引を作る（Supabase）。
-- 状態: 2026-10-18 に Supabase へ適用済み（書き込み可能な別名の MCP supabase-write から、承認を得て実行）。
-- 目的: 検索を DB の chunks に一本化するための比較（現行 BM25 / ベクトル / pgroonga + ベクトルの RRF。evals/README.md）で、
--       pgroonga の経路を測れるようにする。これだけではアプリの検索は変わらない（読むコードは別。比較のあとに決める）。
--
-- ・拡張は extensions スキーマに入れる（Supabase の手順。pgvector と同じ）。専用ロール chatbot_app は search_path が chatbot だけなので、
--   読む側の SQL では演算子（&@~）と関数（pgroonga_score）をスキーマ付きで書く。USAGE は 0001 で付与済み。
-- ・索引は本文（content）だけ。ベクトルの入力に使う見出し（heading）は含めない（比較で足りなければ別に決める）。
-- ・既定の分かち書きは Groonga の TokenBigram（日本語は 2 文字ずつ）。現行 BM25 の 2-gram に近いが、同じではない。
--   スコアは Groonga のもので、BM25 そのものではない。日本語での質は未測定。
-- ・app/ingestion/repository.py の _SETUP_SQL には入れない（CI の素の Postgres には pgroonga が無く、起動で落ちる。0001 と同じ理由）。
-- ・ロールバック（drop index / drop extension）は、影響を確かめてから別に承認を得る（このファイルには含めない）。

create extension if not exists pgroonga with schema extensions;

create index if not exists chunks_content_pgroonga_idx
  on chatbot.chunks using pgroonga (content);
