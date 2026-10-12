-- Sign-in is by email address. An address works before it is confirmed ("soft"
-- verification), but a forgotten password can be reset only through a confirmed one.
ALTER TABLE users ADD COLUMN email TEXT;             -- lowercase; NULL on an account made before this
ALTER TABLE users ADD COLUMN email_verified_at TEXT;
ALTER TABLE users ADD COLUMN pending_email TEXT;     -- a requested change, until its link is opened
CREATE UNIQUE INDEX users_email ON users (email);

-- Links sent by email. Only the SHA-256 of a token is kept, so a copy of the database cannot
-- be used to confirm an address or reset a password.
CREATE TABLE email_tokens (
    token_hash TEXT PRIMARY KEY NOT NULL,
    user_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('verify', 'reset')),
    email TEXT NOT NULL,                             -- the address the link was sent to
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
