-- Whether the daily issue is emailed to the reader's Kindle. Off, it is read on the web pages
-- only, while Send to Kindle still works for what they pick there.
ALTER TABLE users ADD COLUMN daily_to_kindle INTEGER NOT NULL DEFAULT 1 CHECK (daily_to_kindle IN (0, 1));
