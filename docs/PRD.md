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
what its own users configure and delivers only to the addresses its readers have set.

## 2. Users and configuration

- An install has one or more **readers**, each with an id, a name, zero or more Send to Kindle
  addresses (none: they read on the web pages), a daily target of stories (default 8; section 7e) and an interests text. They
  live in the database: a reader is an account of the web pages
  (section 9a), made by an admin's invitation (or `forenoon-web add-user`) and edited on its
  Reading and account pages. The
  pipeline reads them at the start of every run. With no readers, nothing is delivered and
  an alert is sent. A reader with no Kindle address reads on the web pages: their issue is
  built and recorded as for anyone (on their newsstand, counted against their day's cap,
  rated the same way), nothing is emailed, and no alert is sent; the run report says so.
  Adding an address sends the next issue to it (with the daily email on, the default). A reader with an address can turn the daily
  email off on their Account page ("Email my daily issue to my Kindle", on for a new account):
  their issue is then read on the web pages the same way, and Send to Kindle still sends what
  they pick there. Sending one article from the web pages
  still needs an address and says so.
- **Sources** belong to the household and are read for the readers who **follow** them: a
  reader's issue is built from the sources they follow, and nothing else. Each reader has
  their own pin on a source. A feed is added **shared** unless the adder unticks it; a
  private feed is the adder's alone until they share it: nobody else sees it anywhere,
  except an admin, who sees every source and may share or unshare any. A source is followed for every reader when it is shared (added shared, or shared later),
  as it is for a new account, unless the reader has unticked "Follow feeds when they are
  shared with the household" on the Sources page; a reader who does not want one unfollows
  it, and later edits leave that alone. Unsharing it leaves its followers following. A
  shared feed a reader does not follow is tagged New for two weeks after it was shared.
  A new account follows every shared source (`add-user --follow-nothing` for none).
  A follow is **chosen** or **made**: made for the reader (a new account's, or a feed being
  shared) until they press "Keep it" in its opened row, follow it by its address, or pin it;
  chosen from then on, and when they follow it themselves. A feed row's dot shows it: filled
  for chosen, a ring for made, and a "+ Follow" button in its place for a feed the reader
  does not follow; a key above the list says so. Unfollowing is in the opened row.
  Follows from before this distinction count as chosen. A feed
  added by an address the reader can already see (shared, their own or followed; case, the
  scheme, `www.` and a trailing slash aside) is followed, not added again. Another reader's
  private feed is never matched, whatever its address, so nobody learns of one by guessing;
  the same feed may then exist twice, once each, and the address check's rewriting of a
  site's address to its feed's can have the same effect. Names are unique across the
  install, since articles and health records refer to a source by name; a reader who picks
  a taken name is told so, and an article two such feeds carry is named for whichever
  reader's run saw it first. Both are accepted as the one small leak.
  - Who may do what: anyone adds feeds, follows, unfollows and pins; the reader who added
    a feed changes or removes it while no other reader has chosen to follow it (a follow
    made for someone holds nothing), and shares or unshares it at any time; an admin
    changes, shares and unshares any source; nobody, admin included, removes a feed another
    reader chose to follow: an admin pauses it instead ("Fetch it for the household" in the
    opened row), which stops it for everyone, and switching a built-in source on or off is
    an admin's too.

  Sources come in two kinds, each with its own section on the web pages' Sources page,
  which also shows how each source's last runs went. A source is one compact row (a
  dot, its colour how the source's last run went and its shape how the reader follows it,
  with both in its tooltip; name, address, and tags for new, pinned, paused,
  places a day, where its text comes from, and shared or private, with the adder's name
  for an admin), opened to change it; feeds that are failing or whose address could not be
  checked are listed first under "Needs attention" (for a reader, only the ones they
  follow), then the rest the reader follows, then those they could follow. The list is
  narrowed by a text filter on name and address and by views with a count each (all,
  failing, paused, pinned), both plain GET parameters; adding a feed or a session is a
  panel opened from a button, apart from the list.
  - **Built in**: front pages the pipeline knows how to read (Hacker News and Lobsters;
    section 7b). They can only be switched on or off. The list comes from the pipeline: one
    that a new version adds appears switched off, so an upgrade never changes what is
    delivered by itself.
  - **Feeds**: RSS or Atom, by address.

  They live in the database. The pipeline reads each reader's at the start of their run; a
  reader who follows nothing gets nothing, and an alert says so. A feed has a name and:
  - pinned (by each reader for themselves): its articles are taken before all other
    sources, every one of them.
  - places a day (default 2, 0 for no limit): the most of an issue it may take. For a pinned
    source, how many of its articles use the day's places; the rest are extra.
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

  **Subscriber sessions.** A site the household subscribes to can have a session: the cookie
  a reader's own browser sends to it, pasted once in the Sources page's Subscriber
  sessions section. It belongs to the site, not to a feed or a reader: every page on the
  site and its subdomains is fetched with it, from any of its feeds or added by hand, for
  whoever follows the feed, so nobody pastes a cookie twice; each feed on the site says so
  on its row. A reader sees the sessions of the sites they have a feed on, may save or
  replace one for such a site, and may remove one they saved; an admin sees and does all.
  The pipeline knows only which sites have one (the `sessions` table). (A
  username and password are not offered: the FT and the NYT both ask for a CAPTCHA at
  sign-in.) Feeds are read as usual, without it.
  Only the article pages the ranking wants read (the reading pass and the day's choices) are
  then fetched with the session, a few a day and at least 20 seconds apart per site, instead
  of a whole paper. The cookie is kept in the logins volume: never in the
  database or the data folder, never shown again, and used only on https pages of the one
  site named with it and its subdomains (`site-<domain>.json`). The pipeline never holds it. It leaves a request in
  `<data>/sessions/.fetch/`, and the browser container (section 7d) reads the page and leaves the answer
  there, so sessions need that container running. Pages are read by Chrome on a virtual
  screen (Xvfb), since some sites admit only a browser, and what it keeps of the session is
  in its own volume. Nothing answers a CAPTCHA: a site that asks for one, shows its paywall
  (the cookie has expired) or keeps the browser out is reported on the session's row. When a
  session needs a fresh cookie, its picks are shown as links (step 4 of section 3), not sent as the
  feed's summary, and the run's alert email names the site. Once a page has been fetched with
  it, the session's row shows the day the site says its sign-in ends (for the sites whose
  sign-in cookie is known: the FT and the NYT), and the alert email says so from 14 days
  before. Pages the source links to on other sites are fetched as usual, without the
  session.

