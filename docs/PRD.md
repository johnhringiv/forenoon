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
    section 7b). An admin switches them on or off for the household. Each reader who follows
    Lobsters chooses what of it they read: its front page, any of its tags, or both, ticked
    in groups by the site's categories (a group with a tick in it is open); the row says
    "Front page", "Front page and 3 tags" or "3 tags". A reader who never chose reads the
    front page, and so does one whose every tag Lobsters has since dropped (shown ticked).
    Saving with nothing ticked is refused: unfollowing is how to stop, and the choice is
    kept for following again. The list
    comes from the pipeline: one that a new version adds appears switched off, so an upgrade
    never changes what is delivered by itself.
  - **Feeds**: RSS or Atom, by address.

  They live in the database. The pipeline reads each reader's at the start of their run; a
  reader who follows nothing gets nothing, and an alert says so. A feed has a name and:
  - pinned (by each reader for themselves): its articles are taken before all other
    sources, every one of them.
  - places a day (default 2; the field left empty for no limit, stored as 0, and a 0
    typed means the same): the most of an issue it may take. For a pinned
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
  its models and token cap, how many articles are read in full, the book check, alerts, what
  is kept) are
  on the web pages' Settings page (admins only) and live in the database. A setting stays
  only when it controls cost, depends on the deploy, or is a real choice; what used to be a
  switch for a later feature is now just how Forenoon works, described here.
- **Each reader's own settings** are on their Reading page: one book a day or one per
  article, the most articles on one broad subject (2; empty or 0 for no limit), room for a surprise
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
   - **its source's places a day** (Sources page; 2 for a feed, none for a built-in source;
     the field empty for no limit). An article carried by several sources counts against whichever has the
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
- **Picking the main content.** Readability (readability-lxml) picks the box with the most
  paragraph text, with these changes.
  - A list item a sentence long (80 characters or more, at most a quarter links) counts as a
    paragraph would, for the box holding the list, so an article written as one numbered list
    (a communiqué's points) is seen at all. Not an item that opens with a headline or link (a
    list of stories), nor a list in a box named as a sidebar, comments or related stories, or
    under the site's heading ("Most read").
  - Readability's step into an `<article>` inside its pick is undone when that `<article>`
    has others of its kind beside it: it is one card of a list.
  - When the best box opens with a heading (h2–h4) beside two or more boxes of its kind that
    open with one of the same level, the box holding those sections is taken instead (an
    article written as sections, which readability would otherwise join only when each scores
    a fifth of the best). Not when one of them is the site's: named so, its heading a link to
    another page, or worded as furniture ("Related posts", "About the author"); a heading
    that opens with references or notes is the article's.
  - Readability joins a box of the article's class when it is at most a quarter links, not a
    fifth (a podcast's first chunk with its show notes).
  - A table inside a table's cell (arXiv's two-line column headings) is kept.
  - Before readability reads the page: Tailwind's scroll margins (`scroll-mt-28`) are taken
    off every box, as readability reads "scroll" as a scrolling widget. The site's sign-in or
    registration box is dropped: named for one by a word of its class or id (`c13-register`,
    `forgotPassword`, `log-in`; not `blogInner`, nor a post's `tag-passwords` or
    `category-accounting`), holding a form or a password field and no `<h1>`, and at most 200
    words or mostly its form. So is a box of cards for the site's other stories: three or more
    `<article>`s side by side, each of at most 80 words under a headline that links to another
    of the site's pages, not in a live blog, with the heading over it.
- **The page's own word count** (used for the checks below and the "extracted N of ~M
  words" warning) is trafilatura's reading of the article, without the comments under it.
