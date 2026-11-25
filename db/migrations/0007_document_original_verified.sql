-- When the PDF of a document (its name and content_hash) was last confirmed to be in the PDF store (R2).
-- Additive and nullable: code that does not know the column keeps working. NULL = not confirmed, so GET /documents/{id}/chunks
-- falls back to a live check of the store (and reports original_pdf from that). Not NULL = confirmed at that time, so /chunks
-- answers original_pdf = true without touching R2: the check moves from the read path to the write path (ADR 0017).
-- Written only by `python -m app.r2_store verify --record` (it sets or clears the value for each ready document), and cleared by
-- ingestion whenever a document's content_hash changes (the confirmation was for the old PDF). GET /documents/{id}/file still
-- verifies the PDF on every request. Apply this migration before deploying code that reads the column.
ALTER TABLE documents ADD COLUMN original_verified_at timestamptz;
