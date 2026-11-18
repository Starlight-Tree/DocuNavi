-- A document's revision label shown next to its title ("初版", "改訂 2", "2026年4月1日時点").
-- Additive and nullable: code that does not know the column keeps working, and a document without a label shows nothing.
-- The value is written only by ingestion, from `revisions.csv` beside the PDFs (app/ingestion/revisions.py). It is never guessed
-- from the PDF text or metadata. NULL = no label. The length limit is the same as the one the ingestion enforces.
ALTER TABLE documents ADD COLUMN revision text
  CHECK (revision IS NULL OR char_length(revision) BETWEEN 1 AND 40);
