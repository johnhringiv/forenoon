-- Model servers reached through the OpenAI chat completions API: OpenAI itself, or a server on
-- the network (LM Studio, Ollama, llama.cpp, vLLM). A model on one is named `<name>/<model>`.
-- Anthropic is built in and never a row here. A connection's key is never stored: it is
-- FORENOON_LLM_KEY_<NAME> in the pipeline's environment.
--
-- The web pages write the settings; the pipeline, which holds the keys, writes what it finds
-- (the status columns) and runs the tests asked for.
CREATE TABLE connections (
    name TEXT PRIMARY KEY CHECK (name GLOB '[a-z0-9]*' AND name NOT GLOB '*[^a-z0-9-]*'
                                 AND length(name) <= 32 AND name != 'anthropic'),
    address TEXT NOT NULL,              -- the API's base, as in http://192.168.1.20:8080/v1
    context_tokens INTEGER NOT NULL DEFAULT 32768 CHECK (context_tokens BETWEEN 2048 AND 2000000),
    output_tokens INTEGER NOT NULL DEFAULT 16384 CHECK (output_tokens BETWEEN 1024 AND 200000),
    timeout_seconds INTEGER NOT NULL DEFAULT 300 CHECK (timeout_seconds BETWEEN 5 AND 3600),
    free INTEGER NOT NULL DEFAULT 1 CHECK (free IN (0, 1)),             -- a server you run: no cost, outside the token cap
    reasoning_off INTEGER NOT NULL DEFAULT 1 CHECK (reasoning_off IN (0, 1)),  -- ask the server to skip reasoning
    created_at TEXT NOT NULL,
    -- Written by the pipeline:
    checked_at TEXT NOT NULL DEFAULT '',
    key_set INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',     -- why the last check failed; '' when it reached the server
    models TEXT NOT NULL DEFAULT '[]',  -- the server's model ids, a JSON array
    -- A test: asked for by the web pages, answered by the pipeline.
    test_model TEXT NOT NULL DEFAULT '',
    test_requested_at TEXT NOT NULL DEFAULT '',
    tested_at TEXT NOT NULL DEFAULT '',
    test_result TEXT NOT NULL DEFAULT '',  -- one line for the page
    CHECK (output_tokens < context_tokens)  -- the context holds the prompt too
);