- **Settings** for the whole install (delivery time, how old an article may be, ranking,
  its models and token cap, how many articles are read in full, alerts, what is kept) are
  on the web pages' Settings page (admins only) and live in the database. A setting stays
  only when it controls cost, depends on the deploy, or is a real choice; what used to be a
  switch for a later feature is now just how Forenoon works, described here.
- **Each reader's own settings** are on their Reading page: one book a day or one per
  article, the most articles on one broad subject (2; 0 for no limit), room for a surprise
  (the exploration pick, on), the model's reason printed in the book (on), and notes kept
  from their ratings (on). Whether their Kindle has a colour screen (images and covers in
  colour; otherwise gray) and whether their daily issue is emailed to it (on) are on their
  Account page beside their Kindle addresses. A new
  account starts with the defaults; migration 0024 started every reader from the values
  the install had.
- An admin setting has a default in the code; only
  a value that differs from it is stored, so a default that changes in a later version
  reaches installs that never touched it. The pipeline reads them before every run. A stored
  value it cannot use is logged and skipped, never fatal.
- **The deploy's own settings** come only from the environment (the `.env` file): the data
  folder, the time zone (`TZ`), the mail server (`FORENOON_SMTP_*`: host, port, username,
  sender, the largest message in bytes, and `FORENOON_SMTP_IMPLICIT_TLS` for TLS from the
  start on port 465 instead of STARTTLS; both programs read the same variables), the web
  pages' public address (`FORENOON_PUBLIC_URL`), the shipped model list
  (`FORENOON_MODELS`, set by the image) and the secrets (`FORENOON_FEEDBACK_SECRET`,
  `FORENOON_SMTP_PASSWORD`, `FORENOON_ANTHROPIC_API_KEY`), and each OpenAI-compatible model
  server (`FORENOON_LLM_<NAME>_URL` and the variables beside it, section 7). There is no
  configuration file.
- A model is required (the Anthropic key, or a model server): it chooses the articles. Alerts are on by default and go
  to every admin's sign-in address unless another address is set.

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
   and passed over comes back only while the feed still lists it. Every candidate is weighed
   (the model weighs at most `llm.max_candidates`, taken round-robin across sources); each
   source's limit on places applies only when the day is filled (section 3a). Every window
   that finds candidates
   reaches back the same `max_age_days`, so nothing falls between two daily runs.
3. **Order.** Links added by hand, then pinned sources, then the rest, chosen by the model
   in three rounds (section 3a); whenever ranking fails (no API key, a failed call, the day's
   tokens spent), they are taken round-robin across sources with the newest item of each source first.
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

## 3a. Choosing the day's articles

The whole process for one reader's day, in order. The sections named give the detail.

1. **Candidates.** Every enabled feed's eligible articles (section 3, step 2), round-robin
   across sources so a busy feed cannot crowd out a quiet one. With ranking on, the built-in
   front pages add their links (section 7b). An article several sources carry this run (a
   feed and a front page, or two front pages) is one candidate with every sighting: the
   model sees each site's votes and comments together, it is credited to all of them, and
   its place counts against whichever of its sources has room.
2. **Ahead of the rest, not judged.** Links the reader added for their next issue lead,
   each taking a place. One whose page still cannot be had once it is `max_age_days` old is
   given up on: it becomes a link-only pick (shown as a link, holding no place). Then pinned
   sources: every article they have is taken; up to the source's places a day they use the
   day's places, and the rest are extra.
3. **Round 1: abstracts** (section 7a). Every other candidate is scored 0 to 10 from its
   title, source and opening, by batches of 80, shuffled so feed articles and finds share
   batches. With the reader's interests, learned notes and recent ratings.
4. **The exploration pick** ("Leave room for a surprise", each reader's own, on by
   default). One place, the day's last, goes to an article drawn at random from those below
   the cut, so the ratings learn about what the model would not choose. It keeps that place
   through the later rounds.
5. **Same event** (section 7e). The 40 best are compared for coverage of one event; those
   become one story, which takes one place.
6. **Round 2: reading** (section 7f). `llm.read_candidates` (40) are fetched and read in
   full, taken in equal shares from every round-1 batch (each by its own batch's scores),
   plus the exploration pick. Each gets a new score and reason, a completeness verdict, how
   long it lasts, and which of its links are its subject.
7. **Round 3: choosing the issue** (section 7f). One call sees every read article at once
   and returns exactly the open places' worth (the day's places less the hand-added and
   pinned ones and the exploration pick), best first, under two rules: at most
   the reader's limit on one broad subject (2 by default), counting the hand-added and pinned
   articles it is shown as already in the issue; and at least one news story of major
   consequence when one scored 7 or more. Within them, better articles first. Then reserves
   in the order to use them, as many as the places held by hand-added and pinned articles
   plus two, so a place freed late (one of those that cannot be sent) still goes as the
   model would choose. The other read articles follow, then the unread ones in round-1
   order. An article left out that its score alone would have given a place, and that keeps
   (judged to last months or years), is **deferred**: it is offered again for the rest of
   its age window, even if its source no longer lists it, and marked as deferred to the
   model, which prefers it over an equal candidate but not over a better one.
