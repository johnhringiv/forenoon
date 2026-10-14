-- What the pipeline has learned about each reader's taste from their ratings: a short note it
-- keeps up to date and gives to the model beside the reader's own interests. The reader can
-- read and edit it on the account page; every version is kept, whoever wrote it.
ALTER TABLE users ADD COLUMN learned TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN learned_at TEXT;
CREATE TABLE profile_history (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    notes TEXT NOT NULL,
    author TEXT NOT NULL CHECK (author IN ('agent', 'reader')),
    created_at TEXT NOT NULL
);
CREATE INDEX profile_history_user ON profile_history (user_id, id);
