-- An admin invites a reader by email. The account exists from then on, with no password and
-- `invited_at` set, until the reader opens the emailed link and chooses one; the pipeline
-- delivers to no account that is still invited. NULL for every account that is in use.
ALTER TABLE users ADD COLUMN invited_at TEXT;
-- The emailed links gain a purpose. SQLite cannot change a CHECK in place.
CREATE TABLE email_tokens_new (
    token_hash TEXT PRIMARY KEY NOT NULL,
    user_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('verify', 'reset', 'invite')),
    email TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
INSERT INTO email_tokens_new SELECT token_hash, user_id, purpose, email, expires_at, created_at FROM email_tokens;
DROP TABLE email_tokens;
ALTER TABLE email_tokens_new RENAME TO email_tokens;