8. **Filling the day** (`daily_stories`, default 8). Candidates are taken in that order
   until the day's places are used, each subject to:
   - **its source's places a day** (Sources page; 2 for a feed, none for a built-in source,
     0 for no limit). An article carried by several sources counts against whichever has the
     most room and is passed over only when all of them have used theirs; links added by
     hand are not counted;
   - **being deliverable**: not judged "not the article" by the reading, under
     45,000 words, and within the message size. Otherwise the place goes to the next.
   - A candidate whose page cannot be had is a link-only pick (section 3, step 4) and takes
     no place.
9. **Following links** (section 7e). A short pointer post is replaced by the pages it is
   about; a longer article is delivered with the page it responds to, in one story.
10. **Recorded.** Every score and reason, delivered or not (the archive shows what was
    passed over and why); for each delivery the policy (`rank-v2`, and the model that scored it), score, rank,
    whether it was the exploration pick and the probability it was put forward (1 when the
    model chose it); and in the run's report, the model's choice with what became of each.

Without the model (no key, a failing call, or over the token cap), steps 3 to 7 are skipped:
hand-added links and pinned sources first, then newest first round-robin, still subject to
each source's places a day; no built-in source is read.

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
`forenoon report` save nothing. A page saved in the last six hours is not fetched again by
another reader's run that picks the same article: two readers of one paywalled feed cost the
site one page. "Fetch again" and links added by hand always fetch.

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
- Code is set in JetBrains Mono NL (no ligatures), embedded only in books that contain code
  (about 200 KB), left-aligned and never hyphenated. The image downloads the font from a
  pinned release, checked against its hash; without the file (`FORENOON_CODE_FONT`, default
  `app/fonts/`), code uses the device's own monospace font. Why: [kindle-books.md](kindle-books.md).
- Emoji are set in Noto Emoji (black and white), cut down to the emoji each book uses (a few
  KB), because the Kindle's fonts have none. Sequences such as families, flags and keycaps
  stay whole and are drawn as one glyph; symbols with a text form (`©`, `™`) are left to the
  reading font. Without the file (`FORENOON_EMOJI_FONT`, default `app/fonts/`), emoji are left
  as they are.

### Size limits

Email size is capped at 45 MiB (`FORENOON_SMTP_MAX_MESSAGE_BYTES` in the env file changes
it) and 25 attachments a message (fixed). If one article alone would exceed the limit its images are
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
- The web pages answer `GET /f/<token>`: they verify the token, record the tap and return a
  small page stating what is now stored ("Rated 4 of 5, finished.") with links to change it.
  Besides sign-in, the forgotten-password and email-link pages and `GET /healthz`, it is the
  only page that needs no login, and it takes no other input.
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
- Taps are rate limited to 30 per minute per client address (see §12 for how the address
  is found behind a proxy).
- `forenoon feedback-log` prints the event log and the current rating per article.

Ratings influence delivery through the model (a run that falls back to newest first does
not use them). The model is given the 40 most
recent, each with the article's length, whether it was finished, why it was picked and the
reader's reason when there is one, and is told to work out what a rating is about before
learning from it (a low rating on a short post is a verdict on thin posts, not on its
source).

## 7a. Ranking with a model (needs the API key)

- **Input.** For every unpinned candidate, up to a ceiling of `llm.max_candidates` (600):
  title, source, author, date and the first 100 words of the feed entry as plain text. No
  article page is fetched for ranking. Also the user's `interests` text and their 40 most
  recent ratings (title, source, rating, finished).
- **Batches.** Candidates are scored `llm.batch_size` (80) to a call. Every call repeats the
  interests and ratings, marked for the provider's prompt cache. Scores from all batches are
  merged into one order. With more than one batch the candidates are shuffled first (seeded by
  the day), so feed articles and finds are judged side by side rather than each on its own
  batch's scale.
- **Output.** A score from 0 to 10 and a reason of at most 15 words per candidate, returned in
  a fixed structure. Entries for unknown or repeated ids and non-numeric scores are discarded;
  reasons are cut to 140 characters and escaped like any other text.
- **Selection.** Pinned articles take slots first (up to each pinned source's places a day;
  the rest of them are extra). The remaining slots go to the highest scores, as refined by
  the reading pass and the choice of the issue (section 3a), except that the day's last slot
  goes to one article chosen uniformly at random from those outside the top picks
  (exploration). If an article then fails extraction, the next in order takes its place.
