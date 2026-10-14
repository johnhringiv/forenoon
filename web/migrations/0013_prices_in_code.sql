-- Model prices are kept in the pipeline's code, by model, so the two price settings are gone.
DELETE FROM settings WHERE key IN ('llm.input_price', 'llm.output_price');
