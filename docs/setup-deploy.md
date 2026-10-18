# Deployment (docker compose)

Target: a NAS or any small x86-64 Linux box. These steps use docker compose;
`deploy/deploy.sh` starts the same containers with plain `docker run` (named `forenoon`,
`forenoon-web`, `forenoon-browser`, `forenoon-cloudflared`) for hosts where compose is not an
option. Report anything that does not match.

## 1. Folder and user

Pick a host folder for Forenoon's state (for example
`/volume1/docker/forenoon`). The containers run as UID/GID 1000, so give them the
folder:

```sh
sudo mkdir -p /volume1/docker/forenoon
sudo chown -R 1000:1000 /volume1/docker/forenoon
```

To run as a different host user instead, set `PUID` and `PGID` in `.env`.

There is no configuration file to write. What belongs to the deploy goes in `.env` (next
step); readers, sources and every other setting are set on the web pages afterwards.

## 2. Environment

```sh
cd deploy
cp .env.example .env
openssl rand -hex 32    # paste as FORENOON_FEEDBACK_SECRET
```

Fill in `FORENOON_HOST_DATA`, `TZ`, the mail server (`FORENOON_SMTP_HOST`,
`FORENOON_SMTP_USERNAME`, `FORENOON_SMTP_SENDER`, `FORENOON_SMTP_PASSWORD`; see
[setup-email.md](setup-email.md)) and `FORENOON_ANTHROPIC_API_KEY`, which Forenoon needs:
the model is what chooses your articles. Without it a run still delivers, newest first, and
the alert says the key is missing. Never commit `.env`.
Changing `FORENOON_FEEDBACK_SECRET` invalidates every rating link already delivered.

## 3. First run

```sh
docker compose build
docker compose up -d
```

Then set up the web pages (below): create your reader and add sources. After that you can
try a run without sending anything:

```sh
docker compose run --rm forenoon report            # triage your sources
docker compose run --rm forenoon run --dry-run --out /data/out
```

In Synology Container Manager, create a Project from the `deploy/` folder instead; the
commands above still work over SSH.

`forenoon` then delivers every day at the time on the Settings page (06:00 unless changed).
Each run writes a report to
`<folder>/reports/`.

## 4. Public address (Cloudflare Tunnel or your own proxy)

The web pages are served from the internet over https: the Kindle's browser opens the rating
links in each book there, and you sign in to the pages from anywhere. Everything except the
rating links and `/healthz` needs a sign-in.

### Cloudflare Tunnel

No inbound port is opened.

1. Cloudflare Zero Trust → Networks → Tunnels → create a tunnel (type _Cloudflared_) and copy
   its token into `.env` as `TUNNEL_TOKEN`.
2. Add a public hostname (for example `forenoon.your-domain.example`) with service
   `http://localhost:8081`.
3. In `.env`, set `FORENOON_PUBLIC_URL` and `FORENOON_WEB_URL` to
   `https://forenoon.your-domain.example`. For docker compose, also set
   `FORENOON_WEB_SECURE_COOKIES=true`; `deploy.sh` sets it itself when `TUNNEL_TOKEN` is set.
   Both already take the client's address from the tunnel's `CF-Connecting-IP` header.
4. Keep `COMPOSE_PROFILES=cloudflared` in `.env` and run `docker compose up -d`.
5. Check: `curl https://forenoon.your-domain.example/healthz` prints `ok`. A 530 / error 1033
   means the tunnel has no connector: check `docker logs` for the cloudflared container.

**Minimum TLS version.** Set the zone's minimum TLS version to 1.2 or lower (SSL/TLS → Edge
Certificates → Minimum TLS Version). With a minimum of 1.3 the Kindle browser cannot connect
and footer links fail with "website inaccessible", while phones and computers work. Seen on
a Kindle Oasis in October 2026. For the same reason, do not put a bot challenge or a
Cloudflare Access login in front of `/f/*`: the Kindle's browser cannot pass them. On the
rest of the hostname, Access is an optional extra layer.

**Network layout.** The containers use Docker's default bridge, and `cloudflared` joins the
`web` container's network namespace, reaching it as `localhost:8081`. Requests from it arrive
from 127.0.0.1, which is why the web pages believe its `CF-Connecting-IP` header from there
and nowhere else. There is deliberately no compose-created network: Synology's DSM firewall
silently drops egress from custom bridge networks, which would break feed fetching and the
tunnel. On DSM, run docker with `sudo`; the binary is at `/usr/local/bin/docker`.

