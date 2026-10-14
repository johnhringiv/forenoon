# Forenoon: product requirements (current behaviour)

This document describes what Forenoon does today. It is written from the code and is the
reference for existing behaviour; planned work is listed at the end and is not built.

## 1. Purpose

Forenoon is a self-hosted reading digest for e-readers. It exists for two reasons: its owner
was spending too much time scrolling and reading whatever a feed served up, and was not
using a Kindle because the long technical posts and news they like to read are not easily
got onto one. Other aggregators exist, but they answer to advertisers or an agenda and are
far less tailored. Forenoon answers only to its reader.

Each morning it reads the feeds, publications and front pages its owner chose, has a model
choose what that reader would want from a description of their interests and their past
ratings, and emails the result to their Kindle as one book. Each article ends with rating
links, and the ratings feed the next day's choices. Web pages show what was delivered and
what was passed over, take ratings and links added by hand, and hold all the settings.

It runs as containers on a home server. It is not a hosted service: each install fetches only
what its own users configure and delivers only to the addresses its admin has set.

## 2. Users and configuration

- An install has one or more **readers**, each with an id, a name, one or more Send to Kindle
  addresses, a daily target of stories (default 8; section 7e) and an interests text. They
  live in the database: a reader is an account of the web pages
  (section 9a), created with `forenoon-web add-user` and edited on its account page. The
  pipeline reads them at the start of every run. With no readers, or for a reader with no
  Kindle address, nothing is delivered and an alert is sent.
- **Sources** come in three kinds, each with its own section on the web pages' Sources page
  (admins only), which also shows how each source's last runs went:
  - **Built in**: front pages the pipeline knows how to read (Hacker News and Lobsters;
    section 7b). They can only be switched on or off. The list comes from the pipeline: one
    that a new version adds appears switched off, so an upgrade never changes what is
    delivered by itself.
  - **Feeds**: RSS or Atom, by address.
  - **Calibre recipes**: publications fetched whole once a day (section 7d), chosen from the
    list the recipes container publishes.

  They live in the database. The pipeline reads them at the start of every run; with no
  feeds or recipes, nothing is delivered and an alert is sent. A feed or a recipe has a name
  and:
  - pinned: its articles are taken before all other sources.
  - where the text comes from: the page, falling back to the feed (default); always the
    page; only the feed, in which case the article page is never fetched; or nowhere
    (link-only), in which case its pages are never fetched and what the ranking picks from it
    is shown on the newsstand as a link, not sent.
  - paused: kept, but not read.

  A source's address is checked within a minute of being added or saved: the scheduler
  reads the feed, or, when a site's own address was given, finds the feed (the page's own
  feed link, then a few common paths) and stores that address instead. The Sources page shows
  what it found, and reloads itself while a check is waiting. The web program never fetches
  an address itself; the pipeline does, with the fetcher that refuses private addresses.

  Articles and health records refer to a source by name, so renaming one starts its health
  record afresh.

  **Subscriber sessions.** A feed of a site the reader subscribes to can carry a session:
  the cookie the reader's own browser sends to the site, pasted once on the feed's row. (A
  username and password are not offered: the FT and the NYT both ask for a CAPTCHA at
  sign-in.) The feed is read as usual, without it.
  Only the article pages the ranking wants read (the reading pass and the day's choices) are
  then fetched with the session, a few a day and at least 20 seconds apart per site, instead
  of a whole paper. The cookie is kept in the logins volume beside recipe logins: never in the
  database or the data folder, never shown again, and used only on https pages of the one
  site named with it and its subdomains. The pipeline never holds it. It leaves a request in
  `<data>/recipes/.fetch/`, and the recipes container reads the page and leaves the answer
  there, so sessions need that container running. Pages are read by Chrome on a virtual
  screen (Xvfb), since some sites admit only a browser, and what it keeps of the session is
  in its own volume. Nothing answers a CAPTCHA: a site that asks for one, shows its paywall
  (the cookie has expired) or keeps the browser out is reported on the feed's row. When a
  session needs a fresh cookie, its picks are shown as links (step 4 of section 3), not sent as the
  feed's summary, and the run's alert email names the feed. Once a page has been fetched with
  it, the feed's row shows the day the site says its sign-in ends (for the sites whose
  sign-in cookie is known: the FT and the NYT), and the alert email says so from 14 days
  before. Pages the source links to on other sites are fetched as usual, without the
  session.

