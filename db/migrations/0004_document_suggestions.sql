-- Suggested follow-up questions per document, shown under an answer as "続けて聞く" (up to 3, each answerable from that document).
-- Additive and nullable: code that does not know the columns keeps working, and NULL means "no suggestions yet".
-- Filled by `python -m app.ingestion.suggest --apply` (one LLM call per document). `suggestions_hash` is the documents.content_hash
-- the questions were made from; a document whose hash differs (the PDF changed) is regenerated.
ALTER TABLE documents ADD COLUMN suggested_questions jsonb
  CHECK (suggested_questions IS NULL OR (jsonb_typeof(suggested_questions) = 'array' AND jsonb_array_length(suggested_questions) <= 3));
ALTER TABLE documents ADD COLUMN suggestions_hash text;
