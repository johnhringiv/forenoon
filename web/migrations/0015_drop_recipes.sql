-- Calibre recipes are gone: every source is a feed or a built-in front page. Any recipe row
-- left is removed (articles delivered from it keep their source's name), and the table is
-- rebuilt without the recipe columns, since SQLite cannot narrow a CHECK.
DELETE FROM sources WHERE kind = 'recipe';
CREATE TABLE sources_new (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,                       -- also how articles and health records refer to it
    kind TEXT NOT NULL DEFAULT 'rss' CHECK (kind IN ('rss', 'builtin')),
    url TEXT NOT NULL DEFAULT '',                    -- the feed, for kind 'rss'
    builtin TEXT NOT NULL DEFAULT '',                -- the pipeline's key, for kind 'builtin'
    extract TEXT NOT NULL DEFAULT 'auto' CHECK (extract IN ('auto', 'page', 'feed', 'link')),
    pinned INTEGER NOT NULL DEFAULT 0,               -- its articles are taken before all others
    enabled INTEGER NOT NULL DEFAULT 1,              -- a paused source is kept but not read
    created_at TEXT NOT NULL,
    check_state TEXT NOT NULL DEFAULT '',            -- '', 'pending', 'ok' or 'failed'
    check_note TEXT NOT NULL DEFAULT ''
);
INSERT INTO sources_new (id, name, kind, url, builtin, extract, pinned, enabled, created_at, check_state, check_note)
    SELECT id, name, kind, url, builtin, extract, pinned, enabled, created_at, check_state, check_note FROM sources;
DROP TABLE sources;
ALTER TABLE sources_new RENAME TO sources;
CREATE UNIQUE INDEX sources_builtin ON sources (builtin) WHERE kind = 'builtin';