- **Models.** A model is named with the connection it is reached through,
  `anthropic/claude-haiku-5-5`. Anthropic is built in. Any other server that speaks the
  OpenAI chat completions API (OpenAI itself, or LM Studio, Ollama, llama.cpp or vLLM on the
  network) is a connection set in the pipeline's environment, as with the self-hosted apps
  that call a model in the background: `FORENOON_LLM_<NAME>_URL`, and optionally its key,
  the context size the server holds (32,768), an answer limit (16,384), a time limit (300 s),
  whether the model may reason first (no; not sent to OpenAI) and whether it is free (yes: a
  server you run costs nothing and stays outside the token cap; only one on a private or
  local address can be). Its models are then named `<name>/<model id>`, the name in lower
  case, and typed into the model settings; the shipped models are suggested. A connection
  whose variables cannot be read is left out, and why is said wherever a model on it is
  missed. More than one can be set. The web pages never reach a server: the Settings page's
  **Save and test** saves the page and asks for a test, which the scheduler, looking at the
  database every five seconds, runs: for each model the settings name, Anthropic's too, the
  start-of-run check and one small request, with its time and whether the answer matched,
  written back line by line. The results page refreshes itself (`<meta refresh>`, no
  scripts) until the answer is in, and stops, asking whether the pipeline is running, when the
  test has not begun in two and a half minutes; it is not begun during the morning run
  (`forenoon test-models` from the terminal). Test calls are logged like any other. Three settings: every call uses `llm.model` (Claude Haiku
  5.5), except the choosing of the issue (section 7f), which can have its own
  (`llm.choose_model`): one small call a day that decides what the reader gets, where a
  stronger model costs cents. `llm.fallback_model` replaces a model for the whole run when,
  at its start, a two-second look at its connection's model list fails or does not list it
  (Anthropic is taken as reachable when its key is set);
  the report and the alert say so. Only an unreachable connection moves a run on, never a
  bad answer. Every call is logged and priced under the model it used, and logged as
  `unreachable` or `error` when it got no usable answer. The models offered, and their prices, are listed in `web/models.json`,
  shipped with the app and read-only: the web pages build it in for the Settings page's suggestions
  and the pipeline reads it for prices and for how each model is asked. The answer is a call
  of one tool, forced where the model allows it; a model that refuses a forced tool call
  (Sonnet 5.5, Opus 5.5, so marked in the file) or one not listed is asked for it in the
  prompt instead. An OpenAI-compatible server is sent the schema in strict mode (every
  object closed, optional fields made nullable) and in the prompt as well, since a server
  that enforces it by grammar does not show it to the model. Reasoning left in the text
  (`<think>`) and a Markdown fence are removed; an answer that is not a JSON object is asked
  for once more, saying what was wrong; an answer cut off at the answer limit is a bad
  answer. Ranking and reading-pass batches are sized to the connection's context size, less
  the whole answer limit: a local server may drop what does not fit without saying. The other
  calls are not batched and fit 16,384 tokens, the smallest context allowed, at the default
  settings; choosing the issue grows with the articles read in full, so reading many more
  than 40 needs a larger context. A connection that cannot be reached (refused, a key refused, or no model list at the start), or whose calls time out twice in a row, is not tried again for the rest of the run; a single timeout or server error loses only that call; a
  rate limit loses only the one call.
- **In the book.** Each chosen article shows "Picked because: …" under its byline and on the
  contents page, unless the reader turned that off on their Reading page.
- **Recorded per delivered article:** policy, the model that scored it, score, reason, rank among the day's candidates,
  whether it was the exploration pick, and the probability the policy put it forward.
- **Limits.** Before each call the day's logged token use plus an estimate for this call is
  checked against `llm.daily_token_cap` (600,000); over the cap, the call is not made and
  later batches are skipped; the cap counts cached input too, and only priced calls: one to
  a server you run costs nothing and is left out. Every call is logged with
  model, input, cache-write, cache-read and output tokens, outcome and cost. The cost is
  worked out when the call is logged, from each model's published prices in
  `web/models.json` (the API does not report prices), so cached input is priced at the
  cache's rates and a long prompt at the model's long-prompt rates. A model missing from the
  file is logged without a cost and reported as unpriced, never guessed. A price changed in
  the file applies to calls from then on. `forenoon usage`
  prints the log, today's total and this month's; the Settings page shows the totals.
- **Fallback.** The model is required; there is no way to run without one. With no API key
  (the alert names the env variable), or when fewer than half the candidates end up scored
  (failed or timed-out calls, unusable answers, the cap reached), the run uses the
  newest-first order, the report says why, and the operator is alerted. When some batches
  fail but at least half the candidates are scored, the scores are used, unscored candidates
  go last, and the report says how many were scored.
- **Safety.** Candidate text is untrusted and is passed as delimited data with an instruction
  to ignore anything in it that reads as an instruction. The model has no tools that act on
  anything and cannot affect recipients, sources or settings.

## 7b. Built-in sources: discovery (skipped when ranking falls back)

- **Sources.** Each is switched on or off on the Sources page; both start on. Hacker News: the front page, plus stories from the last `max_age_days` with
  more than 40 points (not just the last day: a story that gets its votes a day late would
  otherwise fall between two runs). Stories never seen before are taken first. Lobsters: the first three pages of its hottest list. Three requests per site per
  run; if a further page fails, the rest are still used.
- **Candidates.** Stories with a link to an article. Dropped: stories without a link, links to
  video and social sites or to files such as PDFs, anything older than `max_age_days`, and
  anything already delivered or previously failed. At most `discovery.per_site` (100) per site.
- **Sightings.** An article a feed or another front page already gave this run is not a second
  candidate: it is added to the first one's sightings. The model is shown every site's votes
  and comments together, and told that being picked up in several places counts in its favour
  (not a fixed bonus). Its place counts against whichever of its sources has the most room,
  so it is held back only when all of them have used their places. A find is credited "site
  via Hacker News and Lobsters", and every route is recorded.
- **Scoring.** Found articles go into the same ranking call as feed articles. The model sees
  the title, the site, points, comment count and (Lobsters) tags and description, since there
  is no abstract.
- **Selection.** Found articles are ranked with feed articles on their score alone, with no
  places kept for them. Like every source, each built-in one takes at most its "places a day"
  (no limit unless set on the Sources page). One that fails extraction gives its
  place to the next candidate, whichever kind it is.
- **In the book.** The source reads "site via Hacker News", with a link to the discussion.
- **Without the model** (no key, failing or over its cap) no found article is delivered.
- A listing that fails is reported and alerted like a failed feed.

## 7c. Extraction check by the model (log only)

- This covers the articles the reading pass (section 7f) did not see: pinned articles and
  pages brought in by a story. After extraction, one call per run sends the model each such
  article that is about to be delivered: title, word count, section headings, and the first
  120 and last 80 words.