- **Settings** for the whole install (delivery time, the format, ranking and its model,
  how many places built-in sources may take, alerts, what is kept) are on the web pages'
  Settings page (admins only) and live in the database. Each has a default in the code; only
  a value that differs from it is stored, so a default that changes in a later version
  reaches installs that never touched it. The pipeline reads them before every run. A stored
  value it cannot use is logged and skipped, never fatal.
- **The deploy's own settings** come only from the environment (the `.env` file): the data
  folder, the time zone (`TZ`), the mail server (`FORENOON_SMTP_*`), the feedback service's
  public address (`FORENOON_PUBLIC_URL`) and the secrets (`FORENOON_FEEDBACK_SECRET`,
  `FORENOON_SMTP_PASSWORD`, `FORENOON_ANTHROPIC_API_KEY`). There is no configuration file.
- Ranking is on by default and used whenever an API key is set. Alerts are on by default and
  go to every admin's sign-in address unless another address is set.

## 3. The daily run

A scheduler runs once a day at the delivery time, in the deploy's time zone. `forenoon run`
does the same on demand. The scheduler reads the settings, readers and sources from the
database again before each run, so changes on the web pages need no restart; if they cannot
be read it keeps the previous settings and logs the error. For each user:

1. **Read feeds.** Each source is fetched with conditional requests (ETag / Last-Modified);
   an unchanged feed is skipped. A response that is not a feed, or any status other than 200
   or 304, counts as a failed source for this run. A feed entry that links to a site's front
   page rather than an article is ignored.
2. **Select candidates.** An item is a candidate for `max_age_days` (default 3) from when
   it was first seen here, not from the date its feed gives: posts are often dated before
   they appear, and some carry no date. Items already delivered to this user, and items
   whose extraction previously failed, are not candidates; nor is an item dated more than 30
   days ago (an old post a feed has brought back). A feed's first read has nothing to count
   from, so there its own dates decide and only its newest 20 are taken; the rest of its back
   catalogue is recorded as seen before the feed was followed and never becomes a candidate.
   A feed's candidates are what it lists now plus what earlier runs read from it within the
   window and the model never weighed for this user, so an article is not lost because the
   feed moved on or the run that would have weighed it did not happen; one the model weighed
   and passed over comes back only while the feed still lists it. There is no limit per
   source: the model weighs at most `llm.max_candidates`, taken round-robin across sources,
   and without the model the daily cap is the limit. Every window that finds candidates
   reaches back the same `max_age_days`, so nothing falls between two daily runs.
3. **Order.** Pinned sources first. The rest are ranked by the model when ranking is on
   (section 7a); otherwise, and whenever ranking fails, they are taken round-robin across
   sources with the newest item of each source first.
4. **Cap.** Candidates are processed in that order until the user's daily cap is reached. The
   cap counts stories delivered during the local calendar day (section 7e), so a second run on
   the same day only sends what is left of the cap. Most stories are a single article.
   **Link-only picks.** A candidate whose page cannot be had (a lapsed subscriber session, a
   site that turns the fetch away, a link-only feed) is not sent and does not take a place:
   the next candidate does. It is recorded as a link-only pick, at most as many a day as the
   cap, and shown on the newsstand as a grayed card with the feed's title and summary and a
   link to the site; it can be rated but not sent by hand. Every run tries its page again
   while it is younger than `max_age_days` and unrated (except for a link-only feed's); once
   it is had, it is sent with that run's issue after the day's own stories, without taking
   one of their places, and its card moves to that issue.
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
- **Split articles.** When the chosen content holds under 60% of a long page's text (500
  words or more), the smallest page element holding at least 90% of it is tried instead, and
  used if it recovers more without exceeding 1.3 times the page's text. This recovers
  articles laid out as several sibling blocks.
- **Structure.** Before the article is picked out of the page, structure that the page
  expresses only through class names and in-page links is rebuilt, by shape and by widely
  used class names, not per site:
  - Footnotes and side notes. A short link (a number or symbol) to a small in-page target,
    or an element classed as a sidenote, becomes a numbered reference; the notes are taken
    out of the page and put back at the end of the article under "Notes", each with a link
    back. They are marked as footnotes so a reader that supports it shows them as pop-ups.
    Where a reference and its note link to each other, the later of the two is the note.
  - Links to a heading on the same page stay links inside the book. Other in-page links
    still point at the site.
  - Inline code, callouts and asides (as block quotes), and comments (italic) and keywords
    (bold) in highlighted code blocks keep their meaning as elements.

  No id or class from the page reaches the book: the ids and link targets are generated.

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

### Saved pages

