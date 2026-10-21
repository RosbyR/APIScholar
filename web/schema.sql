-- D1 schema for website votes. Apply once:
--   npx wrangler@4.145.0 d1 execute paperbot --remote --file web/schema.sql
-- Rows are append-only; `paperbot pull-feedback` imports rows with id > its watermark.
CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id   TEXT NOT NULL,
    rating     INTEGER NOT NULL CHECK (rating IN (1, -1)),
    note       TEXT,
    at         TEXT NOT NULL,
    user_email TEXT
);
CREATE INDEX IF NOT EXISTS idx_feedback_paper ON feedback(paper_id);
