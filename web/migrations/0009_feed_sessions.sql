-- A feed of a site the reader subscribes to can carry a session: the site's cookie, kept in
-- the logins volume (never here), with which the article pages the ranking wants are fetched.
-- This column says there is one and names the only site it may be sent to.
ALTER TABLE sources ADD COLUMN session_domain TEXT NOT NULL DEFAULT '';
