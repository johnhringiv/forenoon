-- A source's address is checked soon after it is added or saved: the pipeline's scheduler
-- fetches it, finds the feed when a site's address was given, and writes back what it found.
ALTER TABLE sources ADD COLUMN check_state TEXT NOT NULL DEFAULT '';  -- '', 'pending', 'ok' or 'failed'
ALTER TABLE sources ADD COLUMN check_note TEXT NOT NULL DEFAULT '';
