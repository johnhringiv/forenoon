-- A feed can be link-only: its pages are never fetched, and what the ranking picks from it is
-- shown on the newsstand as a link instead of being sent. SQLite cannot widen a CHECK, so the
-- table is rebuilt.
CREATE TABLE sources_new (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,                       -- also how articles and health records refer to it
    kind TEXT NOT NULL DEFAULT 'rss' CHECK (kind IN ('rss', 'recipe', 'builtin')),
    url TEXT NOT NULL DEFAULT '',                    -- the feed, for kind 'rss'
    recipe TEXT NOT NULL DEFAULT '',                 -- the folder under recipes/, for kind 'recipe'
    recipe_title TEXT NOT NULL DEFAULT '',           -- the Calibre recipe run into that folder
    builtin TEXT NOT NULL DEFAULT '',                -- the pipeline's key, for kind 'builtin'
    extract TEXT NOT NULL DEFAULT 'auto' CHECK (extract IN ('auto', 'page', 'feed', 'link')),
    pinned INTEGER NOT NULL DEFAULT 0,               -- its articles are taken before all others
    enabled INTEGER NOT NULL DEFAULT 1,              -- a paused source is kept but not read
    created_at TEXT NOT NULL,
    check_state TEXT NOT NULL DEFAULT '',            -- '', 'pending', 'ok' or 'failed'
    check_note TEXT NOT NULL DEFAULT '',
    session_domain TEXT NOT NULL DEFAULT ''          -- a feed's subscriber session is for this site
);
INSERT INTO sources_new (id, name, kind, url, recipe, recipe_title, builtin, extract, pinned, enabled,
                         created_at, check_state, check_note, session_domain)
    SELECT id, name, kind, url, recipe, recipe_title, builtin, extract, pinned, enabled,
           created_at, check_state, check_note, session_domain FROM sources;
DROP TABLE sources;
ALTER TABLE sources_new RENAME TO sources;
CREATE UNIQUE INDEX sources_builtin ON sources (builtin) WHERE kind = 'builtin';
