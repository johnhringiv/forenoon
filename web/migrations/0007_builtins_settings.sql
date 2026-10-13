-- Sources come in three kinds now: built-in (front pages the pipeline knows how to read,
-- only ever switched on or off), feeds, and Calibre recipes. SQLite cannot widen a CHECK, so
-- the table is rebuilt.
CREATE TABLE sources_new (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,                       -- also how articles and health records refer to it
    kind TEXT NOT NULL DEFAULT 'rss' CHECK (kind IN ('rss', 'recipe', 'builtin')),
    url TEXT NOT NULL DEFAULT '',                    -- the feed, for kind 'rss'
    recipe TEXT NOT NULL DEFAULT '',                 -- the folder under recipes/, for kind 'recipe'
    recipe_title TEXT NOT NULL DEFAULT '',           -- the Calibre recipe run into that folder
    builtin TEXT NOT NULL DEFAULT '',                -- the pipeline's key, for kind 'builtin'
    extract TEXT NOT NULL DEFAULT 'auto' CHECK (extract IN ('auto', 'page', 'feed')),
    pinned INTEGER NOT NULL DEFAULT 0,               -- its articles are taken before all others
    enabled INTEGER NOT NULL DEFAULT 1,              -- a paused source is kept but not read
    created_at TEXT NOT NULL,
    check_state TEXT NOT NULL DEFAULT '',            -- '', 'pending', 'ok' or 'failed'
    check_note TEXT NOT NULL DEFAULT ''
);
INSERT INTO sources_new (id, name, kind, url, recipe, extract, pinned, enabled, created_at, check_state, check_note)
    SELECT id, name, kind, url, recipe, extract, pinned, enabled, created_at, check_state, check_note FROM sources;
DROP TABLE sources;
ALTER TABLE sources_new RENAME TO sources;
CREATE UNIQUE INDEX sources_builtin ON sources (builtin) WHERE kind = 'builtin';

-- The two built-ins there are today start switched on; the pipeline adds any later one
-- switched off.
INSERT OR IGNORE INTO sources (name, kind, builtin, enabled, created_at) VALUES
    ('Hacker News', 'builtin', 'hackernews', 1, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    ('Lobsters', 'builtin', 'lobsters', 1, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'));

-- What an admin changed on the Settings page. A key that is absent has the pipeline's own
-- default; these were config.yaml.
CREATE TABLE settings (
    key TEXT PRIMARY KEY,                            -- 'delivery_time', 'llm.model'
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
) WITHOUT ROWID;
