-- What the pipeline needs to know about a reader, edited on the account page. These were
-- the `users` entries of config.yaml.
ALTER TABLE users ADD COLUMN kindle_addresses TEXT NOT NULL DEFAULT ''; -- one per line
ALTER TABLE users ADD COLUMN daily_stories INTEGER NOT NULL DEFAULT 8;
ALTER TABLE users ADD COLUMN interests TEXT NOT NULL DEFAULT '';
