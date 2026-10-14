-- Found articles are ranked with the feeds' on their score alone, so the two settings that
-- kept places for them and set the score they needed are gone.
DELETE FROM settings WHERE key IN ('discovery.slots', 'discovery.min_score');
