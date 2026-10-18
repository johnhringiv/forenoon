-- Ratings are a verdict and how far the reader got, not 1 to 5 (the pipeline brings its own
-- ratings table forward: app/store.py). A link kept as read now carries a verdict: a 5 is
-- "more like this", a 4 "worth it", a 2 or 1 "not good", and a 3 none (as app/store.py does).
CREATE TABLE add_requests_verdicts (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    url TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('next', 'now', 'keep')),
    verdict TEXT CHECK (verdict IN ('not-for-me', 'not-good', 'worth-it', 'more')),
    requested_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'done', 'failed')),
    error TEXT NOT NULL DEFAULT '',
    article_id TEXT NOT NULL DEFAULT '',
    done_at TEXT,
    dismissed INTEGER NOT NULL DEFAULT 0
);
INSERT INTO add_requests_verdicts (id, user_id, url, action, verdict, requested_at, status, error, article_id, done_at, dismissed)
SELECT id, user_id, url, action,
       CASE WHEN rating = 5 THEN 'more' WHEN rating = 4 THEN 'worth-it' WHEN rating <= 2 THEN 'not-good' END,
       requested_at, status, error, article_id, done_at, dismissed
  FROM add_requests;
DROP TABLE add_requests;
ALTER TABLE add_requests_verdicts RENAME TO add_requests;
CREATE INDEX add_requests_user ON add_requests (user_id, requested_at);
CREATE INDEX add_requests_article ON add_requests (article_id, user_id);