Each article's fetched page and feed entry are kept as received in `pages/` in the data
folder, for good by default (`keep_pages_days`: a number of days prunes older ones, 0 keeps
none). They are the only copy of what was read: for re-sending, and for learning from later. `forenoon replay` runs the current
extractor over every saved page and lists those whose verdict or word count differs from what
was recorded, so a change to extraction can be checked against real pages. Dry runs and
`forenoon report` save nothing.

## 5. Images

Images in the body are downloaded and embedded: at most 40 per article and 8 MiB per article
after conversion. Each is flattened onto white (so transparent images stay readable),
converted to greyscale by default, scaled to fit 1200×1600 and saved as JPEG. Images smaller
than 48 pixels on a side (tracking pixels, icons) and any that cannot be fetched or decoded
are dropped and counted in the report.

## 6. The book

- **Format `issue` (default).** One EPUB per day, titled `Forenoon · YYYY-MM-DD`:
  - a cover image for the library view: the Forenoon mark, the date, the story count and the
    first few headlines, drawn in grayscale with no font files needed;
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
- **Why (optional).** After a rating, the page always offers one more tap for the reason.
  For 1 to 3: not interested in the topic, too thin, already knew this, too long, wrong or
  misleading. For 4 and 5: more on this topic, more from this source, liked the depth. The
  rating counts without it. What was liked can be several of these at once; what was wrong is
  one, so a new choice replaces the old. Tapping a chosen reason again takes it back. A later
  rating that no longer fits a reason drops it.
- **Report a problem.** "This article came out broken or incomplete" is a separate link on
  that page and a button on the web pages. It is a report about the delivery to whoever runs
  the install, not a rating or a reason: it is logged, written to the extraction log, and
  listed for admins on the web pages' Problems page. The rating is left as it was.
- **Latest tap wins** for rating, finished and reason. Every tap is also kept in an event
  log.
- Links in articles delivered before ratings existed still work: "more like this" counts as
  5, "less like this" as 1, "finished" sets the finished flag only.
- Requests are rate limited to 30 per minute per client address, taken from Cloudflare's
  `CF-Connecting-IP` header.
- `forenoon feedback-log` prints the event log and the current rating per article.

Ratings influence delivery only when model ranking is on. The model is given the 40 most
recent, each with the article's length, whether it was finished, why it was picked and the
reader's reason when there is one, and is told to work out what a rating is about before
learning from it (a low rating on a short post is a verdict on thin posts, not on its
source).

## 7a. Ranking with Claude (on by default; needs an API key)

- **Input.** For every unpinned candidate, up to a ceiling of `llm.max_candidates` (600):
  title, source, author, date and the first 100 words of the feed entry as plain text. No
  article page is fetched for ranking. Also the user's `interests` text and their 30 most
  recent ratings (title, source, rating, finished).
- **Batches.** Candidates are scored `llm.batch_size` (80) to a call. Every call repeats the
  interests and ratings, marked for the provider's prompt cache. Scores from all batches are
  merged into one order.
- **Output.** A score from 0 to 10 and a reason of at most 15 words per candidate, returned in
  a fixed structure. Entries for unknown or repeated ids and non-numeric scores are discarded;
  reasons are cut to 140 characters and escaped like any other text.
- **Selection.** Pinned articles take slots first. The remaining slots go to the highest
  scores, except that the day's last slot goes to one article chosen uniformly at random from
  those outside the top picks (exploration). If an article then fails extraction, the next in
  order takes its place.
- **In the book.** Each chosen article shows "Picked because: …" under its byline and on the
  contents page (`llm.show_reasons`).
- **Recorded per delivered article:** policy, score, reason, rank among the day's candidates,
  whether it was the exploration pick, and the probability the policy put it forward.
- **Limits.** Before each call the day's logged token use plus an estimate for this call is
  checked against `llm.daily_token_cap` (600,000); over the cap, the call is not made and
  later batches are skipped. Every call is logged with model, input and output tokens and outcome;
  `forenoon usage` prints the log, today's total and this month's, each with an estimated cost
  from the prices in `llm.input_price` and `llm.output_price` (the API does not report
  prices).
- **Fallback.** With no API key, or when fewer than half the candidates end up scored
  (failed or timed-out calls, unusable answers, the cap reached), the run uses the
  newest-first order, the report says why, and the operator is alerted. When some batches
  fail but at least half the candidates are scored, the scores are used, unscored candidates
  go last, and the report says how many were scored.
- **Safety.** Candidate text is untrusted and is passed as delimited data with an instruction
  to ignore anything in it that reads as an instruction. The model has no tools that act on
  anything and cannot affect recipients, sources or settings.

