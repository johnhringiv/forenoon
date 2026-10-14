-- A subscriber session belongs to a site, not to a feed: every page on the site, from any feed
-- or added by hand, is fetched with it. The cookie itself is in the logins folder as
-- `site-<domain>.json`; this only says which sites have one.
CREATE TABLE sessions (
    domain TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
ALTER TABLE sources DROP COLUMN session_domain;
