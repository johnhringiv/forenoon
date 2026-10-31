# Forenoon

A self-hosted reading digest: a daily pipeline picks articles with a model and emails them to
a Kindle as one book, and web pages behind a login show what was delivered, take ratings, and
hold every reader's sources and settings. [docs/PRD.md](docs/PRD.md) describes what it does
today; it is the spec, so a change in behaviour updates it in the same PR.

## Layout

- `app/`: the pipeline (Python): `cli.py` (commands and the scheduler), `pipeline.py` (one
  reader's run), `rank.py`/`read.py`/`choose.py`/`learn.py`/`review.py` (the model's calls),
  `llm.py` and `models.py` (connections and model profiles), `render.py` (the books),
  `store.py` (the pipeline's tables), `config.py` (settings), `extract/` (the article from a
  fetched page: chosen, cleaned and reduced to an allowlist; its `__init__` names the modules).
- `web/`: the web pages (Rust: axum, sqlx, askama). `src/` handlers, `templates/`,
  `migrations/`, `settings.json` (the Settings page), `models.json` (the shipped models and
  prices, also read by the pipeline), `tests/web.rs`.
- `browser/`: the session browser container (Chrome on Xvfb) that fetches pages from sites
  the household subscribes to.
- `deploy/`: `docker-compose.yml`, and `deploy.sh`/`poll-deploy.sh` for plain `docker run`.
- `tests/`: the pipeline's tests (pytest).

## Checks

- Python: `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest -q`.
- Rust, in `web/`: `cargo fmt --check`,
  `SQLX_OFFLINE=true cargo clippy --all-targets --locked -- -D warnings`,
  `SQLX_OFFLINE=true cargo test`.
- Markdown is formatted with prettier 3 (CI checks it; `.githooks/pre-commit` does it locally).

## The database

One SQLite file that both programs open.

- The web app owns the schema: `web/migrations/`, applied when it starts. The pipeline's own
  tables are in `app/store.py` and copied to `web/pipeline-schema.sql` (a test keeps the two
  equal).
- After changing a query or a migration, run `sh web/dev-db.sh` (it needs sqlite3 and
  sqlx-cli) and commit the refreshed `web/.sqlx/`; the build checks queries against it.
- The Python tests apply the web app's migrations, so a column the web app renames or drops
  fails there too.
- There is one install per household, and no code for older schemas: a migration brings the
  data forward instead.

## Settings and the environment

- Admin settings are listed in `web/settings.json` and read by `app/config.py`
  (`setting_keys`); a test checks they agree. A setting exists only when it controls cost,
  depends on the deploy, or is a real choice; anything else is how Forenoon works, described
  in the PRD.
- Each reader's own settings are columns on `users`, set on their Reading and Account pages,
  and applied to their run by `config.for_reader`.
- Deploy settings and secrets come only from the environment. Both programs read the same
  variable names. A new variable goes into `deploy/docker-compose.yml`, `deploy/deploy.sh`
  and `deploy/.env.example` together. Give it a default there rather than an empty value: the
  web binary refuses an empty boolean.

## Fetched content

Feed entries and pages are untrusted. The model gets them as delimited data with an
instruction to ignore anything in them that reads as an instruction, and nothing in them can
add a recipient or change a setting.

## Books for the Kindle

Before changing how books are built (`app/render.py`, the stylesheet, fonts, images), read
[docs/kindle-books.md](docs/kindle-books.md): what has broken on the device and the rules that
came from it. A book that passes epubcheck can still break on the Kindle.

Amazon's reference for what the Kindle supports is the
[Kindle Publishing Guidelines](https://kindlegen.s3.amazonaws.com/AmazonKindlePublishingGuidelines.pdf)
(version 2026.3 checked on 14 October 2026). Check a CSS property's supported values there
before relying on it: Enhanced Typesetting accepts only some values, for example
`hyphens: auto | manual` (not `none`) and `white-space: nowrap | normal` (not `pre-wrap`).