- It answers `ok`, `suspect` or `bad` per article with a problem of at most 20 words, for
  example a paywall notice, a teaser, an ending cut short, or menu text.
- Verdicts are stored and shown in the run report; `forenoon checks` lists the articles it
  did not judge `ok`, beside the extractor's own verdict. **Delivery is never changed.**
- The call counts against the daily token cap and is skipped when it would cross it.
- It runs on `llm.model`: the verdicts change nothing that is delivered, so the routine model
  is enough. It always runs when ranking does.

## 7d. The browser container (optional)

- A separate container holds Chrome, the only thing that reads a subscriber session's cookie.
  It mounts the logins volume (read only), `<data>/sessions/` and a volume of its own,
  `forenoon-browser`, for what Chrome keeps of each session; never the database or the rest
  of the data folder.
- It looks for page requests in `<data>/sessions/.fetch/` every second and answers them one
  at a time, at least 20 seconds apart per site. Chrome runs unchanged on a virtual screen
  (Xvfb), not headless, because some sites admit only a browser. A page showing the site's
  paywall, a CAPTCHA or a browser check that does not let it through is answered with why,
  and nothing answers a CAPTCHA.
- How each session's last page went, with the day the site says its sign-in ends, is kept in
  `<data>/sessions/status.json` for the Sources page and the run's alerts.
- It also takes pictures of interactive figures: a `<figure>` the page's scripts draw (a
  chart, a slider comparing pictures), recognised by having controls, a canvas or a script,
  and no picture, player, table, code or quote of its own. Without the scripts it is often
  only its labels run together, so extraction puts a placeholder first in it, numbered by
  the figure's place among the page's figures (not counting any in `<noscript>` or
  `<template>`, as a browser running scripts counts them) with the page's total. For each
  article going in a book, the pipeline asks for up to 20 (fewer when the article's own
  pictures leave less room), with the site's session when it has one, and a deadline of 150
  seconds. The page is any site's, scripts and all, in the container that holds the
  cookies, so:
  - Chrome runs in its own sandbox, which the container's seccomp profile
    (`browser/seccomp.json`: Docker's default plus `clone`, `unshare` and `chroot`) allows.
    Without the profile, or on a kernel without user namespaces, no pictures are taken.
  - It runs in a child process killed at the deadline, whatever the page does.
  - It reaches the network only through a proxy in the container that connects to public
    internet addresses alone, by the address it resolves itself: https only, and never this
    machine, the LAN or the Docker host, whatever a redirect, socket, worker or DNS answer
    says. Service workers are blocked.
  Chrome refuses the page if its figures are not as many as the saved page's, opens any
  closed `<details>`, and photographs each figure as it first appears, caption hidden. The
  picture replaces what the figure held, with a line saying it is interactive on the
  original page; without a picture (none taken, or not kept for the article's picture
  budget) the figure keeps its own text, with the line. The container touches
  `<data>/sessions/.fetch/alive` every five seconds, and nothing is asked for while it is
  stale, so an install without it never waits; a page whose pictures time out is not
  followed by more requests for pictures in that run, and a request for a subscriber's page
  that waits behind a busy container does not count as the container being down.
- There were Calibre news recipes here until October 2026, fetching whole papers once a day.
  They went once every source had a feed: the subscriber sessions above fetch only what the
  ranking picks. Articles that came in through a recipe keep the route `recipe`.

## 7e. Stories

The daily cap counts stories, not articles. A story is one article, or up to
three articles on one subject delivered together. An article over 45,000 words is never
delivered, whether picked or reached through a link. It
is a failsafe for manuals and books; choosing well is the model's job. The number of articles in
a day's book is therefore a target, not a limit.

- **Articles about another page.** After the day's stories are chosen, one model call is
  shown each chosen feed article's opening words and
  up to eight of its links to other sites, and returns the links that are the article's
  subject: the page it quotes, announces, summarises or responds to. Usually that is none.
  Those pages are fetched, and the ones that extract cleanly join the story:
  - A post of fewer than 150 words is a pointer. The pages replace it: the post itself is not
    printed, the first page carries a "Found through …" credit, and the post is recorded as
    delivered so it is not offered again.
  - A longer article is a response or commentary. It is delivered first and the pages follow
    it.
  - When no page can be delivered, the article goes out as it was.
  - When the model cannot be asked (an outage, the day's tokens spent), no links are
    followed and the article goes out as it is.
  - Links to the article's own site, to video, social and chat sites, to files and to front
    pages are never followed. Discovered articles are not examined.
- **Same event.** One more model call compares the 40 best-scored candidates and returns those that cover the same specific
  event, announcement or release. When one of them is chosen, the others are delivered right
  after it as part of its story; any beyond the limit are dropped for the day rather than
  taking a slot of their own. Ids the model invents or repeats are ignored, and a failed call
  means every article is its own story.
- **In the book.** A story's later articles are listed under its first on the contents page
  and carry a "More on: …" line. The contents page gives the count of stories and articles. Each article keeps its own rating links.
- **Recorded.** Each delivery stores the story it belonged to. An article brought in by a
  story is recorded with the choice (score, reason) of the candidate that brought it.

## 7f. The reading pass

Ranking judges a title and an opening. Before the day is chosen, the model also reads the
best candidates in full.

- The `llm.read_candidates` (40) best-ranked candidates are fetched and extracted, taken in
  equal shares from every ranking batch (each batch's share by its own scores, since batches
  score on slightly different scales); the exploration pick is always among them. Their
  text, up to `llm.read_words` (2,500) words each, goes to the model about ten articles to a
  call, with the reader's interests and ratings.
- Per article the model returns a score from 0 to 10, a reason, whether the text is a
  complete article (`ok`, `suspect`, `bad`), how long it stays worth reading (`days` for
  news tied to the week's events, `months`, or `years`), and which of its links are its
  subject (section 7e, in place of the separate link call).
- The read articles are tried first, best score first; candidates that were not read follow
  in ranking order. The exploration pick keeps the day's last slot. Scores and reasons from
  reading replace the ranking ones in the book and in what is recorded.
- **Choosing the issue** (on `llm.choose_model` when set, else `llm.model`: one small
  call, so a stronger model here costs a few cents a day). One more
  call sees every read article
  at once: title, source, length, how long it lasts, the reading's score and reason, and a
  story id for those on one event; with the open places and each source's limit. It returns
  exactly that many, best first, with each one's broad subject, keeping to at most
  the reader's limit on one broad subject (2 by default, 0 for none; the hand-added
  and pinned articles, listed as already in the issue, count) and including a news story of major
  consequence when one scored 7 or more; within those, better articles first; and
  then reserves in the order to use them: as many as the places hand-added and pinned
  articles hold, plus two. Any it names beyond the places become its first reserves. The
  chosen are tried first, the reserves next (a hand-added or pinned article whose page
  cannot be had frees its place late, and the reserve takes it rather than the next score),
  then the other read articles in score order. The exploration pick still keeps the last
  place and is not offered to it. A chosen article's propensity is 1; one left out, reserves
  included, keeps only its chance of the exploration draw. A failed or unusable reply leaves
  the score order. It is skipped when there are no more read articles than open places. The
  run's report lists the choice and then the reserves, each with its subject and whether it
  was deferred earlier, and what became of each: sent, sent
  in another story, not needed (a reserve), or why not (its source had used its places, the
  day was full, the reading held it back, its page could not be had); and "issue chosen (n
  of m)" when the model named fewer than the places.
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

## 7h. Notes on each reader's taste

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
  9a). An edit is not a lock: the next rewrite starts from it. "Keep notes from my ratings"
  on the reader's Reading page turns it off for them: the notes are then neither rewritten
  nor given to the model, and are kept for when it is turned on again.

