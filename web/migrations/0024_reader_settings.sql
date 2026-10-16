-- What was an admin's setting for the whole install and is a matter of each reader's taste or
-- device is now each reader's own, starting from the value the install had.
ALTER TABLE users ADD COLUMN format TEXT NOT NULL DEFAULT 'issue' CHECK (format IN ('issue', 'articles'));
ALTER TABLE users ADD COLUMN grayscale INTEGER NOT NULL DEFAULT 1;
ALTER TABLE users ADD COLUMN explore INTEGER NOT NULL DEFAULT 1 CHECK (explore IN (0, 1));
ALTER TABLE users ADD COLUMN most_per_subject INTEGER NOT NULL DEFAULT 2 CHECK (most_per_subject BETWEEN 0 AND 20);
ALTER TABLE users ADD COLUMN show_reasons INTEGER NOT NULL DEFAULT 1;
ALTER TABLE users ADD COLUMN learn INTEGER NOT NULL DEFAULT 1;

UPDATE users SET format = (SELECT value FROM settings WHERE key = 'format')
 WHERE EXISTS (SELECT 1 FROM settings WHERE key = 'format' AND value IN ('issue', 'articles'));
UPDATE users SET grayscale = (SELECT value IN ('1', 'true') FROM settings WHERE key = 'images.grayscale')
 WHERE EXISTS (SELECT 1 FROM settings WHERE key = 'images.grayscale');
UPDATE users SET explore = (SELECT CAST(value AS INTEGER) > 0 FROM settings WHERE key = 'llm.explore_slots')
 WHERE EXISTS (SELECT 1 FROM settings WHERE key = 'llm.explore_slots');
UPDATE users SET most_per_subject = (SELECT MIN(MAX(CAST(value AS INTEGER), 0), 20) FROM settings WHERE key = 'llm.most_per_subject')
 WHERE EXISTS (SELECT 1 FROM settings WHERE key = 'llm.most_per_subject');
UPDATE users SET show_reasons = (SELECT value IN ('1', 'true') FROM settings WHERE key = 'llm.show_reasons')
 WHERE EXISTS (SELECT 1 FROM settings WHERE key = 'llm.show_reasons');
UPDATE users SET learn = (SELECT value IN ('1', 'true') FROM settings WHERE key = 'llm.learn')
 WHERE EXISTS (SELECT 1 FROM settings WHERE key = 'llm.learn');

-- Moved to the readers, or no longer settings at all: always done.
DELETE FROM settings WHERE key IN (
    'format', 'images.grayscale', 'llm.explore_slots', 'llm.most_per_subject', 'llm.show_reasons', 'llm.learn',
    'llm.choose_issue', 'llm.group_stories', 'llm.follow_links', 'llm.check_extraction',
    'max_story_entries', 'max_article_words'
);