- **Split articles.** When the chosen content holds under 60% of a long page's text (500
  words or more), the smallest page element holding at least 90% of it is tried instead, and
  used if it recovers more without exceeding 1.3 times the page's text, and holds at least
  80% of the main content's paragraphs of 12 words or more. This recovers articles laid out
  as several sibling blocks, and is not a box that holds most of the page's words without
  the article (a site's menu, the comments under a post).
- **Continued articles.** When the page goes on with the article in a later box, its
  paragraphs are put back after the article: a box of the same kind and class nearby, in the
  same `<article>`, as when an advert splits the text in two, or the box right after the
  article's whose paragraphs are classed like the article's, as behind a "read more" button.
  Only a box of paragraphs (three or more of some length, mostly not links, not already in
  the article) counts, and the search stops at a heading between the boxes, at the next
  story's title, and at a box named as comments or page furniture.
- **Live blogs.** A live blog (a box named `liveblog` or `liveblog-body` holding three or
  more `<article>` entries) is taken entry by entry, newest first as the page has them, when
  that gives at least 1.5 times the words of the main content and holds what the main content
  kept. Entries that are mostly links, comments, a pinned entry shown twice, and relative
  times such as "1h ago" are left out.
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
  - Formulas are written once, as text: MathML, KaTeX and MathJax output, and TeX that the
    page renders in the browser (between `$$…$$`, `\[…\]` and `\(…\)`, an undelimited
    `\begin{align}…\end{align}` and the like, and `$…$` where the page's settings turn it on;
    only on a page that loads MathJax or KaTeX, or Substack's formulas, so a price in dollars
    is left alone). Letters are italic, exponents and indices
    `<sup>` and `<sub>`, fractions `a/b` and roots `√`; the page's own TeX macros are expanded.
    A formula that does not read in one line (a matrix, cases) keeps its TeX source once, in
    `<code>` (a code block when it is alone on its line). A TeX array of plain cells (a results
    table typeset as math) becomes a table. A display formula, or a formula alone in its box, is
    a paragraph of its own. Wikipedia's formulas are text where they read in a line, and its
    picture of the formula otherwise.
  - Lists that are part of the text are kept though they are mostly links (the main-content
    step drops such lists as menus): one a sentence introduces ("…please at least:"), or a
    short paragraph such as "References", "Sources" or "See also", or whose items say
    something beside their links (times, bylines and counts aside). Not one under a label or
    heading of the site's (related posts, tags, sharing, a newsletter, "Most read", "More
    news"), in a box named as the site's (recommendations, social links, a sign-up), holding a
    form or button, a table of contents, a run of one-word links, links to share the page or
    to social accounts, story cards (a link beside a teaser block), or one whose links go
    mostly to the site's other pages, unless a sentence introduces it and its items say
    something of their own.
  - Section headings marked up as links to themselves (permalinks), with ids that spell
    their text, or with scroll margins are kept, and so is a section whose id spells its
    heading ("typing-and-tools"), and a real table (with header cells or a caption) in
    scrolling boxes or under any heading. A heading an article ends on ("Related",
    "Comments", "Back to top") is not freed; one starting "References", "Sources" or "See
    also" introduces the list under it.
  - Short blocks are not dropped for being short: a code block in the boxes a highlighter
    nests it in, or beside a label ("stdout"); a line of the article in a box of its own
    where the page boxes each paragraph (a sibling box of the same class holds one too), with
    words of its own beside any link and not a site's pitch; a sub-list of short values in a
    list item.
  - A subheading alone in its box (the BBC's) moves to just before the first paragraph of the
    box after it, when that paragraph opens it, so it goes with its text.
  - A heading that is a sentence (twelve words or more, under half links: a Q&A's question
    citing its sources) is not taken for a link heading.
  - An article's opening paragraphs stay with it: a paragraph before the article's box with
    a link in its first sentence, and the paragraphs a box opens with when the best box is
    inside it (Cloudflare's intro before its boxes of quotes).
  - Loose text between blocks is read as the page shows it: each run of text and inline
    elements between two blocks is one paragraph, and two `<br>`s end one (Phoronix writes its
    articles so; readability cut each sentence at its links). An element holding a block (a
    custom element, `<font>`) is a block too. Only prose is made a paragraph: three words or
    more outside links (not counting scripts and styles), with a sentence's end or a lead-in's
    colon; a label, a toolbar's strings, a line of dates or "et al." is left as it was.
  - A heading that opens the page's copy and repeats the title the page declares (in
    `<title>` or `og:title`) is dropped once the copy is chosen and judged: the book prints
    the title above the article.
  - A `<source>` that the parser leaves open no longer swallows the rest of its box, outside
    a player, whose fallback text still goes with it.
  - Boxes that sell, credit or play are dropped by their markup: a sign-up form (one asking
    for an email address) and the box around it when that only pitches it; a short box named
    for a newsletter, a sponsor or the next story, with a call to act in it and not one of a
    run of such boxes; an advert card whose links all go to one address off the site, marked
    `nofollow` or `sponsored`; a box named as the author's at the end of the post that opens
    with a link to them and whose bio names them; a player's controls; a table of contents a
    script was to fill; and, at the end, a line that is only a short link to the page itself
    ("Share", "Read more") or to an author's page; and a pitch the article ends on, broken off
    where its list of the site's links was left out ("If you liked this article, please" over
    "subscribe to my newsletter"), with its heading when nothing else was under it.
  - The label a "skip past" link jumps to ("after newsletter promotion"), which is there for
    screen readers, is dropped with the link.
  - Asides that are not the article are dropped: a teaser (a short label and then only
    links, two or more, such as "Recommended" or "You may like") and one holding a form with an
    address field and little else (a newsletter sign-up). Never dropped: a box classed as a callout (a warning,
    a tip, "see also"), one of over 400 words or half the page's text, one holding something
    an in-page link points at (a footnote), and one whose label is an author's ("Update:", "References",
    "Correction:", "By", "Photo", "Part 2 of 3"). Pull quotes stay, as the editors set them.

  No id or class from the page reaches the book: the ids and link targets are generated.