After a host reboot `cloudflared` can start before `web` and fail to join its namespace. If
the health check above fails after a reboot, run `docker compose up -d` again, or let
`poll-compose.sh` (below) do it.

### Your own reverse proxy

Publish the pages on a host port with `WEB_LISTEN` and point the proxy at it, terminating
https there. Then set, in `.env`:

- `FORENOON_PUBLIC_URL` and `FORENOON_WEB_URL` to the proxy's https address;
- `FORENOON_WEB_SECURE_COOKIES=true`;
- `FORENOON_WEB_CLIENT_IP_HEADER` to the header your proxy puts the client's address in
  (`X-Forwarded-For` or `X-Real-IP`), and `FORENOON_WEB_TRUSTED_PROXIES` to the address the
  proxy connects from (as Docker sees it), so that no one else can set it. Without these the
  sign-in limits count every visitor as the proxy.

With secure cookies on, a sign-in holds only at the https address: a browser drops a secure
cookie anywhere else. So when `FORENOON_WEB_URL` is an https address, a request for any other
host (`http://<host>:8081`) is redirected to the same page there; `/healthz` is answered
wherever it is asked. A request from one of `FORENOON_WEB_TRUSTED_PROXIES` is never
redirected, so a proxy that names its upstream as the host (nginx does unless given
`proxy_set_header Host $host`; Apache unless `ProxyPreserveHost On`) must be listed there. Prefer
passing the host on: a proxy on the same machine reaches the container from Docker's bridge
gateway (often `172.17.0.1`), which is also where LAN visits to the published port can come
from, so trusting it would let those visits through without the redirect.

### Continuous deploy with docker compose

CI publishes every merge to `main` to GHCR. To follow it, set `IMAGE`, `WEB_IMAGE` (and
`BROWSER_IMAGE` if you use the browser) in `.env` to their `ghcr.io/<owner>/<repo>…:latest`
tags, then schedule `deploy/poll-compose.sh` every 5 minutes as root, from the folder holding
`docker-compose.yml` and `.env` (DSM: Control Panel → Task Scheduler → user-defined script;
enable the email on abnormal termination):

```sh
cd /path/to/compose-folder && sh poll-compose.sh >> poll-compose.log 2>&1
```

It runs `docker compose pull` and `docker compose up -d`, which replaces only the containers
whose image changed and starts any that stopped, then checks `FORENOON_PUBLIC_URL/healthz`.
An unchanged install costs a manifest check per image. Roll back by pinning `:sha-<commit>`
tags in `.env`. The GHCR packages must be public: the host pulls anonymously.

## Alternative: deploy script and continuous deploy

`deploy/deploy.sh` starts the same containers with plain `docker run`, for hosts where
a script is handier than a compose project (it was written for Synology DSM). It reads the
same `.env` file:

```sh
sudo sh deploy.sh .env                       # pulls IMAGE
sudo sh deploy.sh .env forenoon.tar.gz  # or loads a saved image
```

Continuous deploy is pull-based, so the host needs no inbound access:

```
push to main ──► CI (lint, tests, docker smoke test) ──► GHCR :latest  (+ :sha-<commit>)
                                                              │
                          host scheduler: poll-deploy.sh ─────┘ pull when the digest changes
                                             │
                                             └─► deploy.sh: replace containers, health check
```

1. Set `IMAGE=ghcr.io/<owner>/<repo>:latest` in `.env`; the poller health-checks
   `FORENOON_PUBLIC_URL`. The host pulls
   anonymously, so the GHCR package must be public.
2. Schedule `poll-deploy.sh` every 5 minutes as root (DSM: Control Panel → Task Scheduler →
   user-defined script; enable the email on abnormal termination):
   `cd /path/to/scripts && sh poll-deploy.sh .env >> poll-deploy.log 2>&1`
3. Roll back by pinning a commit: `sudo IMAGE=ghcr.io/<owner>/<repo>:sha-<commit> sh deploy.sh .env`
   (and set the same `IMAGE` in `.env`, or the poller moves you forward again).

The poller follows the `cloudflared` image too (`CLOUDFLARED_IMAGE`, default `:latest`): when
only that changed it replaces just the tunnel container, and the pages are unreachable for a
few seconds. A new web image replaces the web container and the tunnel inside it. The poller also restarts the stack after a reboot if a container failed to come up. A deploy
that lands while the daily run is in progress interrupts it; articles already emailed are
recorded and not resent, and the rest go out with the next run.

## Web pages and readers

