-- The model is required now: there is no switch to run without one.
DELETE FROM settings WHERE key = 'llm.enabled';
