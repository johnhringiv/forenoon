-- A built-in source carries many publishers, so it has no limit on the day's places unless one
-- is set; each feed keeps its own (2 by default).
UPDATE sources SET daily_max = 0 WHERE kind = 'builtin' AND daily_max = 2;