The web pages are where everything is set up: readers (who gets a book, at which Kindle
address, how many stories a day and what they are interested in), sources, and the install's
settings. They also show a newsstand of what was
delivered, why each article was picked and how you rated it, where ratings can be changed.
Everything is behind a login, and each account sees only its own reader's articles. They run
from their own image. Without at least one reader the daily run delivers nothing.

1. In `.env`, set `WEB_IMAGE=ghcr.io/<owner>/<repo>-web:latest` (the GHCR package must be
   public, like the others). `deploy.sh` requires it; docker compose builds it otherwise.
2. A full deploy or `docker compose up -d` starts it; `sudo ONLY=web sh deploy.sh .env`
   replaces just it and the tunnel.
3. So the pages can email you (to confirm your address and to reset a forgotten password),
   `FORENOON_WEB_URL` in `.env` is the address you open the pages at. They go through the
   mail server already set there. Without one these emails are written to the container's
   log (`docker compose logs web`), which is enough on a one-person install.
4. Create your reader. You sign in with the email address; the id names you in the
   pipeline's records. You are asked for a password of at least 10 characters:
   `sudo docker compose exec web forenoon-web add-user <id> --email <you>@example.com --kindle <you>@kindle.com`
   (under `deploy.sh`: `sudo docker exec -it forenoon-web forenoon-web add-user …`)
   The first account is an admin. Dates are shown in the container's `TZ` unless you pass
   `--timezone`. Everyone else is invited from the **Accounts** page once you are signed in:
   they get an emailed link, choose their own password and Kindle address, and start by
   following every source shared with the household. Each Kindle must have the sender
   address from `.env` on its approved list at Amazon; the join page says so and links there.
5. Open the public address and sign in. A banner asks you to confirm your address: press
   **Send it again** and open the link in the email. Everything works before you do, but a
   forgotten password can only be reset through a confirmed address.
6. On **Reading**, write your interests, set the stories a day and how your issue is made.
   Changes apply from the next run.
7. On **Sources**, add the feeds to read; a reader who follows none gets nothing. A feed is
   the adder's alone until shared with the household; an admin sees every one.
   Hacker News and Lobsters are built in and can be switched off there. Sessions for sites
   you subscribe to are saved there too once the browser container runs
   ([setup-sessions.md](setup-sessions.md)).
8. On **Settings** (admins only), change the delivery time and anything else from its
   default. Nothing needs changing for a first run.

Other account commands, run the same way: `set-password <id>` (also signs the account out
everywhere) and `users`, which lists each account's email and marks invitations not yet
accepted. `set-password` is the way back in
if the email route fails.

The deploy gives the container two secrets: the mail password and the feedback secret, which
checks the rating links. Session cookies pasted into the Sources page are written to the
`forenoon-logins` Docker volume.

## A public demo (optional)

An install can show a few readers' pages to anyone, read-only, each at a hostname of its own:
the pages a visitor sees are the product's, so a demo cannot drift from what you deploy.

1. In `.env`, list the personas as `host=reader` pairs:
   `FORENOON_WEB_DEMO=mara.forenoon.example=mara,theo.forenoon.example=theo`
   On start, the web program makes any persona named in `web/demo.json` that is not an
   account yet, with its interests and no Kindle address, and on an install with no feeds it
   adds the file's shared feeds, followed by every persona. A persona not in the file is an
   ordinary reader you create yourself (`add-user`, no `--kindle`) and write interests for.
2. Nothing already there is touched on later starts, so change interests and feeds on the
   pages as usual, signed in on the install's own hostname.
3. Route each hostname to the web pages as well: on a tunnel, another public hostname to
   `http://localhost:8081`; on your own proxy, another server name to the same upstream.
   Cloudflare's proxy may cache these pages: they carry `Cache-Control: public, max-age=300`
   and set no cookie.

A demo host serves that persona's Newsstand, Archive and Sources, with a banner that names
the demo, links to the other personas and shows the persona's interests. Nothing on it can be
changed, and every other address there, the sign-in page included, is 404. Like every page,
these ask search engines not to index them; a landing page of your own is what gets found.
Your own hostname is unchanged: sign in there to look after the personas. Keep your own
account off the list: a persona is shown as a reader whatever its role, but its Archive shows
everything weighed for it, so a persona should hold nothing you would not publish.

## Backups

Everything is in the data folder: `forenoon.sqlite3` (plus its `-wal` file), which holds
readers, sources, settings and ratings, and what was fetched and sent. Back up the folder;
keep `.env` somewhere safe too. Session cookies are in the `forenoon-logins` Docker volume and
are not part of that backup; they can be pasted in again. The `forenoon-browser` volume only
holds what the browser container keeps of each session, which it makes again by itself.
