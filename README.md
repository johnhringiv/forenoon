# Forenoon

**A morning paper for your e-reader, edited for one reader: you.**

Forenoon is made to cut the time you spend scrolling, find the writing you actually care
about, and make your e-reader useful for more than books. Reading on e-ink, away from a
feed, helps with the scrolling too.

Other aggregators exist. They are paid for by ads or pushed along by an agenda, and they
know far less about what you want. Forenoon runs on your own machine and has one job:
find what you would choose to read, and put it where you read best.

Every morning it reads your RSS feeds, pulls the publications you subscribe to through
Calibre recipes, and searches aggregation sites (Hacker News, Lobsters). Claude chooses from
all of it against a plain-English description of your interests, and the result is emailed
to your Kindle as one book with a contents page. Each article ends with rating links, so
you can say what you thought without leaving your e-reader, and your ratings shape the next
day's choices.

## What it does

- **Chooses by reading, not by headline.** Every candidate is ranked from its title and
  opening; the best 40 are then read in full before the day is chosen, so a teaser with a
  good title loses to an article with something in it.
- **Learns from you.** Rate 1 to 5 from the Kindle or the web pages, with an optional
  one-tap reason. Describe your interests in your own words and change them whenever you like.
- **Stories, not links.** A short post that only points at an article is replaced by the
  article. Several reports of one event share one place in the day.
- **Your subscriptions.** Publications you pay for come in through Calibre news recipes,
  chosen from a list, or through their feeds with your own subscriber session.
- **A newsstand and an archive.** Web pages show each day's issue, and everything the
  model considered and passed over, with its reasons. Filter by source, rating, date or how
  an article arrived; send anything to the Kindle as a book of its own.
- **Add your own.** Paste a link to put it in tomorrow's issue, send it now, or file
  something you have already read along with your rating.
- **Clean books.** Articles are stripped to their text and pictures, with code and tables
  kept readable on e-ink. No scripts, trackers or remote content reach the device.
- **Cheap to run.** One measured day: 23 sources including a whole Financial Times, about
  165 candidates ranked, 40 read in full, 16 stories delivered in 80 seconds, for **about
  two cents** of Claude Haiku. With no API key it still delivers: pinned sources first, then
  the newest articles in turn across sources.
- **No one else's incentives.** No ads, no engagement metrics, no account with anybody. One
  SQLite file and a folder on your own machine. The only public endpoint accepts signed
  rating links and nothing else.

Everything is set up in the browser: sources, interests, delivery time and the rest.

What it does today is described in full in [docs/PRD.md](docs/PRD.md).

## Run it locally

```sh
uv sync
mkdir -p run/data
export FORENOON_DATA_DIR=run/data FORENOON_FEEDBACK_SECRET=$(openssl rand -hex 32)

# a reader to deliver to; asks for a password for the web pages
(cd web && cargo run -- --db ../run/data/forenoon.sqlite3 add-user me --email you@example.com --kindle you@kindle.com)
# the web pages: sign in at http://127.0.0.1:8081/ and add feeds on Sources; stop with Ctrl-C
(cd web && cargo run -- --db ../run/data/forenoon.sqlite3 serve --listen 127.0.0.1:8081)

uv run forenoon report                          # per-source extraction report
uv run forenoon run --dry-run --out run/out     # build EPUBs, send nothing
uv run forenoon golden --out run/out                           # rendering test file for the device
```

To deliver for real, set the mail server in the environment (`FORENOON_SMTP_HOST`,
`FORENOON_SMTP_USERNAME`, `FORENOON_SMTP_SENDER`, `FORENOON_SMTP_PASSWORD`) and run
`forenoon run`. What belongs to the deploy is in the
environment (`deploy/.env.example` lists it all), and everything else is set on the web
pages and kept in the database.

| Command                                            | What it does                                                                                                               |
| -------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `forenoon report`                                  | Extracts the latest items of every source and prints clean / degraded / failed with reasons. Delivers and records nothing. |
| `forenoon run [--dry-run] [--out DIR] [--user ID]` | One delivery now. Safe to rerun: nothing is sent twice and the daily cap holds.                                            |
| `forenoon schedule`                                | Runs `forenoon run` every day at the delivery time. This is the container's default command.                               |
| `forenoon feedback`                                | Serves the public feedback endpoint (`/f/<token>`, `/healthz`).                                                            |
| `forenoon feedback-log`                            | Prints the logged feedback.                                                                                                |
| `forenoon send-queue`                              | Fetches the links readers added and sends what they asked for on the web pages. The scheduler does this every minute.      |
| `forenoon golden`                                  | Writes a test EPUB with code, tables, lists and an image.                                                                  |

## Deploy

`deploy/docker-compose.yml` runs the pipeline, the feedback endpoint and (optionally) a
Cloudflare Tunnel, the recipes runner and the web pages. The web pages are a separate Rust
program in `web/`, behind a login: the newsstand, the archive, sources, settings and your
account. Setup guides:

- [docs/setup-email.md](docs/setup-email.md): SMTP relay and Amazon's approved sender list
- [docs/setup-deploy.md](docs/setup-deploy.md): compose on a NAS, Cloudflare Tunnel, backups
- [docs/setup-recipes.md](docs/setup-recipes.md): subscriber publications through Calibre news recipes (optional)

## Security notes

- All fetched content is untrusted. Article HTML is reduced to an allowlist of elements and
  attributes; scripts, iframes, forms, styles and remote resources never reach the EPUB.
  Images are re-encoded and embedded.
- Recipients come only from readers' accounts, where an admin sets them. Nothing in a feed or
  article can choose one.
- The fetcher refuses non-public addresses, on redirects too. This check does not stop DNS
  rebinding, so do not give the container network access to anything sensitive.
- Feedback links carry an HMAC-signed, expiring token. The endpoint needs no login, is
  idempotent and is rate limited.
- The web pages need a login by email address (Argon2id password hashes, limited sign-in
  attempts) and show each reader only their own articles. A forgotten password is reset
  through a link emailed to a confirmed address. Keep them on a trusted network for now.

## Development

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
git config core.hooksPath .githooks   # once: prettier-formats staged Markdown on commit
```

The web pages (`web/`, Rust):

```sh
cd web
cargo test
cargo fmt --check && cargo clippy --all-targets -- -D warnings
./dev-db.sh   # after changing a query, a migration or the pipeline's schema: refreshes .sqlx
```

Their SQL is checked at build time against saved query data in `web/.sqlx`, so no database
is needed to build. `web/pipeline-schema.sql` is a copy of the pipeline's tables; a Python
test fails when it differs from `app/store.py`.
