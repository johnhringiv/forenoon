-- What the pipeline reads from: feeds and Calibre recipes. These were sources.yaml.
CREATE TABLE sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,                       -- also how articles and health records refer to it
    kind TEXT NOT NULL DEFAULT 'rss' CHECK (kind IN ('rss', 'recipe')),
    url TEXT NOT NULL DEFAULT '',                    -- the feed, for kind 'rss'
    recipe TEXT NOT NULL DEFAULT '',                 -- the folder under recipes/, for kind 'recipe'
    extract TEXT NOT NULL DEFAULT 'auto' CHECK (extract IN ('auto', 'page', 'feed')),
    pinned INTEGER NOT NULL DEFAULT 0,               -- its articles are taken before all others
    enabled INTEGER NOT NULL DEFAULT 1,              -- a paused source is kept but not read
    created_at TEXT NOT NULL
);
