-- Display labels of a chunk's pages, from the PDF's /PageLabels (cover, i, 1, A-1 ...).
-- `page` / `page_end` stay the 1-based physical page numbers (citations link to them, the viewer opens #page=N).
-- Additive and nullable: code that does not know the columns keeps working, and NULL means "show the page number".
-- Filled by re-ingesting a PDF (a chunk whose text did not change is updated in place; no re-embedding).
ALTER TABLE chunks ADD COLUMN page_label text;
ALTER TABLE chunks ADD COLUMN page_end_label text;
