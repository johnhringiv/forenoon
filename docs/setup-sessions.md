# Subscriber sessions: sites you pay for

For a site you subscribe to, Forenoon reads its feeds as usual and fetches only the articles
the ranking wants, signed in as you. It does so with the cookie your own browser uses for
the site, and an optional fourth container that runs Chrome.

Use a session only for what your subscription lets you read. Automated access may be
against a site's terms even for subscribers: that is yours to check.

## How it fits together

1. The feed supplies headlines, and the ranking picks. Only the articles it wants read are
   fetched from the site, one at a time and at least 20 seconds apart.
2. The pipeline never holds the cookie. It leaves a request in `<data>/sessions/.fetch/`.
   The `forenoon-browser` container reads the page with Chrome, signed in as you, and leaves
   the answer there.
3. Chrome runs on a virtual screen (Xvfb), because some sites (the FT among them) let only a
   browser through to their articles. Nothing answers a CAPTCHA: if a site asks for one, the
   session's row and an alert email say so.

## Set it up

1. Turn the container on:
   - **`deploy.sh`:** set `BROWSER_IMAGE=ghcr.io/<owner>/<repo>-browser:latest` in `.env`
     and run `deploy.sh` again. `poll-deploy.sh` then keeps it up to date.
   - **docker compose:** add `browser` to `COMPOSE_PROFILES`.
2. On the **Sources** page, add the site's feeds under **Feeds** as usual. Add as many as
   you like: several sections of one paper share one session.
3. Under **Subscriber sessions**, give the site (such as `ft.com` or `nytimes.com`), paste
   the cookie your browser sends to it when you are signed in, and press **Save session**.
   Every page on that site and its subdomains is then fetched with it: from any of its
   feeds, and links added by hand. Each feed on the site says so on its row. Saving the
   site again replaces the cookie.

The session is the household's. Another reader who follows a feed on the site, or adds one,
gets its pages fetched with the same cookie without pasting anything; they see the session
on their Sources page but cannot remove it. A reader can save a session only for a site they
have a feed on; an admin, for any site.

Two ways to get the cookie:

- **A cookie-export extension** (quickest), such as Cookie-Editor: on the signed-in site,
  export its cookies as a header string and paste that.
- **The developer tools:** go to the Network tab and reload, pick any request to the site
  and copy the value of its `Cookie` request header.

A username and password are not offered: the FT and the NYT both ask for a CAPTCHA at
sign-in, and nothing here answers one.

## How long a session lasts

It works until the site ends that session. Every page fetched with it keeps what the site
sends back, so a session that a site renews as it is used lasts as long as it is used. For
the FT and the NYT, the session's row shows the day the site says the sign-in ends (the
NYT's is a year), and the alert email warns from 14 days before.

When a session stops working, the articles the ranking picks from the site are shown on the
newsstand as grayed links rather than sent as the feed's summary, and the daily run's alert
email names the site. Paste a fresh cookie and each run fetches them again: those it gets
within the age limit (the Settings page) are sent with that day's issue, after its own
stories.

A feed you want only as headlines (a paywalled site with no session, say) can have **Take
the text from** set to **Nowhere: show picks as links**. Its pages are never fetched, and its
picks are links on the newsstand.

## Where things are kept

- **The cookie:** the `forenoon-logins` Docker volume, as `site-<domain>.json`. Only the web
  pages and the browser container mount it; it is never shown again and is sent to that
  site only.
- **What the browser keeps of each session:** a second volume, `forenoon-browser`, which only
  the browser container mounts.
- **How each session's last page went:** `<data>/sessions/status.json`, shown on the Sources
  page.

## When something goes wrong

- The session's row on the Sources page shows how its last page went.
  `sudo docker logs forenoon-browser` shows every page fetched.
- A run that waits for a page and gets no answer says the browser container is not running,
  and stops asking for ten minutes. Picks from the site are shown as links meanwhile.