- **Sanitising.** The body is reduced to an allowlist of elements (paragraphs, headings,
  lists, quotes, code, tables, figures, images, links and inline emphasis). Scripts, styles,
  forms, embeds, video, navigation and footers are removed. All attributes are dropped except
  link targets, image sources and alt text, table spans and list start numbers. Links and
  images must be absolute `http(s)` (or `mailto:` / embedded image data). Top-level headings
  become second-level, invalid nesting is repaired, and containers left empty are removed.
  A `<picture>` keeps its own image behind its `<source>` alternatives, and takes theirs when
  the image has no address on the web (none, or an embedded stand-in): the last address of the
  first source that has one (`srcset`, else `data-srcset`). An embedded (base64) 1×1 GIF or PNG as an
  image's `src` counts as no picture: it holds a picture's place until a script loads it. So
  does a placeholder file a script swaps out (named `placeholder`, `blank`, `spacer`,
  `transparent`, `pixel` or `1x1`, alone or after `grey`, `image`, `default` or `lazy`, as
  Monochrome's `image_placeholder.svg` and the BBC's `grey-placeholder.png`): the image takes
  its lazy-loading address instead (`data-src` and the like), or is dropped when it has none.
  An image in a figure with no address of its own but the link around it, holding nothing
  else, takes the link's address (Yahoo's blank pixel, its photo on the link). A picture in a
  button named for enlarging it (a lightbox's "Enlarge image", not a video's play button nor
  a reveal toggle) is taken out of the button, which the book drops. An `<object>` showing a
  picture file (`type="image/…"`, or a picture file's address: an SVG chart, an arXiv figure,
  an equation) becomes an image of it, or of the fallback image it holds, its text the alt
  text. A status badge (shields.io, badgen, a CI's `badge.svg`) is not kept. A
  character lxml refuses to set again (a control character such as a form feed in a kernel
  source's code, or U+FFFE/U+FFFF), written or as a reference, is read as a space in the page
  and the feed; the book check reads the page, and the model's HTML, the same way. A caption a figure also shows over its picture (an overlay, a lightbox's
  text) appears once.
- **Figure pictures.** Readability takes a box holding only a picture for empty, so a figure
  can keep its caption and lose its photo (a box of its own, a lightbox button). After
  readability, such a figure gets back the pictures of the page's figure with the same caption
  (the words a reader sees, its own and not a figure's within it; all its words when it has
  none); nothing is taken out. A figure within a figure gets none, nor gives any. The page is
  read as extraction has rebuilt it (an `<aside>` the article keeps as a quote is no longer a
  sidebar). The page's figures counted are those within the box holding the article's first
  and last paragraphs (of 40 letters or more, spaces not counted) when it is found, figures with no words only then; otherwise anywhere.
  Either way not those in a sidebar, footer or box of other stories (one holding half the
  page's words is the article's, whatever its name). Figures sharing a caption are paired in
  order, only when the page has as many as the article, and a pair only when the two sit
  next to the same paragraph of the article, before or after (one outside figures that the
  article and the page each have once); and on each side where the two have the same
  neighbour (that paragraph, or the article's start or end) the same figures must stand
  between it and them. The only figure with its caption on each side may pair beside no such
  paragraph on either. An article figure inside a box readability kept that is named as one
  of other stories gets none (its page figure is not counted).
  Only pictures the book would show come back. None for a video's figure (a player or embed,
  a player drawn by script, a picture that is or links to a video site's) or a figure of more
  than 8 pictures (a widget's grid, cards for other stories counted; one of more than 64
  gives none). Not a picture in
  anything else removed (a button, `<noscript>`), in a caption, an `<aside>`, a figure within
  the figure, or a card for another story (named so, or linking to another of the site's
  pages, or to another post where the article's address names its post in the query; a link
  to a picture file, to the photo's own attachment, media or gallery page, or to one under the
  article's address, is the photo's); nor a hidden one, an icon, logo or share button, one of
  48 pixels or less, an embedded one, a second size of the same photo, or one the article
  already shows. In a `<picture>` holding a blurred stand-in and the photo, only its last
  image is taken, with its sources. Of a graphic drawn once per screen width (ai2html's
  artboards, one shown at a time), only the widest copy is taken, and none when its labels
  are text drawn over the picture (a map's place names), which the book would lose. A page figure that is itself hidden, or named as a card for
  other stories or an icon, gives none; so does one inside anything the book removes, a
  player, a hidden box, a link to a video, a share link or a link to another story, up to the
  article's own box (or the page's body when that is not found). None goes into a figure
  holding a table, code or a quote.
- **Dropped figures.** Readability scores boxes by their text, so a photo in a box of its own
  between two boxes of the article's paragraphs (the BBC's, the New York Times') can go whole,
  caption and all. After it, each figure of the page within the box holding the article's
  first and last paragraphs goes back between the same two paragraphs (each of which the
  article and the page have once), but only where the article has those two paragraphs one
  after the other; among the figures it kept there, in the page's order. Not a figure that
  shares a box with a paragraph the article lacks and none it has (a card for another story
  readability dropped whole), nor one that, or a box around which within the article's box,
  is named as furniture, comments or other stories. It comes back with the pictures the book
  would show (as for figure pictures above) that the article lacks, and its own caption
  without text hidden from sight (the BBC's "Image caption, "), unless the article already
  has those words. When the article kept the figure's caption without its picture somewhere
  else (readability's copy can lack a credit line, so one caption's words holding the other's
  counts), the pictures go into that figure instead. A figure before the article's first
  paragraph or after its last (an author's photo, related stories) is not taken this way. The
  headings the article and the page have once count as such neighbours too, so a photo under a
  heading stays in its section. At most 40 pictures come back to one article.
- **The lead photo.** The photo a news page sets under its headline sits outside the box of
  paragraphs readability keeps, so it is put back: the picture the page names as its own
  (`og:image`), found on the page between the headline (or the top of the box holding the
  headline and the text, where a photo is set above the headline) and the article's first
  paragraph of 20 words or more. The address the book shows must be that photo too (not a lazy
  stand-in), or another crop of it among the same `<picture>`'s sources. It goes before the first
  of the article's paragraphs that the page shows after it, so a standfirst or a short first
  line stays where the page has it, and before any list or quote holding that paragraph. Its
  caption is the figure's own, or else a box named caption within a few boxes of the photo (Ars
  Technica, WIRED), unless the article has that caption already. The named picture and the one
  shown are matched by the file's name less the pieces that size or crop it (`-1200x630`,
  `-superJumbo`, `-Social`; a name that is only a size names nothing), or by an id in their
  addresses (a UUID, a long hex run) used by fewer than four of the page's pictures; an id alone
  does not match two files of different names when neither name holds it (Ghost's site id is in
  every address). Not taken: a picture the page does not name (an author's portrait), the named
  picture elsewhere (a card after the text), in a menu or the site's header (a `<header>` holding
  a menu or a link to the site's home, a banner, a masthead) when that does not also hold the
  headline, a video's poster, a hidden or tiny picture (as for figure pictures above), nor a
  second lead when the article already opens with a picture or has this one. For a picture's
  size, 0 means not given, unless it is the only size given. A lazy picture's stand-in file
  (`img-placeholder-md.jpg`) may carry a size.
- **Stacks of photos.** A carousel or slideshow shows one photo at a time; the book shows its
  first, with its caption, and skips the rest. A stack is a box named carousel, slider,
  slideshow, swiper, slick, glide, splide or flickity, or marked
  `aria-roledescription="carousel"` or `"slide"`. A gallery or lightbox (a grid of photos
  shown at once, a photo that enlarges) is not a stack. The copies of slides a looping
  carousel adds (`slick-cloned`, `swiper-slide-duplicate`, or hidden from screen readers) are
  never its first. Once a stack's first photo is seen (put back, or already in the article),
  the rest stay out. This holds for figure pictures and
  dropped figures alike.
- **Preferred-source badge.** A link holding only a picture, to a Google preferred-source page
  or a Google News publication page (asking readers to pick the site as a source), is removed.
- **Feed footers.** The lines a feed entry ends with about the post rather than of it are
  removed from the end of the article: a tags line ("Tags:", "Filed under:", "Topics:",
  "Categories:", mostly links), a short "Via …" line with a link, and WordPress's "The post …
  appeared first on …". Only at the very end, and only lines of those shapes.
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

An SVG is drawn with resvg at twice its size on the page (its width and height, else its
viewBox, an icon's `0 0 24 24` being 24 pixels), within the same 1200×1600, then converted
like the rest; one under 6 pixels on
both sides is dropped, as an equation set as SVG can be one letter. Its text is set in Arimo
and Tinos, which take the room Arial and Times New Roman do, so labels drawn for those fit
their boxes, and code in JetBrains Mono NL; no system font is read. Before drawing, what
would load anything from outside the file is taken out: entities and DTDs are not read,
links to pictures other than embedded PNG, JPEG, GIF or WebP, stylesheet imports, scripts
and embedded HTML are removed, and relative addresses resolve to an empty folder. One of more
than 50,000 elements is not drawn. Each is drawn in a process of its own, limited to 1 GiB of
memory, 30 seconds of processing and 45 seconds in all, with none of the pipeline's
environment (its keys), as a hostile SVG can ask the renderer
for more memory than the machine has, crash it, or keep it busy for good; one that fails is
dropped like a picture that cannot be decoded. A picture
a line of text high (96 pixels or less as drawn: an equation) does not count toward the 40,
so a page of equations keeps them all; once the 40 are used, only SVG addresses are still
fetched, and at most 200 pictures are fetched for one article, in at most five minutes.

## 6. The book

- **Format `issue` (default).** One EPUB per day, titled `Forenoon · YYYY-MM-DD`:
  - a cover image for the library view: the Forenoon mark, the date, the story count and the
    first few headlines, drawn in grayscale with no font files needed;
  - a contents page with the date, article count and total reading time, then each article's
    title, source and reading time;
  - one chapter per article: title, author, source, date, reading time, a link to the
    original, the body, then the rating links (section 7); a "Contents" link at the top and
    bottom;
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

A rating is 1 to 5, with an optional note in the reader's own words. The number says how
much; the note says why, when the reader cares to.

- **The scale.** 1: not for me. 2: not worth its place. 3: fine. 4: worth its place. 5: more
  like this.
- **In the book.** Each article ends with "How was it?" and one row of five links, 1 to 5,
  with the ends of the scale under it ("1: not for me. 5: more like this."). One tap records
  the rating.
- Each link carries a signed token (HMAC-SHA256, truncated to 16 bytes) naming the user,
  article, signal (`r1` to `r5`) and expiry (default 60 days after delivery).
- The web pages answer `GET /f/<token>`: they verify the token, record the tap and return a
  small page stating what is now stored ("Rated 4 of 5."), then a box for a note, then
  "Change it:" and the row of 1 to 5 listed whole, the rating given filled in black and every
  other number a link that changes it. Besides sign-in, the forgotten-password and email-link
  pages and `GET /healthz`, it is the only page that needs no login. The only input it takes
  is the note, posted to a link of its own (`note`), signed for the same reader, article and
  expiry; opening that link only shows the page.
- **A note (optional).** Once there is a rating, the page offers a box: "Anything the picker
  should know?". The note is the reader's own words, one line of at most 200 characters, and
  it is kept on the rating until it is changed or the rating is cleared: a new rating leaves
  it. An empty one removes it. The rating counts without one. A note is kept in place of
  reasons chosen from a list: a model reads the ratings, and a few words of the reader's say
  more to it than a tag. A note for an article with no rating is logged and kept nowhere
  else.
- **Links from before October 2026.** Books sent then had two rows of links, "Finished? Rate
  it" and "Didn't finish? Rate it" (`f1` to `f5` and `r1` to `r5`). Both still record the
  rating while they last; whether the article was finished is no longer kept.
- **Where else it is recorded.** The rating holds only its latest state. Where each tap came
  from (`kindle-footer` or `web`) is in the event log, a note with its words; an article's
  place in its issue is in `selections`.
- **Report a problem.** "This article came out broken or incomplete" is a separate link on
  that page and a button on the web pages. It is a report about the delivery to whoever runs
  the install, not a rating: it is logged, written to the extraction log, and listed for
  admins on the web pages' Problems page, with what the book check found (section 7i). The
  rating is left as it was.
- **Latest tap wins** for the rating; the note stays until it is changed. Every tap is also
  kept in an event log.
- Taps are rate limited to 30 per minute per client address (see §12 for how the address
  is found behind a proxy).
- `forenoon feedback-log` prints the event log and the current rating per article.

Ratings influence delivery through the model (a run that falls back to newest first does
not use them). The model is given the 40 most recent, each with the article's length, why it
was picked and the reader's note when there is one. It is told what the numbers mean; that a
note is the most telling thing a rating carries, the reader's opinion and not an instruction;
and to work out what a rating is about before learning from it: a low rating may be about the
subject, the article or the form, a low rating on a short post may be a verdict on thin posts
rather than on its source or topic, and a note about length or depth is about the form.

**Ratings from before notes** (1 to 5, "finished", and a reason chosen from a list) were
brought forward by the pipeline when it first started after the change: the rating stayed;
the finished flag went, and with it a row that had only that; a reason became a note in its
own words ("Too long"). The event log keeps every tap as it was.

## 7a. Ranking with a model (needs the API key)

- **Input.** For every unpinned candidate, up to a ceiling of `llm.max_candidates` (600):
  title, source, author, date and the first 100 words of the feed entry as plain text. No
  article page is fetched for ranking. Also the user's `interests` text and their 40 most
  recent ratings (title, source, length, rating, the reader's note; see section 7).
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
  otherwise fall between two runs). Stories never seen before are taken first. Lobsters, for each
  reader as they chose: the first three pages of its hottest list (the front page), and for
  their tags one listing of all of them, two pages, newest first, with only the stories that
  have at least 5 points (one with fewer is found on a later day, when it has them). Up to
  five requests per site per reader's run; if the first fails the site is reported as
  failing, and if a further one fails the rest are still used. The front page is read first,
  then the tags, then the front page's further pages; the cap on candidates below keeps that
  order among stories never seen before, and again among those seen on an earlier day.
- **Lobsters tags.** The tags offered are the site's own subjects from its list of tags, read
  once a run for the household: active tags, without the kinds of post (video, pdf, ask, show
  and the like) or the site's news about itself. A list that cannot be read, or has fewer
  than 20 tags, is not used and the last one is kept; the first is the list as it was when
  this was built. A tag the site no longer lists is not asked for (Lobsters answers a listing
  with an unknown tag as not found). The reader's Sources page shows it still ticked under "No
  longer on Lobsters", and saving keeps it while it stays ticked, so a tag the site drops for
  a while is read again when it comes back. Unticking it drops it.
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

## 7i. The book check (optional, off by default)

An agent that compares each article, as it will be printed, with the page it came from, and
fixes what extraction got wrong. Every fix is logged as a problem, so the extractor can be
improved for the next article like it. "Check every article with an agent before it is sent"
(`llm.book_check`) on the Settings page turns it on; `llm.check_model` is the model (Claude
Sonnet 5.5).

- **When.** For each article about to go in a book, as it is assembled: after the pictures of
  interactive figures are put in, before its images are fetched. Not in a dry run, not for
  an article that will be held back anyway (judged not the article, or over the word limit),
  and not for one sent again from the web pages. An article with no saved page (a feed's own
  text, or saved pages turned off) is not checked; the report counts it.
- **Only an Anthropic model**: the check needs tool use with pictures, through Anthropic's own
  client and key. With any other model the check is skipped and the run report says why.
- **What it is given**, all marked as untrusted data whose instructions are to be ignored: the
  title, source, address and word count, and which copy the article is (the site's feed entry
  or the web page); the article as numbered blocks (paragraphs,
  headings, lists, quotes, code, figures, tables and notes, each with its HTML; an embedded
  picture is written `picture:N`), whole up to 30,000 characters and shortened after, each
  shortened block marked as such with the tool that shows it, so a listing that stops is not
  taken for an article that does; and the
  first 8,000 characters of the saved page's visible text (no scripts, styles or navigation)
  with its total length.
- **Its tools**: `page_find` (where a phrase is on the page, with 300 characters around each
  of up to 10 places), `page_read` (up to 6,000 characters from an offset), `page_view` (a
  screenshot of the live page, section 7d; unavailable while the browser container is not
  running), `page_copy` (the page's own HTML from the block where one phrase is to the block
  where another is after it, reduced to the allowlist and kept as `copy:N`; a start the page
  repeats, such as a heading over each section, is the one nearest before the end), `article_read` (blocks in
  full, up to 16,000 characters, saying where it stops), `replace_blocks`, `delete_blocks`,
  `insert_after`, `insert_from_page` and `replace_from_page` (a copy made and placed in one
  call), and `finish` (a verdict, `ok`, `fixed` or `cannot_fix`, a summary of at most
  300 characters, and each problem found: its kind, what was wrong and why it thinks so). The
  kinds are cut short, missing content, page furniture, broken media, formatting, wrong
  article and other. Blocks are numbered afresh after each edit, and the edit's answer lists
  the blocks around it with their new numbers.
- **The feed's copy.** When the article is the feed's copy (it has 85% of the page's words,
  section 4), it is told that feeds often leave out what the page shows around the text
  (pictures and captions, embedded posts, notes), to look for those on the page and restore
  them when they are the article's: of an embedded post, its author and text, without its
  counts of views, replies, reposts or likes, its buttons or its "Read more".
- **What it may do.** It is told to edit only to restore what the page has or remove what is
  not the article, and never to rewrite, summarise or add to the author's text; to restore with
  `insert_from_page` or `replace_from_page`, so the page's links, emphasis, heading levels and
  quotes come back as the page has them (a copy from `page_copy` may also stand between blocks
  of its own HTML as `copy:N`, never inside one); to write HTML itself only to fix formatting the
  page's markup does not give, or for a passage the tools cannot find; and to change only the
  blocks that are wrong. An edit made from copies is recorded with their numbers. Everything it writes goes through the extractor's allowlist (section 4) before it is put in the article;
  links to the article's own notes and headings stay links in the book, and a link left
  pointing at nothing is unwrapped. An image must be one the article has or one whose address
  is on the saved page; it is fetched and converted with the rest (section 5).
- **Limits.** At most 12 calls for an article, 200,000 tokens (input, cached input and
  output), three minutes, and the check's own daily budget (`llm.check_daily_tokens`,
  3,000,000), checked before each call. It is apart from `llm.daily_token_cap`: the check's
  tokens never count against the ranking's, so a busy morning of checks cannot leave the
  next run without the model; the settings page shows the two apart. `finish` is taken only as a turn's one call, so the verdict is given
  knowing how the edits went. Its edits are kept only when it finishes: at a limit the check
  is recorded as `incomplete` and the article goes out as it was. A failed call, a refusal or anything else
  that goes wrong is logged and leaves the article as it was; a delivery is never stopped by
  it. Each call uses adaptive thinking at medium effort, with the system prompt, the tools and
  the conversation so far marked for the prompt cache.
- **Where it is logged.** Every call in the model log (`book-check`, with its cached tokens,
  priced like any other); every check in the `book_checks` table (verdict, summary, the
  problems, each edit with the HTML before and after, and its tokens); `fixed` and
  `cannot_fix` on the Problems page (section 9a) with the summary, each problem, and whether
  the article was edited; and in
  the run report, how many were checked, with each verdict, their tokens and cost.
- **Cost**, at Sonnet 5.5's prices: the first real run (20 recent articles, 18 October 2026) fixed 6 and found 14 fine in 38 calls and 502,105 tokens, 62% of them read from the
  cache: $0.60 in all, about 3 cents an article, roughly $0.30 a day for a reader with ten. The delivery waits for the checks, up
  to three minutes an article.

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
  and no picture, player, table, code or quote of its own; and of drawn figures, which the
  book would lose: an inline SVG of 20 elements or more (a chart, a diagram) or an embedded
  page that is not a video, a player or a post (Spotify, SoundCloud, a podcast host,
  Instagram, TikTok, X and the like) but a Flourish chart or a demo, with none of those
  either, and not a player's (named so: its controls are SVG). The room these pictures have in
  the article's budget leaves out its SVG pictures, mostly equations. A drawn figure's picture comes with no line
  under it; without one, the line sends the reader to the original page for the figure. Without the scripts it is often
  only its labels run together, so extraction puts a placeholder first in it, numbered by
  the figure's place among the page's figures (not counting any in `<noscript>` or
  `<template>`, as a browser running scripts counts them) with the page's total. For each
  article going in a book, the pipeline asks for up to 20 (fewer when the article's own
  pictures leave less room), with the site's session when it has one, and a deadline of 150
  seconds. The page is any site's, scripts and all, in the container that holds the
  cookies, so:
  - Chrome runs in its own sandbox, which the container's seccomp profile
    (`browser/seccomp.json`: Docker's default for 64-bit x86 and ARM only, plus `clone`, `unshare` and `chroot`) allows.
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
- It also shows the book check's model a page as a reader sees it (section 7i): one
  screenshot of an 800 by 1200 screen, scrolled so a phrase the model names is in its middle
  (or the top of the page), as JPEG of at most 1 MB. It is taken the same way as the pictures
  of figures (the sandbox, the child process killed at the deadline, the public-only proxy),
  of the article's own page only, with its site's session when it has one, and waits its
  turn behind the 20 seconds between pages of one site.
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
  at least 10 ratings and at least 5 new ones since the last time; a note written or changed
  counts as new. One model call.
- **From what.** The interests, the notes as they stand, and the reader's latest 200 ratings
  with each article's length, why it was picked and the reader's own note. Titles are
  untrusted and are given only as material.
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
  already been sent, has rated, or added by hand as read.

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
- **Only keep it: I have read it.** Kept, with the reader's rating when given. It is never
  sent, and the rating counts with the others when the model chooses.
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
- **Deleting an account.** An admin can delete any joined account but their own, from the
  Accounts page, so an install always keeps an admin. It is behind a disclosure that says what
  goes. The account goes with everything recorded for it: deliveries, selections,
  assessments, book checks, ratings, rating events and problem reports, requests, reading-profile history,
  follows, Lobsters tag choices and email links. Its browsers are signed out, and the rating links in books it was
  sent stop recording anything, even if a later account is given the same id (a link counts
  only for an article delivered to that account). The articles stay, since other readers share them, and the
  sources and site logins it added pass to the admin who deleted it; the run reports in the
  data folder, and the copies of books kept there, keep its id. A daily run, Send to Kindle or
  added link already under way when it is deleted sends it nothing more than a message already
  going out, and drops or refuses what it would have written. There is no
  undo.
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
  the reader's time zone. Within an issue the cards follow the book's order (the pipeline
  records each delivery's place in it); picks shown only as links come after. Each card shows source, author, title linked to the original, lead
  image and description when the page had them, word count and reading time, how it was
  chosen (score and rank, pinned, exploration pick, the model's reason) and its rating.
  `?show=rated` or `?show=unrated` filters it. A story's articles share one card: the lead
  article, with the others delivered with it listed beside it, each still rated on its own.
- **Ratings can be changed there**: set or change the 1 to 5 rating, write or change the
  note, or clear the rating; pressing the rating already given records nothing. Changes are
  logged like taps, with `web` as their source. "Add a note" beside the rating opens the note's
  box; a saved note is shown as a quotation with Edit beside it, which opens the box in place
  with the note in it. Cancel closes the box and forgets what was typed. Save is off until the
  words differ from what is kept.
- **Archive** (`/archive`). The reader's library as a list, one row per article: everything
  delivered to them, and everything the model weighed for them and passed over (section 7g).
  A row shows the source, the title linked to the original, the summary when there is one,
  the model's reason, whether it was delivered, read by the model or only weighed from its
  headline, the score, the length, how long it stays worth reading and every way it reached
  us. Filters are a form sent by GET, so any view is a link: words in the title, delivered
  or read or everything, the feed or front page that brought it, how it arrived,
  the reader's rating (rated, not rated, 4 or 5, 1 or 2), how long it lasts, a date range,
  and the order (newest, score, rating, length). Without a choice it shows what was delivered, added by hand or read by the
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
- **Problems** (`/problems`, admins only). The articles readers reported as broken or
  incomplete (section 7), and those the book check fixed or could not fix (section 7i) with
  its summary and each problem it found, newest first, each with its word count and what the
  extractor made of it.
- **Navigation.** Every page a signed-in reader sees carries the same row of pages under the
  name: Newsstand, Archive, Reading, Sources, then for an admin Settings, Accounts and
  Problems (with the count of reports and book check findings), then the reader's name, which opens the account page. The current
  page is marked. Sign out is on the account page. Below the row each page has its own
  controls: the newsstand's view switch (all, rated, not rated) and links that jump to each
  issue on the page; the Settings page's links to its sections.
- **Buttons.** The button that carries out a form is filled in the accent colour; "Send to
  Kindle" is outlined in it and set apart from the rating. The small plain buttons are
  choices. A rating is the five numbers as one joined control, the one given filled in, with
  "Clear" beside it once there is one. Under it, once there is a rating, a one-line box for a
  note, in a form of its own so that Enter saves the note and presses nothing else. The pages
  work without scripts, so the Archive's filters apply when "Apply filters" is pressed.
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
  be plain `http(s)`, and the content security policy allows no script but the page's own
  (the light/dark switch and the note box), named by its hash. Lead images are loaded by the
  browser from the article's own site, without a referrer.
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
  icons, `/robots.txt`, `/sitemap.xml` and the health check; everything else there is 404.
  It carries `Cache-Control: public, max-age=300`, and Cloudflare's Web Analytics beacon for
  forenoon.dev, the one script its policy allows. It and the demo hosts' three pages are the
  only pages meant to be indexed: the sitemap lists them all, and each demo host's robots.txt
  names it. Each demo page's title names the persona and the page ("Mara's archive ·
  Forenoon demo"), and its canonical link is its own https address without a query. The
  icons and the share card may be indexed on those hosts too, as may the landing page's
  pictures; thumbnails, other sites' pictures, are disallowed in robots.txt and keep their
  noindex. Unset on an ordinary install.
- Who owns what in the database: the web program owns accounts (with the readers' settings), sources, follows
  and sessions; the pipeline owns everything else and only reads the accounts. A Python
  test applies the web program's migrations and reads readers through them, so a column the
  pipeline needs cannot be dropped unnoticed. Of the pipeline's tables the web program writes only ratings and the
  tap log, by the same rules as the pipeline's `record_feedback`. Its queries are checked at build time
  against a copy of the pipeline's schema (`web/pipeline-schema.sql`), and a test fails when
  the copy and `app/store.py` differ. The pipeline brings its own tables forward when it
  starts; until it has (a fresh install, or the first start after a change to them) the web
  pages show no issues and answer no rating links, rather than erring.
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
- Text responses are compressed (Brotli or gzip, as the browser asks), the landing page,
  robots.txt and the sitemap included. Not zstd: Cloudflare takes only Brotli or gzip from
  an origin, and Brotli is the smaller of the two on these pages anyway. Every response carries
  `Cache-Control: no-transform`, so a proxy in front (Cloudflare's) passes the encoding on
  rather than decompressing the response and compressing it again. The form token is
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
