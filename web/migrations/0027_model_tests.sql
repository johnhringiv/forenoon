-- The Settings page's Test button: the page asks for a test of the models the settings name,
-- and the pipeline, which holds the connections and keys, runs it within a minute and writes
-- the result here. One row: a new request replaces the last.
CREATE TABLE model_tests (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    requested_at TEXT NOT NULL,
    answered TEXT NOT NULL DEFAULT '',  -- the requested_at it answered; behind it while one is waiting
    started TEXT NOT NULL DEFAULT '',   -- the requested_at the pipeline has begun on
    tested_at TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT ''     -- a line per model
);
