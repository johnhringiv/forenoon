-- Each reader follows their own sources. A source belongs to the household: added by one
-- reader, private to them until shared, and read for everyone who follows it. Pinning is the
-- follower's, not the source's.
CREATE TABLE follows (
    user_id TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,               -- its articles are taken before all others, for this reader
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, source_id)
);
CREATE INDEX follows_source ON follows (source_id);
ALTER TABLE sources ADD COLUMN added_by TEXT NOT NULL DEFAULT '';  -- the account that added it; '' for a built-in or one from before this
ALTER TABLE sources ADD COLUMN shared INTEGER NOT NULL DEFAULT 0;  -- listed for the rest of the household to follow
-- What is there today is the household's list: shared, and followed by every reader as it was pinned.
UPDATE sources SET shared = 1;
INSERT INTO follows (user_id, source_id, pinned, created_at)
    SELECT users.id, sources.id, sources.pinned, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now') FROM users, sources;
ALTER TABLE sources DROP COLUMN pinned;
-- Who pasted a session, so that only they or an admin remove it.
ALTER TABLE sessions ADD COLUMN saved_by TEXT NOT NULL DEFAULT '';
