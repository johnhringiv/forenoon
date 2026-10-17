# The public demo: personas and sources

The demo at forenoon.dev is an ordinary Forenoon install with three readers and no Kindle
addresses, shown read-only at one hostname each (`FORENOON_WEB_DEMO`; see
[setup-deploy.md](setup-deploy.md)). Every morning the three share one pool of candidates and
each gets a different issue. This page is the install's configuration in prose: who the readers
are, what they say they want, and what they draw from. Change it here first, then on the
install.

## What the demo has to show

- **The same pool, three issues.** The pitch is that the choosing is personal. A visitor who
  opens two newsstands side by side should see different leads, with a shared story only when
  something big happened. So the personas are far enough apart to pick differently, and the
  sources are broad enough that each persona has plenty to choose from.
- **Reasons on every card.** The model's one-line reason is what makes the picks legible.
  Interests written in concrete terms produce concrete reasons.
- **Pointer posts replaced by articles**, several reports of one event grouped as a story,
  and a passed-over archive with reasons. Hacker News and Lobsters supply the pointers;
  mainstream news feeds supply the duplicates.
- **Nothing private.** A persona's Archive shows everything weighed for it, and its
  interests are shown on the page. No subscriber sessions, no paywalled feeds.

## The personas

Each is a reader account with 8 stories a day, the daily issue format, grayscale pictures and
the exploration pick on, which are the defaults. The interests text is what the model reads.
It is written as a reader would write it, in the first person, with what to prefer and what
to skip, because that is what produces the best reasons.

### Mara (`mara.forenoon.dev`)

Systems programmer. Reads for depth, not news.

> I write systems software: Rust mostly, some C and Zig, and I spend a lot of time inside
> databases and compilers. I want long technical posts that explain how something actually
> works: a storage engine's write path, a borrow checker's rules, a GC pause traced to its
> cause, a benchmark with the methodology shown. Postmortems and debugging stories are my
> favourite genre. Hardware is interesting when it changes how software is written (new
> memory hierarchies, SIMD, GPU programming models). I like the people side of this world
> too: how a team maintains a large codebase, how open source projects make decisions.
>
> Skip: product launches and funding news, listicles, beginner tutorials, "10 tips" posts,
> anything about AI that is not a concrete engineering result, and general tech news unless
> it is about a security hole with real technical detail. One good 30-minute read beats five
> short ones.

### Theo (`theo.forenoon.dev`)

Policy and economics reader. Wants reported pieces and arguments with numbers.

> I follow economics and public policy the way other people follow sport: housing and land
> use, energy and the grid, industrial policy, trade, central banks, how cities and
> institutions actually work. I want reported pieces and well-argued essays with data in
> them, from any side, and I like a long explainer on something unglamorous (permitting,
> transmission lines, procurement, pensions). History of institutions and infrastructure is
> very welcome. I will read a good piece on technology when it is about its economics or
> regulation rather than the technology itself.
>
> Skip: hot takes, partisan point-scoring, market wraps and daily price moves, personal
> finance, and anything about crypto. A few genuinely major news events a week are enough; I
> am not trying to keep up with the day.

### June (`june.forenoon.dev`)

Science and long-form reader. A little news when it matters.

> I read for pleasure: science writing, natural history, medicine, the history of ideas,
> and long reported features about people and places. I like a piece that follows one
> researcher or one question for a few thousand words, essays that take an unexpected angle,
> and good explanations of new results in physics, biology, climate or neuroscience written
> for an intelligent non-specialist. Book-length thinking in article form. Some technology
> is fine when it is about what it does to people.
>
> Skip: press-release science ("study finds"), health advice, productivity and self-help,
> and anything that needs me to already follow a field's gossip. I want one or two important
> news stories only when something has really happened; otherwise no news at all.

Why these three: they overlap at the edges (Mara and Theo on the economics of technology,
Theo and June on history, Mara and June on a well-told research story) so the demo shows the
model drawing lines, not sorting by category.

## The sources

One shared pool, 24 feeds plus the two built-in front pages. All plain RSS or Atom, free to
read, with full text on the page; no subscriber sessions. Each feed is shared with the
household and followed by all three personas, at the default of two places a day, so no single
source can fill an issue. The built-in front pages have no limit, as usual.

Built in: **Hacker News** and **Lobsters**. They are where the pointer posts come from.

