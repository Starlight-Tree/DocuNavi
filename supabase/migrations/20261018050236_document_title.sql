-- 文書の題名（PDF のメタデータ /Title）。source（ファイル名）とは別に持ち、資料一覧の主表示に使う。
-- 適用は MCP の apply_migration で、同じ内容。Supabase の履歴の版: 20261018050236（UTC 表記。JST では 2026-10-18 14:02）、名前 document_title。適用済み。
--
-- null のまま残してよい（題名の無い PDF、まだ読み直していない既存の文書）。画面はファイル名で代える。
-- 既存の文書の題名は、この列を足しただけでは入らない。PDF から読み直して入れる別の手順が要る（承認してから）。
-- 適用の順序（この列は、適用済み）: この列を足す → コードを配る（API が d.title を SELECT するので、順序が逆だと一覧が 503 になる）。

alter table chatbot.documents
  add column if not exists title text;
