-- A model is named with the connection it is reached through: anthropic/claude-haiku-5-5. The
-- extraction check no longer has a model of its own; it runs on the main one.
UPDATE settings SET value = 'anthropic/' || value
 WHERE key IN ('llm.model', 'llm.choose_model') AND value != '' AND instr(value, '/') = 0;
DELETE FROM settings WHERE key = 'llm.check_model';