## 7b. Built-in sources: discovery (needs ranking)

- **Sources.** Each is switched on or off on the Sources page; both start on. Hacker News: the front page, plus stories from the last `max_age_days` with
  more than 40 points (not just the last day: a story that gets its votes a day late would
  otherwise fall between two runs). Stories never seen before are taken first. Lobsters: the first three pages of its hottest list. Three requests per site per
  run; if a further page fails, the rest are still used.
- **Candidates.** Stories with a link to an article. Dropped: stories without a link, links to
  video and social sites or to files such as PDFs, anything older than `max_age_days`, and
  anything already in a feed read this run, already delivered, or previously failed. At most
  `discovery.per_site` (100) per site.
- **Scoring.** Found articles go into the same ranking call as feed articles. The model sees
  the title, the site, points, comment count and (Lobsters) tags and description, since there
  is no abstract.
- **Selection.** Found articles scoring at least `discovery.min_score` (6) take up to
  `discovery.slots` (2) of the day's slots, and never more than half of them; the rest go to
  feed articles as before. If a found article fails extraction, the next best found article
  takes its place, then feed articles.
- **In the book.** The source reads "site via Hacker News", with a link to the discussion.
- **Without the model** (off, failing or over its cap) no found article is delivered.
- A listing that fails is reported and alerted like a failed feed.

## 7c. Extraction check by the model (on when ranking is on; log only)

- This covers the articles the reading pass (section 7f) did not see: pinned articles and
  pages brought in by a story. After extraction, one call per run sends the model each such
  article that is about to be delivered: title, word count, section headings, and the first
  120 and last 80 words.
- It answers `ok`, `suspect` or `bad` per article with a problem of at most 20 words, for
  example a paywall notice, a teaser, an ending cut short, or menu text.
- Verdicts are stored and shown in the run report; `forenoon checks` lists the articles it
  did not judge `ok`, beside the extractor's own verdict. **Delivery is never changed.**
- The call counts against the daily token cap and is skipped when it would cross it.
  `llm.check_extraction: false` turns it off.

## 7d. Recipe sources (optional)

- A separate **recipes container** holds Calibre. Once a day at `RECIPES_TIME` it runs each
  recipe listed in `<data>/recipes/recipes.conf` and writes the issue to
  `<data>/recipes/<folder>/<date>.epub`. An issue is published only when the recipe succeeds
  and the issue has at least one article: Calibre reports a paper of empty sections as a
  success, which is what a site refusing the recipe looks like. Issues older than 7 days are
  removed.
- Recipes are set up entirely on the Sources page. The container publishes
  `catalogue.json`, the publications its Calibre can fetch and which of them want a
  subscriber's login; the page offers that list. Adding one picks its folder, writes
  `recipes.conf` (the web pages own that file) and asks for it to be fetched at once; the
  container looks for such requests every 20 seconds. How each recipe's last fetch went
  (`status.json`) is shown beside it, and the page reloads itself while one is running.
  "Fetch now" asks again at any time.
- A login is typed on the Sources page, opened for you when the publication needs one. It
  is written to a Docker volume that only the web pages (to write) and the recipes container
  (to read) mount. It is never in the data folder, which the pipeline mounts, never in the
  database, and never shown again: the page shows only the username. Removing the recipe
  removes its login. The recipes container mounts only that volume and the `recipes` folder.
