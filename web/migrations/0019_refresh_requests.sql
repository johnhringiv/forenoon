-- "Fetch again" on the Archive page. The web app adds a row; the pipeline's scheduler fetches
-- the article's page again, extracts it with the code it runs now, and replaces the summary,
-- length, thumbnail and extraction verdict, then records how it went. What the reader did with
-- the article (ratings, deliveries) is not touched.
CREATE TABLE refresh_requests (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    article_id TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'done', 'failed')),
    error TEXT NOT NULL DEFAULT '',
    done_at TEXT
);
CREATE INDEX refresh_requests_user ON refresh_requests (user_id, requested_at);
