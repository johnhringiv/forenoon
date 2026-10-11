# Forenoon: product requirements (current behaviour)

This document describes what Forenoon does today. It is written from the code and is the
reference for existing behaviour; planned work is listed at the end and is not built.

## 1. Purpose

Forenoon is a self-hosted reading digest for e-readers. Each morning it reads the feeds its
owner configured, extracts the new articles, and emails them to the owner's Kindle as one
book. Each article ends with rating links; ratings are recorded for later use in ranking.

It runs as containers on a home server. It is not a hosted service: each install fetches only
the feeds its own users configure and delivers only to the addresses in its own config.

## 2. Users and configuration

- An install has one or more **users**, each with an id, a name, one or more Send to Kindle
  addresses and a daily article cap (default 8).
- **Sources** are RSS or Atom feeds. Each has a name, a URL and optional settings:
  - `pinned`: its articles are taken before all other sources.
  - `extract`: `auto` (default), `page` (always use the web page) or `feed` (use only the
    feed's own text; the article page is never fetched).
  - `users`: restrict the source to some users; otherwise every user gets it.
- Configuration lives in `config.yaml` and `sources.yaml`. Unknown keys are rejected at
  start-up. Secrets (`FORENOON_FEEDBACK_SECRET`, `FORENOON_SMTP_PASSWORD`) come only from the
  environment.

## 3. The daily run

A scheduler runs once a day at `delivery_time` in the configured time zone. `forenoon run`
does the same on demand. For each user:

1. **Read feeds.** Each source is fetched with conditional requests (ETag / Last-Modified);
   an unchanged feed is skipped. A response that is not a feed, or any status other than 200
   or 304, counts as a failed source for this run.
2. **Select candidates.** Items older than `max_age_days` (default 3) are ignored, as are
   items already delivered to this user and items whose extraction previously failed. At most
   5 items per source are considered per run.
3. **Order.** Pinned sources first, then the rest, each group taken round-robin across
   sources with the newest item of each source first. There is no ranking yet.
4. **Cap.** Articles are processed in that order until the user's daily cap is reached. The
   cap counts articles delivered during the local calendar day, so a second run on the same
   day only sends what is left of the cap.
5. **Extract, render, deliver** (sections 4 to 6).
6. **Report.** A Markdown and JSON report is written to `reports/` in the data folder.

A run is safe to repeat: an article is recorded as delivered only after the email carrying it
was accepted by the relay, and a delivered article is never sent to the same user again.

## 4. Extraction

All fetched content is treated as untrusted.

- **Fetching.** Only public `http`/`https` hosts are fetched, on every redirect hop; private
  and loopback addresses are refused. Responses are size-capped (feeds 32 MiB, pages and
  images 12 MiB), requests to one host are spaced at least one second apart, and at most five
  redirects are followed.
- **Choosing the body.** The article page is reduced to its main content. If the feed carries
  the article's text and it has at least 85% of the page's word count, the feed's copy is
  used, since it is the author's own clean version.
- **Sanitising.** The body is reduced to an allowlist of elements (paragraphs, headings,
  lists, quotes, code, tables, figures, images, links and inline emphasis). Scripts, styles,
  forms, embeds, video, navigation and footers are removed. All attributes are dropped except
  link targets, image sources and alt text, table spans and list start numbers. Links and
  images must be absolute `http(s)` (or `mailto:` / embedded image data). Top-level headings
  become second-level, invalid nesting is repaired, and containers left empty are removed.
- **Lead paragraph.** If the page's description is visible on the page but missing from the
  extracted body (a standfirst in the page header), it is added in italics at the top.
- **Link cards.** An embedded "bookmark" link (a whole block wrapped in one link) is rebuilt
  as a bordered card: linked title, up to two lines of description, and the site's host name.
- **Verdict.** Each article is `clean`, `degraded` or `failed`, with reasons:
  - failed: fewer than 40 words, or nothing extracted;
  - degraded: much shorter than the page (under 60%), under 150 words, a paywall marker, a
    cookie or bot-wall marker, or an ending that looks truncated.
    Degraded articles are still delivered; failed ones are not and are not retried. A page
    that could not be fetched is retried on later runs.
- **Preview data.** The article's lead image URL (`og:image`, else the first image in the
  body) and a description (the page's, else the opening text, up to 400 characters) are
  stored for a future listing page. The image is not downloaded for this.

## 5. Images

Images in the body are downloaded and embedded: at most 40 per article and 8 MiB per article
after conversion. Each is flattened onto white (so transparent images stay readable),
converted to greyscale by default, scaled to fit 1200×1600 and saved as JPEG. Images smaller
than 48 pixels on a side (tracking pixels, icons) and any that cannot be fetched or decoded
are dropped and counted in the report.

## 6. The book

- **Format `issue` (default).** One EPUB per day, titled `Forenoon · YYYY-MM-DD`:
  - a contents page with the date, article count and total reading time, then each article's
    title, source and reading time;
  - one chapter per article: title, author, source, date, reading time, a link to the
    original, the body, then the rating links; a "Contents" link at the top and bottom;
  - a navigation table so the e-reader's own "go to" menu lists every article.
- **Format `articles`.** One EPUB per article, titled `FN YYYY-MM-DD · Source · Title`.
- Output is EPUB 3 with an NCX for older converters, and is byte-for-byte reproducible for
  the same input.

### Size limits

Email size is capped by `smtp.max_message_bytes` (default 45 MiB) and
`smtp.max_attachments` (25). If one article alone would exceed the limit its images are
dropped and it is marked degraded; if it still does not fit it is skipped and marked failed.
A day's issue that would exceed the limit is split into parts (`· part 1`, `· part 2`).
Emails are sent one at a time and their articles recorded as each succeeds, so a failure
part-way never causes a resend.

## 7. Ratings

- Each article ends with two rows of five links:
  - "Finished? Rate it:" 1 2 3 4 5
  - "Didn't finish? Rate it:" 1 2 3 4 5

  One tap records both the rating and whether the article was finished.

- Each link carries a signed token (HMAC-SHA256, truncated to 16 bytes) naming the user,
  article, signal and expiry (default 60 days after delivery).
- The **feedback service** is the only part exposed to the internet. It accepts
  `GET /f/<token>`, verifies the token, records the tap and returns a small page stating what
  is now stored ("Rated 4 of 5, finished.") with links to change it. It has no login and takes
  no other input. `GET /healthz` returns `ok`.
- **Latest tap wins** for both rating and finished. Every tap is also kept in an event log.
- Links in articles delivered before ratings existed still work: "more like this" counts as
  5, "less like this" as 1, "finished" sets the finished flag only.
- Requests are rate limited to 30 per minute per client address, taken from Cloudflare's
  `CF-Connecting-IP` header.
- `forenoon feedback-log` prints the event log and the current rating per article.

Ratings are stored but do not yet influence what is delivered.

## 8. Delivery

Books are emailed as attachments through an SMTP relay (STARTTLS, with login) to the user's
Send to Kindle addresses. Recipients come only from the user's entry in `config.yaml`;
nothing in a feed or article can add or change a recipient. The sender address must be on
the Amazon account's approved sender list.

## 9. Reports and alerts

- **Run report.** Per source: a verdict (clean / degraded / failed / no items), counts, and
  the most common reason; per article: status, word count, extraction method, images dropped
  and reason. Also the number delivered and any delivery error.
- **Triage.** `forenoon report` extracts the latest items of every source without delivering
  or recording anything.
- **Alerts.** When `alerts.to` is set, the operator is emailed if a delivery fails, a run
  crashes, a run has nothing new to deliver (unless the day's cap was already used), or a
  source has failed a set number of runs in a row (default 3, repeated at each multiple).
  Alerts use the same relay; a failure to send one is logged and does not affect the run.

## 10. Commands

| Command                                         | Purpose                                                      |
| ----------------------------------------------- | ------------------------------------------------------------ |
| `forenoon run [--dry-run] [--out DIR] [--user]` | One delivery now; `--dry-run` builds files and sends nothing |
| `forenoon schedule`                             | Run every day at `delivery_time`                             |
| `forenoon report [--per-source N]`              | Triage sources                                               |
| `forenoon feedback [--host] [--port]`           | Serve the feedback endpoint                                  |
| `forenoon feedback-log`                         | Print taps and current ratings                               |
| `forenoon golden [--out DIR]`                   | Write the rendering-test book                                |

## 11. Storage

One SQLite database in the data folder, shared by the pipeline and the feedback service:
articles (with extraction verdict and preview data), deliveries, the tap event log, current
ratings, per-source failure counts and feed cache validators. Older databases are upgraded in
place on start-up. Backing up the data folder backs up everything.

## 12. Deployment

- One image, published to GHCR by CI on every push to `main`. Containers run as UID 1000
  with a read-only root filesystem and no added capabilities.
- Three containers: the scheduler, the feedback service, and a `cloudflared` tunnel that
  shares the feedback container's network namespace. No host port is published.
- `deploy/docker-compose.yml`, or `deploy/deploy.sh` with plain `docker run`.
  `deploy/poll-deploy.sh` redeploys when the app image changes, replaces only the tunnel
  when just its image changed, and restarts the stack after a reboot.
- The Kindle browser needs TLS 1.2, so the feedback hostname's zone must not require 1.3.

See `docs/setup-deploy.md` and `docs/setup-email.md`.

## 13. Security properties

- Fetched content cannot choose recipients, run code, or reach private network addresses.
- The public endpoint accepts only signed, expiring tokens and writes only ratings.
- Secrets are never read from or written to config files, reports or logs.
- The address check on fetches resolves names separately from the connection, so it does not
  stop DNS rebinding; the containers should not sit on a network with sensitive services.

## 14. Verified and not verified

- Verified on a Kindle Oasis (October 2026): delivery through Send to Kindle, rendering of
  text, headings, code, tables, lists, images including transparent ones, and rating taps
  through the tunnel.
- Embedded fonts appear to be dropped by Send to Kindle's conversion; code uses the device's
  monospace font.
- Not yet checked on a device: the daily issue's contents page and chapter navigation, and
  the link card's appearance.

## 15. Not built yet

- Ranking of any kind; ratings are only stored.
- LLM use: ranking or summaries with Claude under a daily token cap, then a local model.
- Discovery from Hacker News, Lobsters and similar; a watch list for vendor announcements.
- A web interface for sources, preferences and a "newsstand" of delivered articles.
- Sources other than RSS/Atom (newsletters by email, Calibre news recipes).
- E-readers other than Kindle.