## 7g. The library: what is kept about every candidate

The pipeline keeps a record of what it considered, not only of what it sent. This is what
the web pages' Archive shows (section 9a).

- **How an article reached us.** Every route is recorded, and an article can have several:
  `feed` (a feed the install follows), `found` (on a built-in front page such as Hacker
  News), `linked` (followed from another article) and `manual` (added by a reader by hand),
  each with the name of the feed or front page; `recipe` on articles from the Calibre recipes
  there once were. A followed blog's post that also reaches a front page has both.
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

### Fetching an article again

Each row on the Archive page has **Fetch again**: after signing in to a site whose page was
refused, or once the extraction has been improved, so an article read badly can be read
again. The web program queues it and the scheduler works it with the added links, within a
minute. The page is fetched again as the article's source would fetch it (from its page,
with the feed's copy kept from the first time to fall back on, when the source is gone) and
saved, and the summary, length, thumbnail and extraction verdict are replaced. Ratings and
deliveries are not touched. When nothing can be read, what was there stays and the reason is
shown on the row for a day. At most 30 a day per reader.

## 8. Delivery

Books are emailed as attachments through an SMTP relay (STARTTLS, or TLS from the start with
`FORENOON_SMTP_IMPLICIT_TLS`; never plain text; with login) to the user's
Send to Kindle addresses. Recipients come only from the reader's own account page;
nothing in a feed or article can add or change a recipient. The sender address must be on
the Amazon account's approved sender list.

## 9. Reports and alerts

- **Run report.** Per source: a verdict (clean / degraded / failed / no items), counts, and
  the most common reason; per article: status, word count, extraction method, images dropped
  and reason. Also the number delivered and any delivery error.
- **Triage.** `forenoon report` extracts the latest items of every source without delivering
  or recording anything. It fetches every feed and page again, so `--source` limits it to the
  sources whose name contains the given text.
- **Alerts.** When alerts are on (`alerts.enabled`), the operator (`alerts.to`, else every
  admin) is emailed if a delivery fails, a run
  crashes, a run has nothing new to deliver (unless the day's cap was already used), or a
  source has failed a set number of runs in a row (default 3, repeated at each multiple).
  Alerts use the same relay; a failure to send one is logged and does not affect the run.

## 9a. Web pages

A separate program, `forenoon-web` (Rust, in `web/`), with its own image. It shares the
database with the pipeline.

- **Accounts.** Every page needs a session except sign-in, password reset (`/forgot`,
  `/reset`), email confirmation (`/verify`), joining by invitation (`/join`), rating links
  (`/f/`), the health check and a demo host's pages (below). An account is a reader: the
  pipeline makes an issue for each account that has joined, emailed to its Kindle addresses
  or, with none or with the daily email turned off, read on the web pages. A signed-in
  reader sees and changes only their own deliveries, ratings, settings and sources (section 2). The first account is
  created from the command line (`forenoon-web add-user <id> --email <address>`) and is an
  admin; there is no sign-up page. An admin has the Settings, Accounts and Problems pages,
  sees every source and session, and pauses sources for everyone. Each reader sets their own
  Kindle addresses.
- **Invitations.** On the Accounts page an admin invites a reader by email address, with an
  id (taken from the address when none is given), a name and a role. The account exists from
  then on, invited: no password, no sign-in, no reset email, and the pipeline delivers nothing
  to it. The address gets a link that works for seven days, once; the admin is shown the link
  too, for when the email does not arrive, and can send a fresh one (the old stops working)
  or take the invitation back, which removes the account. The join page shows the address,
  asks for a name, password, time zone and Kindle addresses, and signs the reader in; the
  address counts as confirmed, the account follows every shared source, and the reader lands
  on the Reading page. Kindle hints name the install's sender address (`FORENOON_SMTP_SENDER`)
  and link to Amazon's Personal Document Settings, where every Kindle must approve it.
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
  or read or everything, the feed or front page that brought it, how it arrived,
  the reader's rating, how long it lasts, a date range, and the order (newest, score, rating,
  length). Without a choice it shows what was delivered, added by hand or read by the
  model, 50 to a page. A row can be rated and sent to the Kindle like a newsstand card, and
  fetched again (section 7g). A form at the top adds a link by hand (section 7g). A reader sees only what was delivered
  to, added by or weighed for them.
