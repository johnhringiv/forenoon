-- Accounts for the web pages. `id` is the reader's id in the pipeline's config, so an
-- account sees that reader's deliveries and ratings.
CREATE TABLE users (
    id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'reader' CHECK (role IN ('admin', 'reader')),
    timezone TEXT NOT NULL DEFAULT 'UTC',
    created_at TEXT NOT NULL
);
