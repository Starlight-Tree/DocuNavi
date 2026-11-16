-- Bookkeeping for the suggestion step that runs after ingestion (app/ingestion/suggest.py).
-- Additive and nullable: code that does not know the columns keeps working.
-- `suggestions_at`: unix seconds when the questions were last saved. Used for a per-document cooldown (no regeneration within minutes
--   of the last save, which also stops two jobs from generating the same document) and for the daily cap.
-- `suggestions_failure`: {"hash": <documents.content_hash the failure belongs to>, "count": n, "at": <unix seconds>, "reason": <fixed code>}.
--   A failure whose hash differs from the current content_hash belongs to an older PDF and is ignored. Cleared when questions are saved.
ALTER TABLE documents ADD COLUMN suggestions_at bigint;
ALTER TABLE documents ADD COLUMN suggestions_failure jsonb
  CHECK (suggestions_failure IS NULL OR jsonb_typeof(suggestions_failure) = 'object');
