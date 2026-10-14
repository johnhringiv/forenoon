-- Links a reader adds by hand on the Archive page. The web app adds a row; the pipeline's
-- scheduler fetches the page, stores the article and records how it went. `action` says what
-- the reader wants done with it: put in their next issue, sent now as a book of its own, or
-- only kept (they have read it; `rating` is theirs when they gave one).
CREATE TABLE add_requests (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    url TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('next', 'now', 'keep')),
    rating INTEGER CHECK (rating BETWEEN 1 AND 5),
    requested_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'done', 'failed')),
    error TEXT NOT NULL DEFAULT '',
    article_id TEXT NOT NULL DEFAULT '',
    done_at TEXT
);
CREATE INDEX add_requests_user ON add_requests (user_id, requested_at);
CREATE INDEX add_requests_article ON add_requests (article_id, user_id);
