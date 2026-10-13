-- "Send to Kindle" presses on the web pages. The web app adds a row; the pipeline's scheduler
-- builds the book, sends it to that reader's own Kindle address and records how it went.
CREATE TABLE send_requests (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    article_id TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'sent', 'failed')),
    error TEXT NOT NULL DEFAULT '',
    done_at TEXT
);
CREATE INDEX send_requests_user ON send_requests (user_id, requested_at);