Systems and software (mostly Mara's):

| Feed                    | Address                                         | Why                                           |
| ----------------------- | ----------------------------------------------- | --------------------------------------------- |
| Simon Willison's Weblog | https://simonwillison.net/atom/everything/      | Short and long; a test of the length judgment |
| Julia Evans             | https://jvns.ca/atom.xml                        | Debugging stories, explained from scratch     |
| Dan Luu                 | https://danluu.com/atom.xml                     | Rare, long, exactly Mara's taste              |
| fasterthanli.me         | https://fasterthanli.me/index.xml               | Very long Rust pieces                         |
| Chips and Cheese        | https://chipsandcheese.com/feed                 | Hardware with methodology                     |
| LWN.net                 | https://lwn.net/headlines/rss                   | Kernel and open source governance             |
| Ars Technica            | https://feeds.arstechnica.com/arstechnica/index | Broad tech news; mostly passed over, visibly  |

Economics and policy (mostly Theo's):

| Feed                    | Address                                                   | Why                                       |
| ----------------------- | --------------------------------------------------------- | ----------------------------------------- |
| Construction Physics    | https://www.construction-physics.com/feed                 | Infrastructure explainers                 |
| Works in Progress       | https://worksinprogress.co/feed                           | Long essays on policy and progress        |
| Noahpinion              | https://www.noahpinion.blog/feed                          | Argued economics, free posts              |
| Marginal Revolution     | https://marginalrevolution.com/feed                       | Many short pointer posts: tests following |
| Apricitas Economics     | https://www.apricitas.io/feed                             | Charts and data                           |
| Statecraft              | https://www.statecraft.pub/feed                           | Interviews on how government works        |
| The Guardian: Long Read | https://www.theguardian.com/news/series/the-long-read/rss | Reported long form, free                  |

Science and long form (mostly June's):

| Feed              | Address                              | Why                                        |
| ----------------- | ------------------------------------ | ------------------------------------------ |
| Quanta Magazine   | https://www.quantamagazine.org/feed/ | New results, explained properly            |
| Nautilus          | https://nautil.us/feed/              | Essayistic science writing                 |
| Aeon              | https://aeon.co/feed.rss             | Ideas and history essays                   |
| Undark            | https://undark.org/feed/             | Reported science and its politics          |
| Knowable Magazine | https://knowablemagazine.org/rss     | Reviews of a field for non-specialists     |
| Longreads         | https://longreads.com/feed/          | Pointers to long features: tests following |

Close to home, both essayists whose pieces cross the personas' lines:

| Feed                    | Address                            | Why                                                    |
| ----------------------- | ---------------------------------- | ------------------------------------------------------ |
| John H. Ring IV         | https://johnhringiv.com/feed       | Forenoon's author: homelab, networking, software       |
| A Cathedral in the Wild | https://shearerp.substack.com/feed | Paul Shearer's essays on ideas and culture, free posts |

News, for the "only when it matters" rule all three share:

| Feed                | Address                               | Why                                      |
| ------------------- | ------------------------------------- | ---------------------------------------- |
| BBC News            | https://feeds.bbci.co.uk/news/rss.xml | Wide, free, clean pages                  |
| The Guardian: World | https://www.theguardian.com/world/rss | Duplicates with the BBC become one story |

Left out on purpose: the Financial Times, the New York Times, The Atlantic and Wired, which
need a subscription or truncate without one; Substacks with paid posts in the feed; and
anything whose terms forbid this kind of reading.

## Setting it up

1. `forenoon-web add-user mara --email mara@forenoon.dev --name Mara --timezone America/New_York`
   and the same for `theo` and `june`, with no `--kindle`. The emails are never used; a
   password is asked for and can be anything, since the demo hosts need no sign-in.
2. Sign in as the admin on the install's own hostname and paste each persona's interests on
   its Reading page, or set `interests` on the `users` rows with `docker exec`.
3. Add the feeds on the Sources page, shared with the household, so all three follow them.
   Check the Sources page the next day for any that failed and replace them.
4. Set `FORENOON_WEB_DEMO=mara.forenoon.dev=mara,theo.forenoon.dev=theo,june.forenoon.dev=june`
   in `.env`, route the three hostnames to the web pages, and restart the web container.
5. After the first run, open the three newsstands side by side. If two leads match on a
   quiet day, the interests are too close: sharpen the "skip" lines first.

The landing page (`site/`) names the personas and links to the three hostnames; keep the
names and one-line descriptions there in step with this page.

## The landing page

The landing page is served by the web program itself, at the hostname in
`FORENOON_WEB_LANDING` (`forenoon.dev`), so the public site is the same deploy as the demo
behind it and needs nothing else running. On a persona host the wordmark at the top left
leads back to it, so a visitor who came in from the landing page can return. It is one HTML page with its styles inline and no
scripts, built into the binary from `web/site` with its pictures: a merge to `main` that
changes it reaches the site with the next image. Only the page, its pictures, the icons and
the health check answer on that host; everything else there is 404. The Kindle pictures are the
device's own screenshots (1264 by 1680) saved as WebP; the newsstand picture is the README's,
taken with `tools/screenshots.py`, and should be retaken from a persona's demo host once the
demo install runs, so that it shows the persona switcher rather than an admin's pages.

Route the landing hostname to the web pages like the persona hostnames. The page and the
persona pages carry `Cache-Control: public`, so Cloudflare's cache serves a busy day. When the
deploy is down (the NAS rebooting, the tunnel gone), a Cloudflare Worker,
`deploy/cloudflare/outage-worker.js`, steps in as gotflashes's does: on `forenoon.dev` it
serves the landing page itself from a copy of `web/site` bundled with the Worker, with a line
under the masthead saying the demo behind its links is away; on a persona host it shows a
short notice. It passes every other answer through, the pages' own errors included. The copy is
taken at deploy time, so a merge to `main` that changes `web/site`, `web/icons` or
`deploy/cloudflare` deploys the Worker again (`.github/workflows/outage-worker.yml`, with the
Cloudflare API token and account id as repository secrets; the token needs Workers Scripts: Edit
on the account, and Workers Routes: Edit and Zone: Read on the forenoon.dev zone); `npx wrangler deploy` from that folder does the same by
hand. Its routes cover `forenoon.dev` and every subdomain.
