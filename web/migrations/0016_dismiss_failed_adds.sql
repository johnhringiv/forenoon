-- A link that could not be added can be cleared from the Archive page: it stays on record,
-- marked dismissed, and is no longer shown.
ALTER TABLE add_requests ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0;
