# Recipes: publications that need Calibre

Most sources are plain feeds. A few are better read through a
[Calibre news recipe](https://manual.calibre-ebook.com/news.html): publications behind a
subscriber login, such as The Economist, or sites whose pages do not extract well. For
those, an optional fourth container runs Calibre once a day and Forenoon reads the result.

Use a recipe only for content you are entitled to read. You are responsible for checking
that the publication's terms allow it.

## How it fits together

1. The `forenoon-recipes` container runs each recipe at `RECIPES_TIME` (default 05:00) and
   writes one issue per day to `<data>/recipes/<folder>/<date>.epub`.
2. At delivery time Forenoon opens the newest issue of each recipe source and splits it into
   its articles. They are candidates like any others: ranked, capped, given rating links and
   never sent twice. No page is fetched for them, so an article the recipe reached with your
   login is read from the issue alone.
3. Which recipes run, and their logins, are set on the web pages' **Sources** page. A login
   is kept in a Docker volume (`forenoon-logins`) that only the web pages and the recipes
   container mount. The pipeline, the feedback service and the tunnel never see it.

Set `RECIPES_TIME` early enough for the recipes to finish before the delivery time. A recipe
may take several minutes.

## Set it up

1. Turn the container on:
   - **`deploy.sh`:** set `RECIPES_IMAGE=ghcr.io/<owner>/<repo>-recipes:latest` in `.env`
     and run `deploy.sh` again. `poll-deploy.sh` then keeps it up to date.
   - **docker compose:** add `recipes` to `COMPOSE_PROFILES`.

   When it starts it publishes the list of publications its Calibre can fetch (about 1,100).

2. On the **Sources** page, under **Calibre recipes**, start typing the publication's name,
   pick it from the list and press **Add**. Its first issue is fetched straight away, and
   the page shows how that went within a few minutes.

3. If the publication needs a subscriber's login, the row opens a login form. Type the
   username and password and press **Save login**; the recipe is fetched again with it. The
   password is never shown again. Any recipe can be given a login the same way, because
   Calibre's own list of which ones need one is not always right.

**Fetch now** on a row runs that recipe again without waiting for the next day.

### Your own recipe

Put a `.recipe` file in `<data>/recipes/`. It appears in the list under its file name the
next time the recipes container starts or finishes its daily run.

## Subscriber sessions: a few articles instead of a whole paper

For a site you subscribe to that has a feed, there is a lighter way than a recipe. Add its
feed under **Feeds** as usual, then open "Add your session" on its row and paste the cookie
your browser sends to the site when you are signed in: sign in to the site in your browser,
open the developer tools, go to the Network tab and reload, pick any request to the site and
copy the value of its `Cookie` request header into **Cookie**. Check **Site** (such as
`ft.com` or `nytimes.com`) and press **Save session**. It works until the site ends that
session.

A username and password are not offered: the FT and the NYT both ask for a CAPTCHA at sign-in,
and nothing here answers one.

The feed supplies headlines; the ranking picks; only the articles it wants read are fetched
from the site, one at a time and at least 20 seconds apart. They are read by an ordinary
Chrome running on a virtual screen in the recipes container, because some sites (the FT
among them) let only a browser through to their articles. Nothing answers a CAPTCHA: if the
site asks for one, the row and an alert email say so.

When a session stops working, its articles are held back rather than sent as the feed's
summary, and the daily run's alert email names the feed. Paste a fresh cookie and they
come in the next run.

The cookie is kept in the same volume as recipe logins and used on that one site
only; what the browser keeps of the session is in a second volume, `forenoon-browser`, which
only the recipes container mounts. Automated access may be against a site's terms even for
subscribers: that is yours to check.

## When something goes wrong

- The row on the Sources page shows the last lines of a failed fetch.
  `sudo docker logs forenoon-recipes` shows every run.
- A source with no issue from the last few days (the age limit on the Settings page) counts
  as a failed source, so the usual alert arrives after it has failed several runs in a row.
- Recipes break when a site changes. The fix is nearly always a newer Calibre: the image is
  rebuilt monthly, and `poll-deploy.sh` replaces only the recipes container when it changes.
- A recipe's login is passed to `ebook-convert` on its command line inside the recipes
  container. Nothing else runs in that container, and the login is never logged.
- `<data>/recipes/recipes.conf` is written by the web pages from the Sources page; a change
  made to it by hand is overwritten.
- Issues are kept for 7 days (`RECIPES_KEEP_DAYS`).
