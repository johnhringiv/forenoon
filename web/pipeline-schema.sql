-- The tables the Python pipeline owns, copied from app/store.py (a test keeps the two equal).
-- The web app reads them, and writes only ratings and feedback_events.
CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    published TEXT,
    first_seen TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    reason TEXT NOT NULL DEFAULT '',
    word_count INTEGER NOT NULL DEFAULT 0,
    image_url TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS deliveries (
    user_id TEXT NOT NULL,
    article_id TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    story TEXT NOT NULL DEFAULT '',
    link_only INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, article_id)
);
CREATE TABLE IF NOT EXISTS feedback_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    article_id TEXT NOT NULL,
    signal TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ratings (
    user_id TEXT NOT NULL,
    article_id TEXT NOT NULL,
    rating INTEGER,
    finished INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, article_id)
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cost REAL
);
CREATE TABLE IF NOT EXISTS selections (
    user_id TEXT NOT NULL,
    article_id TEXT NOT NULL,
    selected_at TEXT NOT NULL,
    policy TEXT NOT NULL,
    score REAL,
    reason TEXT NOT NULL DEFAULT '',
    rank INTEGER,
    explore INTEGER NOT NULL DEFAULT 0,
    propensity REAL,
    model TEXT NOT NULL DEFAULT '',  -- the model that scored it, named with its connection
    PRIMARY KEY (user_id, article_id)
);
CREATE TABLE IF NOT EXISTS extraction_reviews (
    id INTEGER PRIMARY KEY,
    article_id TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    verdict TEXT NOT NULL,
    problem TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS source_health (
    source TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS http_cache (
    url TEXT PRIMARY KEY,
    etag TEXT,
    last_modified TEXT
);
CREATE TABLE IF NOT EXISTS article_routes (
    article_id TEXT NOT NULL,
    route TEXT NOT NULL,
    via TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (article_id, route, via)
);
CREATE TABLE IF NOT EXISTS assessments (
    user_id TEXT NOT NULL,
    article_id TEXT NOT NULL,
    assessed_at TEXT NOT NULL,
    score REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    read INTEGER NOT NULL DEFAULT 0,
    lasting TEXT NOT NULL DEFAULT '',
    deferred_at TEXT NOT NULL DEFAULT '',  -- left out of an issue for variety, when
    PRIMARY KEY (user_id, article_id)
);
CREATE TABLE IF NOT EXISTS book_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    model TEXT NOT NULL,
    verdict TEXT NOT NULL,  -- ok, fixed, cannot_fix, incomplete or error
    summary TEXT NOT NULL DEFAULT '',
    fixes TEXT NOT NULL DEFAULT '[]',  -- JSON: [{"kind", "what", "why"}], as the model gave them
    edits TEXT NOT NULL DEFAULT '[]',  -- JSON: [{"tool", "first", "last", "before", "after"}]
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0
);
