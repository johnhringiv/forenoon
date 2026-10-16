-- Found articles are ranked with the feeds' on their score alone, so the two settings that
-- kept places for them and set the score they needed are gone.
DELETE FROM settings WHERE key IN ('discovery.slots', 'discovery.min_score');

-- Instead, every source has its own limit on the day's places, found articles' too. 0 is none.
ALTER TABLE sources ADD COLUMN daily_max INTEGER NOT NULL DEFAULT 2;
