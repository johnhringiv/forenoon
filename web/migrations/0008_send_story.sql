-- "Send story to Kindle": the request names any article of the story, and the pipeline sends
-- every article delivered with it as one book.
ALTER TABLE send_requests ADD COLUMN story INTEGER NOT NULL DEFAULT 0;