- A source of `type: recipe` reads the newest issue in its folder (and any earlier issue from
  the last `max_age_days` that was never read, so a missed run does not lose a day's paper)
  and splits it into
  articles, in the issue's order: navigation is removed, the original URL is taken from the
  issue (minus tracking parameters, including the marker Calibre's FT recipe adds), and images
  are taken from the issue itself. Every article in the issue becomes a
  candidate.
- Recipe articles are extracted from the issue only; their pages are never fetched. From
  there they are treated like feed articles: sanitised, ranked, capped, rated and
  deduplicated by URL against every other source.
- No issue from the last `max_age_days`, or an unreadable one, counts as a failed source.

## 7e. Stories

The daily cap counts stories, not articles. A story is one article, or up to
`max_story_entries` (3) articles on one subject delivered together. An article over
`max_article_words` (45,000) is never delivered, whether picked or reached through a link. It
is a failsafe for manuals and books; choosing well is the model's job. The number of articles in
a day's book is therefore a target, not a limit.

- **Articles about another page.** After the day's stories are chosen, one model call
  (`llm.follow_links`, on by default) is shown each chosen feed article's opening words and
  up to eight of its links to other sites, and returns the links that are the article's
  subject: the page it quotes, announces, summarises or responds to. Usually that is none.
  Those pages are fetched, and the ones that extract cleanly join the story:
  - A post of fewer than 150 words is a pointer. The pages replace it: the post itself is not
    printed, the first page carries a "Found through …" credit, and the post is recorded as
    delivered so it is not offered again.
  - A longer article is a response or commentary. It is delivered first and the pages follow
    it.
  - When no page can be delivered, the article goes out as it was.
  - Without a working model, only pointers are followed, through their first four links.
  - Links to the article's own site, to video, social and chat sites, to files and to front
    pages are never followed. Recipe articles and discovered articles are not examined.
- **Same event.** When ranking is on (`llm.group_stories`, on by default), one more model
  call compares the 40 best-scored candidates and returns those that cover the same specific
  event, announcement or release. When one of them is chosen, the others are delivered right
  after it as part of its story; any beyond the limit are dropped for the day rather than
  taking a slot of their own. Ids the model invents or repeats are ignored, and a failed call
  means every article is its own story.
- **In the book.** A story's later articles are listed under its first on the contents page
  and carry a "More on: …" line. The contents page gives the count of stories and articles. Each article keeps its own rating links.
- **Recorded.** Each delivery stores the story it belonged to. An article brought in by a
  story is recorded with the choice (score, reason) of the candidate that brought it.

## 7f. The reading pass (on when ranking is on)

Ranking judges a title and an opening. Before the day is chosen, the model also reads the
best candidates in full.

- The `llm.read_candidates` (40) best-ranked candidates are fetched and extracted. Their
  text, up to `llm.read_words` (2,500) words each, goes to the model about ten articles to a
  call, with the reader's interests and ratings. Recipe articles need no fetch.
- Per article the model returns a score from 0 to 10, a reason, whether the text is a
  complete article (`ok`, `suspect`, `bad`), how long it stays worth reading (`days` for
  news tied to the week's events, `months`, or `years`), and which of its links are its
  subject (section 7e, in place of the separate link call).
- The read articles are tried first, best score first; candidates that were not read follow
  in ranking order. The exploration pick keeps the day's last slot. Scores and reasons from
  reading replace the ranking ones in the book and in what is recorded.
- An article judged `bad` (a paywall notice, a teaser, an error page) is held back for the
  day and listed in the report. `suspect` is reported and does not change delivery.
- Both prompts tell the model that news of major consequence scores at least 7 whatever the
  reader's interests.
- What was extracted for reading is reused when the article is built, so each page is fetched
  once. Images are fetched only for the articles that are delivered.
- A batch that fails or would cross the daily token cap is skipped; its articles keep their
  ranking order. `llm.read_candidates: 0` turns the pass off.
- Measured on one day (October 2026, Claude Haiku 5.5): 23 sources including a full FT issue,
  about 165 candidates ranked, 40 read in full, 16 stories (25 articles) chosen, 167,000
  tokens, about $0.02, 80 seconds.

## 7h. Notes on each reader's taste (on when ranking is on)

A reader's interests are their own words and are never changed by the pipeline. Beside them
it keeps a second text of its own: a few plain statements of what the reader's ratings show.

- **When.** After a delivery, at most once a week per reader, and only when the reader has
  at least 10 ratings and at least 5 new ones since the last time. One model call.
- **From what.** The interests, the notes as they stand, and the reader's latest 200 ratings
  with each article's length, whether it was finished, why it was picked and the reader's
  own reason. Titles are untrusted and are given only as material.
- **What comes back.** At most 8 statements of at most 200 characters each, each resting on
  several ratings. The model is told to keep a current note the ratings still bear out, to
  change one they contradict, and to write no instructions. When it has nothing reliable to
  say, the notes stay as they were.
- **Use.** The ranking and reading prompts are given the notes beside the interests; where
  the two disagree, the interests win.
- **The reader's.** The web pages show the notes, let the reader edit or empty them, and
  keep every version, the pipeline's and the reader's, with a way back to each (section
  9a). An edit is not a lock: the next rewrite starts from it. `llm.learn` on the Settings
  page turns the whole thing off.

## 7g. The library: what is kept about every candidate

The pipeline keeps a record of what it considered, not only of what it sent. This is what
the web pages' Archive shows (section 9a).

- **How an article reached us.** Every route is recorded, and an article can have several:
  `feed` (a feed the install follows), `recipe`, `found` (on a built-in front page such as
  Hacker News), `linked` (followed from another article) and `manual` (added by a reader by
  hand), each with the name of the feed, recipe or front page. A followed blog's post that also reaches a front page has both.
- **What the model made of it, per reader.** Every candidate the ranking scored has its
  score and reason kept; a candidate the reading pass read has the score and reason from
  reading, and how long it stays worth reading. A judgement made from a headline never
  replaces one made by reading. Pinned articles are not put to the model and have none.
- **A front-page find is on record once it has been ranked**, whether or not it is tried.
  On later days it counts as already seen, so newer finds are considered before it.
- **The text is kept for what passed the first filter.** The pages fetched for the reading
  pass are saved as for delivered articles (`keep_pages_days`, for good by default), along
  with their length and summary. Candidates ranked from a headline alone have a row and no
  saved page.
- Nothing is recorded on a dry run.
- **Never sent twice, never sent once read.** The daily run skips an article the reader has
  already been sent, rated or marked finished.

### Links added by hand

A reader adds a link on the Archive page and says what to do with it. The web program only
queues it; the scheduler checks the queue every minute (`forenoon send-queue` works it at
once), fetches the page as it fetches any article (public addresses only, and with a feed's
subscriber session when one covers the site), saves the page, and stores the article with
the route `manual` and its site as the source.

- **Put it in my next issue.** It goes first in the reader's next daily issue, ahead of
  pinned sources, built from the page saved when it was added, and takes one of the day's
  places. If the day is already full it waits for the next. Recorded as chosen by `added`.
- **Send it to my Kindle now.** Sent as a book of its own, as Send to Kindle does.
- **Only keep it: I have read it.** Marked finished, with the reader's rating when given.
  It is never sent, and the rating counts with the others when the model chooses.
- A link that cannot be fetched or has no article in it is marked failed, with the reason
  shown on the Archive page for a day. At most 30 links a day per reader.

## 8. Delivery

Books are emailed as attachments through an SMTP relay (STARTTLS, with login) to the user's
Send to Kindle addresses. Recipients come only from the reader's account, where an admin sets them;
nothing in a feed or article can add or change a recipient. The sender address must be on
the Amazon account's approved sender list.

## 9. Reports and alerts

- **Run report.** Per source: a verdict (clean / degraded / failed / no items), counts, and
  the most common reason; per article: status, word count, extraction method, images dropped
  and reason. Also the number delivered and any delivery error.
- **Triage.** `forenoon report` extracts the latest items of every source without delivering
  or recording anything. It fetches every feed and page again, so `--source` limits it to the
  sources whose name contains the given text.
- **Alerts.** When `alerts.to` is set, the operator is emailed if a delivery fails, a run
  crashes, a run has nothing new to deliver (unless the day's cap was already used), or a
  source has failed a set number of runs in a row (default 3, repeated at each multiple).
  Alerts use the same relay; a failure to send one is logged and does not affect the run.

## 9a. Web pages (optional)

A separate program, `forenoon-web` (Rust, in `web/`), with its own image. It shares the
database with the pipeline.

- **Accounts.** Every page except sign-in and the health check needs a session. An account
  is a reader: the pipeline delivers to each account that has a Kindle address. A signed-in
  reader sees and changes only their own deliveries, ratings and settings. Accounts are created from the command line
  (`forenoon-web add-user <id> --email <address>`); there is no sign-up page. The first account is an admin,
  later ones readers; the role is stored but grants nothing yet.
- **Sign-in is by email address**, stored lowercase so casing and stray spaces cannot make
  two accounts of one address. The account id is separate and never changes: it names the
  reader in the pipeline's records.
- **Soft confirmation.** A new address works at once, unconfirmed. A banner offers to email
  a confirmation link (valid 24 hours, at most 5 emails an hour per account); the link needs
  no session. Nothing is locked until then except password reset.
- **Forgotten password.** The sign-in page links to a form that emails a reset link (valid
  60 minutes, single use) when the address belongs to an account and is confirmed. The
  answer is the same whether or not it does. Choosing the new password signs the account
  out everywhere.
- **Changing the address** needs the current password, and uses a pending address: the link
  goes to the new address and the old one keeps working until that link is opened. It can
  be cancelled.
- **Emails** are sent through the mail server named in the environment
  (`FORENOON_SMTP_*`), with links built from `FORENOON_WEB_URL`, never from the request.
  With no server set they are written to the log. Only the SHA-256 of a link's token is
  stored.
- **Passwords and sessions.** Passwords are stored as Argon2id hashes, at least 10 characters.
  Sign-in is limited, in any 5 minutes, to 30 attempts from one client address, 10 from one
  address for one email, and 100 for one email from anywhere. Reset requests and reset
  attempts are limited per client address per hour.
  A session lasts 30 days from its last use, in an `HttpOnly`, `SameSite=Lax` cookie.
  Changing a password signs out every other browser.
- **Newsstand.** Delivered articles grouped by issue, newest first, 14 issues to a page, in
  the reader's time zone. Each card shows source, author, title linked to the original, lead
  image and description when the page had them, word count and reading time, how it was
  chosen (score and rank, pinned, exploration pick, the model's reason) and its rating.
  `?show=rated` or `?show=unrated` filters it. A story's articles share one card: the lead
  article, with the others delivered with it listed beside it, each still rated on its own.
- **Ratings can be changed there**: set or change the 1 to 5 rating, mark finished or not,
  choose the reason with one click, or clear the rating. Changes are logged like taps, with `web` as their
  source.
- **Archive** (`/archive`). The reader's library as a list, one row per article: everything
  delivered to them, and everything the model weighed for them and passed over (section 7g).
  A row shows the source, the title linked to the original, the summary when there is one,
  the model's reason, whether it was delivered, read by the model or only weighed from its
  headline, the score, the length, how long it stays worth reading and every way it reached
  us. Filters are a form sent by GET, so any view is a link: words in the title, delivered
  or read or everything, the feed, recipe or front page that brought it, how it arrived,
  the reader's rating, how long it lasts, a date range, and the order (newest, score, rating,
  length). Without a choice it shows what was delivered, added by hand or read by the
  model, 50 to a page. A row can be rated and sent to the Kindle like a newsstand card. A
  form at the top adds a link by hand (section 7g). A reader sees only what was delivered
  to, added by or weighed for them.
- **What Forenoon has learned** (`/learned`, linked from the account page). The notes the
  pipeline keeps on the reader's taste (section 7h): when they were last rewritten, a box to
  edit them (2,000 characters at most), and the last 10 versions with who wrote each and a
  button to restore it.
- **Buttons.** The button that carries out a form is filled in the accent colour; "Send to
  Kindle" is outlined in it and set apart from the rating row. The small plain buttons are
  choices: ratings, reasons, finished. The pages use no scripts, so the Archive's filters
  apply when "Apply filters" is pressed.
- **Send to Kindle.** Each article has a button that sends it to the reader's own
  Kindle address, as a book of its own with fresh rating links. An article the daily run
  passed over can be sent from the Archive: it is then recorded as delivered and marked
  "Sent on request", so the daily run never sends it again. The web program only
  queues the request; the scheduler checks the queue every minute, builds the book from the
  page saved when the article was first delivered (fetching it again only if there is none)
  and records the outcome, which the card shows. At most 10 a day per reader; it is not
  counted against the daily target. `forenoon send-queue` works the queue at once. The book is
  named by the article's title alone, with no date in front, and has a cover made from the
  article's thumbnail with its source and title (words alone when it has no picture).
- **Stories.** A story's articles share one card with a header across the top saying how
  many there are. The lead article has the large picture; the others are listed beside it
  under "Also in this story", each with its own small thumbnail when it has one. The Rated
  and Not rated views show a story whole when any of its articles matches. The header has
  **Send story to Kindle**: every article delivered in the story as one book with a contents
  page, named and covered by its first article. It counts as one send. A short post that was
  replaced by the pages it pointed at is left out, as it was from the delivered book.
- **Account page.** Display name, time zone, password, and the reading settings the
  pipeline uses: interests, stories a day, and Kindle addresses. Only an admin can change
  Kindle addresses, because they decide where content is emailed.
- Every change is a form post that must carry the session's token and, when the browser says
  where the form came from, must come from the site itself, so another site open in the same
  browser cannot make one.
- Article text is untrusted here as elsewhere: everything is escaped, links and images must
  be plain `http(s)`, and the content security policy allows no scripts. Lead images are
  loaded by the browser from the article's own site, without a referrer.
- **Pictures.** The pipeline makes a small thumbnail for each article it prepares, from the
  page's lead image or else the article's first few pictures (including a recipe article's,
  which live inside the issue), and keeps it in `thumbs/` in the data folder. The web pages
  serve it, only to a reader the article was delivered to. A card falls back to the
  article's own lead image address when there is no thumbnail, and has no picture when the
  article has none. `forenoon thumbs` makes thumbnails for articles delivered earlier.
- Who owns what in the database: the web program owns accounts (with the readers' settings)
  and sessions; the pipeline owns everything else and only reads the accounts. A Python
  test applies the web program's migrations and reads readers through them, so a column the
  pipeline needs cannot be dropped unnoticed. Of the pipeline's tables the web program writes only ratings and the
  tap log, by the same rules as the feedback service. Its queries are checked at build time
  against a copy of the pipeline's schema (`web/pipeline-schema.sql`), and a test fails when
  the copy and `app/store.py` differ.
- It is still meant for a trusted network only. Before it is exposed publicly it needs: the
  secure-cookie setting behind https, and the client address taken from the proxy for the
  sign-in limit.

## 10. Commands

| Command                                         | Purpose                                                       |
| ----------------------------------------------- | ------------------------------------------------------------- |
| `forenoon run [--dry-run] [--out DIR] [--user]` | One delivery now; `--dry-run` builds files and sends nothing  |
| `forenoon schedule`                             | Run every day at `delivery_time`                              |
| `forenoon report [--per-source N] [--source X]` | Triage sources, or only those whose name contains X           |
| `forenoon feedback [--host] [--port]`           | Serve the feedback endpoint                                   |
| `forenoon thumbs`                               | Make the web pages' thumbnails for articles delivered earlier |
| `forenoon send-queue`                           | Fetch the links readers added and send what they asked for    |
| `forenoon feedback-log`                         | Print taps and current ratings                                |
| `forenoon usage`                                | Print recent model calls and today's token use                |
| `forenoon checks [--all]`                       | Print articles the model thought were badly extracted         |
| `forenoon replay [--all]`                       | Re-run extraction over saved pages and show what changed      |
| `forenoon golden [--out DIR]`                   | Write the rendering-test book                                 |

The web pages have their own program:

| Command                                                                                | Purpose                                    |
| -------------------------------------------------------------------------------------- | ------------------------------------------ |
| `forenoon-web serve [--listen ADDR] [--secure-cookies]`                                | Serve the pages                            |
| `forenoon-web add-user ID --email ADDR [--kindle ADDR] [--name] [--role] [--timezone]` | Create an account; asks for the password   |
| `forenoon-web set-password ID`                                                         | Change a password, signing the account out |
| `forenoon-web users`                                                                   | List the accounts                          |
| `forenoon-web health`                                                                  | Exit 0 when the local server answers       |

## 11. Storage

A copy of each book sent is kept in `issues/`, for good by default (`keep_issues_days`, as
for saved pages).

One SQLite database in the data folder, shared by the pipeline, the feedback service and the
web pages: articles (with extraction verdict and preview data), the routes they arrived by,
the model's judgements per reader, deliveries, the tap event
log, current ratings, per-source failure counts, feed cache validators, and the web pages'
accounts and sessions. Older databases are upgraded in
place on start-up. Backing up the data folder backs up everything.

## 12. Deployment

- Images are published to GHCR by CI on every push to `main`: the app, the recipes runner
  and the web pages. Containers run as UID 1000 with a read-only root filesystem and no
  added capabilities.
- Three containers: the scheduler, the feedback service, and a `cloudflared` tunnel that
  shares the feedback container's network namespace. No host port is published. An optional
  fourth runs Calibre recipes from its own image, and an optional fifth serves the web pages
  from theirs (`WEB_IMAGE`) on a host port chosen with `WEB_LISTEN`.
- `deploy/docker-compose.yml`, or `deploy/deploy.sh` with plain `docker run`.
  `deploy/poll-deploy.sh` redeploys when the app image changes, replaces only the tunnel
  when just its image changed, and restarts the stack after a reboot.
- The Kindle browser needs TLS 1.2, so the feedback hostname's zone must not require 1.3.

See `docs/setup-deploy.md`, `docs/setup-email.md` and `docs/setup-recipes.md`.

## 13. Security properties

- Fetched content cannot choose recipients, run code, or reach private network addresses.
- The public endpoint accepts only signed, expiring tokens and writes only ratings.
- The web pages need a login and show each reader only their own data. They have not been
  hardened for the public internet yet; keeping them on a trusted network is still the
  operator's job.
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

- Summaries; a local model as an alternative to Claude; a ranker that needs no model call.
- Searching Hacker News and Lobsters by topic rather than by popularity; a watch list for vendor
  announcements.
- The whole archive of a followed blog; full-text search of stored articles; expiring
  articles past their shelf life; a "throwback" section of older pieces worth a second look.
- A search agent, or an MCP server, over the library.
- Editing the prompts in the browser; sign-up and admin pages for accounts.
- Newsletters by email as a source.
- E-readers other than Kindle.