- **Reading** (`/reading`). What the pipeline picks for: the reader's interests, in their
  own words, and their stories a day; how their issue is made (one book or one per article,
  the most on one broad subject, room for a surprise, the model's reason printed; section
  2); below them, the notes the pipeline keeps on the reader's taste (section 7h), shown as
  text to read with when they were last rewritten, a switch to keep them or not, a
  "Correct these notes" toggle that opens a box to edit them (2,000 characters at most),
  and the last 10 versions with who wrote each and a button to restore it. `/learned`, the notes' old address, leads here.
- **Navigation.** Every page a signed-in reader sees carries the same row of pages under the
  name: Newsstand, Archive, Reading, Sources, then for an admin Settings, Accounts and
  Problems (with the count of reports), then the reader's name, which opens the account page. The current
  page is marked. Sign out is on the account page. Below the row each page has its own
  controls: the newsstand's view switch (all, rated, not rated) and links that jump to each
  issue on the page; the Settings page's links to its sections.
- **Buttons.** The button that carries out a form is filled in the accent colour; "Send to
  Kindle" is outlined in it and set apart from the rating row. The small plain buttons are
  choices: ratings, reasons, finished. The pages work without scripts, so the Archive's
  filters apply when "Apply filters" is pressed.
- **Light and dark.** The pages follow the system's setting. A sun or moon button at the end
  of the masthead switches; the choice is kept in that browser, and choosing what the system
  already shows goes back to following it. It is the pages' one script; without scripts the
  button is not shown.
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
- **Account page.** Display name, time zone, Kindle addresses with "Email my daily issue to
  my Kindle" and "My Kindle has a colour screen", email, password, and sign out. A reader changes only their own row, so the only addresses an account can send to are
  the ones its owner typed. Interests and stories a day are on the Reading page.
- Every change is a form post that must carry the session's token and, when the browser says
  where the form came from, must come from the site itself, so another site open in the same
  browser cannot make one.
- Article text is untrusted here as elsewhere: everything is escaped, links and images must
  be plain `http(s)`, and the content security policy allows no script but the light/dark
  switch, named by its hash. Lead images are
  loaded by the browser from the article's own site, without a referrer.
- **Pictures.** The pipeline makes a small thumbnail for each article it prepares, from the
  page's lead image or else the article's first few pictures, and keeps it in `thumbs/` in
  the data folder. The web pages
  serve it, only to a reader the article was delivered to. A card falls back to the
  article's own lead image address when there is no thumbnail, and has no picture when the
  article has none. `forenoon thumbs` makes thumbnails for articles delivered earlier.
- **Demo hosts** (`FORENOON_WEB_DEMO`, `host=reader` pairs). For a public demo, an
  ordinary install's pages are also reached at one hostname per persona, each a reader of
  the install. A request for such a host needs no sign-in and sees that reader's Newsstand,
  Archive and Sources, as they are, with a banner saying so and links to the other personas
  in place of the reader's own pages; the persona's interests text is shown behind a click.
  The rating and Send to Kindle buttons are shown and do nothing; the forms that add, follow,
  edit or rate are left out, as are the Subscriber sessions. Every other address on a demo
  host, the sign-in page included, is 404, and so is any post. No session is started for a
  visitor and the pages carry `Cache-Control: public, max-age=300`, so a cache in front
  serves a busy day. A persona is shown as a reader whatever its role. The install's own
  hostname is unchanged. On start, a configured persona that is not an account yet is made
  from `web/demo.json` (a reader with its interests, no Kindle address and a password nobody
  knows), and when the install has no feeds at all the file's shared feeds are added and
  followed by every persona; nothing already there is touched, so later changes on the
  pages stand.
- **The landing host** (`FORENOON_WEB_LANDING`, a hostname). The project's public page,
  built into the program from `web/site`, is served at that hostname with its pictures, the
  icons and the health check; everything else there is 404. It is the one page meant to be
  indexed, and it carries `Cache-Control: public, max-age=300`. Unset on an ordinary install.
