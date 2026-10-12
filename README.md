# Forenoon

Self-hosted reading digest for e-readers. Every morning it goes through your feeds, your
newspaper subscriptions and the front pages of Hacker News and Lobsters, has Claude pick what
you would want to read, and emails it to your Kindle as one book with a contents page. Each
article ends with rating links (1 to 5, finished or not) that feed the next day's choices.

On one measured day it read 23 sources including a whole Financial Times, ranked about 165
candidate articles against a plain-English description of the reader's interests, read the
best 40 in full, and delivered 16 stories (25 articles) in 80 seconds, for **about two cents**
of Claude Haiku. With no API key it still works: pinned sources first, then the newest
articles round-robin across sources.

- **Stories, not links.** A short post that only points at an article is replaced by the
  article. Several reports of one event share a slot.
- **Subscriptions.** Publications you pay for come in through Calibre news recipes, with
  your login kept in one isolated container.
- **Yours.** One SQLite file and a folder on your own machine. The only public endpoint
  accepts signed rating links and nothing else.

What it does today is described in [docs/PRD.md](docs/PRD.md).

## Quick start (local)

```sh
uv sync
mkdir run && cp deploy/config.example.yaml run/config.yaml
cp deploy/sources.example.yaml run/sources.yaml
export FORENOON_FEEDBACK_SECRET=$(openssl rand -hex 32)

uv run forenoon --config run/config.yaml report               # per-source extraction report
uv run forenoon --config run/config.yaml run --dry-run --out run/out   # build EPUBs, send nothing
uv run forenoon golden --out run/out                           # rendering test file for the device
```

Then fill in `users`, `smtp` and `public_base_url` in `run/config.yaml`, set
`FORENOON_SMTP_PASSWORD`, and run `forenoon run` to deliver for real.

| Command                                            | What it does                                                                                                               |
| -------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `forenoon report`                                  | Extracts the latest items of every source and prints clean / degraded / failed with reasons. Delivers and records nothing. |
| `forenoon run [--dry-run] [--out DIR] [--user ID]` | One delivery now. Safe to rerun: nothing is sent twice and the daily cap holds.                                            |
| `forenoon schedule`                                | Runs `forenoon run` every day at `delivery_time`. This is the container's default command.                                 |
| `forenoon feedback`                                | Serves the public feedback endpoint (`/f/<token>`, `/healthz`).                                                            |
| `forenoon feedback-log`                            | Prints the logged feedback.                                                                                                |
| `forenoon golden`                                  | Writes a test EPUB with code, tables, lists and an image.                                                                  |

## Deploy

`deploy/docker-compose.yml` runs the pipeline, the feedback endpoint and (optionally) a
Cloudflare Tunnel. Setup guides:

- [docs/setup-email.md](docs/setup-email.md): SMTP relay and Amazon's approved sender list
- [docs/setup-deploy.md](docs/setup-deploy.md): compose on a NAS, Cloudflare Tunnel, backups
- [docs/setup-recipes.md](docs/setup-recipes.md): subscriber publications through Calibre news recipes (optional)

## Security notes

- All fetched content is untrusted. Article HTML is reduced to an allowlist of elements and
  attributes; scripts, iframes, forms, styles and remote resources never reach the EPUB.
  Images are re-encoded and embedded.
- Recipients come only from `config.yaml`. Nothing in a feed or article can choose one.
- The fetcher refuses non-public addresses, on redirects too. This check does not stop DNS
  rebinding, so do not give the container network access to anything sensitive.
- Feedback links carry an HMAC-signed, expiring token. The endpoint needs no login, is
  idempotent and is rate limited.

## Development

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
git config core.hooksPath .githooks   # once: prettier-formats staged Markdown on commit
```