- Who owns what in the database: the web program owns accounts (with the readers' settings), sources, follows
  and sessions; the pipeline owns everything else and only reads the accounts. A Python
  test applies the web program's migrations and reads readers through them, so a column the
  pipeline needs cannot be dropped unnoticed. Of the pipeline's tables the web program writes only ratings and the
  tap log, by the same rules as the pipeline's `record_feedback`. Its queries are checked at build time
  against a copy of the pipeline's schema (`web/pipeline-schema.sql`), and a test fails when
  the copy and `app/store.py` differ.
- They are served publicly over https, through a Cloudflare Tunnel or the operator's own
  reverse proxy. With `FORENOON_WEB_SECURE_COOKIES` the session cookie is
  `__Host-forenoon_session`, marked Secure, and responses carry `Strict-Transport-Security`.
  A request for any host other than the https `FORENOON_WEB_URL` (the LAN address) is
  redirected to the same page there, since a sign-in cannot hold anywhere else; `/healthz`
  is answered wherever it is asked, and a request from a trusted proxy is never redirected.
  The client address for the sign-in and tap limits comes from the proxy's header
  (`FORENOON_WEB_CLIENT_IP_HEADER`) only when the request came from a trusted address
  (`FORENOON_WEB_TRUSTED_PROXIES`, default loopback, which is where a tunnel sharing the
  container's network connects from); for a list such as `X-Forwarded-For`, the last entry.
- Text responses are compressed (zstd, Brotli or gzip, as the browser asks). The form token is
  sent under a fresh random mask on every page, so compressed sizes cannot reveal it next to
  text a visitor chose (BREACH). Pages may not be framed, opened windows keep no handle on
  them, and other sites cannot load their responses (`frame-ancestors 'none'`,
  `Cross-Origin-Opener-Policy` and `Cross-Origin-Resource-Policy: same-origin`).

## 10. Commands

| Command                                         | Purpose                                                       |
| ----------------------------------------------- | ------------------------------------------------------------- |
| `forenoon run [--dry-run] [--out DIR] [--user]` | One delivery now; `--dry-run` builds files and sends nothing  |
| `forenoon schedule`                             | Run every day at `delivery_time`                              |
| `forenoon report [--per-source N] [--source X]` | Triage sources, or only those whose name contains X           |
| `forenoon thumbs`                               | Make the web pages' thumbnails for articles delivered earlier |
| `forenoon send-queue`                           | Fetch the links readers added and send what they asked for    |
| `forenoon feedback-log`                         | Print taps and current ratings                                |
| `forenoon usage`                                | Print recent model calls and today's token use                |
| `forenoon checks [--all]`                       | Print articles the model thought were badly extracted         |
| `forenoon replay [--all]`                       | Re-run extraction over saved pages and show what changed      |
| `forenoon golden [--out DIR]`                   | Write the rendering-test book                                 |

The web pages have their own program:

| Command                                                                                                   | Purpose                                    |
| --------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| `forenoon-web serve [--listen ADDR] [--secure-cookies] [--client-ip-header H] [--trusted-proxies LIST]`   | Serve the pages and answer rating links    |
| `forenoon-web add-user ID --email ADDR [--kindle ADDR] [--name] [--role] [--timezone] [--follow-nothing]` | Create an account; asks for the password   |
| `forenoon-web set-password ID`                                                                            | Change a password, signing the account out |
| `forenoon-web users`                                                                                      | List the accounts, invited ones marked     |
| `forenoon-web health`                                                                                     | Exit 0 when the local server answers       |

## 11. Storage

A copy of each book sent is kept in `issues/`, for good by default (`keep_issues_days`, as
for saved pages).

One SQLite database in the data folder, shared by the pipeline and the web pages: articles (with extraction verdict and preview data), the routes they arrived by,
the model's judgements per reader, deliveries, the tap event
log, current ratings, per-source failure counts, feed cache validators, and the web pages'
accounts and sessions. Older databases are upgraded in
place on start-up. Backing up the data folder backs up everything.

## 12. Deployment

- Images are published to GHCR by CI on every push to `main`: the app, the browser and the
  web pages. Containers run as UID 1000 with a read-only root filesystem and no
  added capabilities.
- Containers: the scheduler, the web pages (`WEB_IMAGE`), and optionally a `cloudflared`
  tunnel that shares the web container's network namespace and the browser for subscriber
  sessions (`BROWSER_IMAGE`). The web pages are published on a host port only with
  `WEB_LISTEN`, for an operator's own proxy.
- `deploy/docker-compose.yml`, or `deploy/deploy.sh` with plain `docker run`.
  `deploy/poll-deploy.sh` redeploys when the app image changes, replaces the web container
  and its tunnel when the web image changed, only the tunnel when just its image changed,
  and restarts the stack after a reboot.
- The Kindle browser needs TLS 1.2, so the public hostname's zone must not require 1.3, and
  `/f/*` must not sit behind a bot challenge or an access login.

See `docs/setup-deploy.md`, `docs/setup-email.md` and `docs/setup-sessions.md`.

## 13. Security properties

- Fetched content cannot choose recipients, run code, or reach private network addresses.
- Rating links accept only signed, expiring tokens and write only ratings. Emailed links
  (confirm, reset, invite) are stored only as hashes, expire, and work once.
- The web pages need a login and show each reader only their own data. Admin pages are
  public too, behind the same login and an admin role.
- Secrets are never read from or written to config files, reports or logs.
- The address check on fetches resolves names separately from the connection, so it does not
  stop DNS rebinding; the containers should not sit on a network with sensitive services.

## 14. Verified and not verified

- Verified on a Kindle Oasis (October 2026): delivery through Send to Kindle, rendering of
  text, headings, code, tables, lists, images including transparent ones, and rating taps
  through the tunnel.
- Embedded fonts survive Send to Kindle's conversion when set on the element itself; CSS
  ligature settings are ignored. Code in the embedded font, left-aligned and unhyphenated,
  verified in test books on 14 October 2026 (see kindle-books.md).
- Not yet checked on a device: the daily issue's contents page and chapter navigation, the
  link card's appearance.
- What has broken on the device and why: [kindle-books.md](kindle-books.md).

## 15. Not built yet

- Summaries; a ranker that needs no model call; shadow mode (a second model ranking beside
  the real run for a while, reported against the ratings); reasoning set per step rather
  than per connection.
- Searching Hacker News and Lobsters by topic rather than by popularity; a watch list for vendor
  announcements.
- The whole archive of a followed blog; full-text search of stored articles; expiring
  articles past their shelf life; a "throwback" section of older pieces worth a second look.
- A search agent, or an MCP server, over the library.
- Editing the prompts in the browser; changing a reader's role from the Accounts page (a
  public sign-up page is not wanted).
- Newsletters by email as a source.
- E-readers other than Kindle.
