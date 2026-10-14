# Deployment (docker compose)

Target: a NAS or any small x86-64 Linux box. These steps were written for Phase 0 and have
not yet been run on a NAS; report anything that does not match.

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
[setup-email.md](setup-email.md)) and, for ranking, `FORENOON_ANTHROPIC_API_KEY`. Never
commit `.env`.
Changing `FORENOON_FEEDBACK_SECRET` invalidates every feedback link already delivered.

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

## 4. Public feedback endpoint (Cloudflare Tunnel)

The Kindle browser must reach the feedback service from the internet. Only that service is
exposed, and no inbound port is opened.

1. Cloudflare Zero Trust → Networks → Tunnels → create a tunnel (type _Cloudflared_) and copy
   its token into `.env` as `TUNNEL_TOKEN`. Use a tunnel of its own rather than adding a
   hostname to one that serves something else.
2. Add a public hostname (for example `feedback.your-domain.example`) with service
   `http://localhost:8080`.
3. Set `FORENOON_PUBLIC_URL=https://feedback.your-domain.example` in `.env`.
4. Keep `COMPOSE_PROFILES=cloudflared` in `.env` and run `docker compose up -d`.
5. Check: `curl https://feedback.your-domain.example/healthz` prints `ok`. A 530 / error 1033
   means the tunnel has no connector: check `docker logs` for the cloudflared container.

**Minimum TLS version.** Set the zone's minimum TLS version to 1.2 or lower (SSL/TLS → Edge
Certificates → Minimum TLS Version). With a minimum of 1.3 the Kindle browser cannot connect
and footer links fail with "website inaccessible", while phones and computers work. Seen on
a Kindle Oasis in October 2026.

**Network layout.** The app containers use Docker's default bridge, and `cloudflared` joins
the `feedback` container's network namespace, reaching it as `localhost:8080`. No host port
is published. There is deliberately no compose-created network: Synology's DSM firewall
silently drops egress from custom bridge networks, which would break feed fetching and the
tunnel. On DSM, run docker with `sudo`; the binary is at `/usr/local/bin/docker`.

After a host reboot `cloudflared` can start before `feedback` and fail to join its
namespace. If the health check above fails after a reboot, run `docker compose up -d` again.

Tailscale Funnel or your own reverse proxy is an alternative: publish the `feedback` port on
loopback (see the comment in `docker-compose.yml`) and use that URL as `FORENOON_PUBLIC_URL`.

The endpoint rate limits by the `CF-Connecting-IP` header, which is only trustworthy behind
Cloudflare. Do not publish port 8080 beyond loopback.

## Alternative: deploy script and continuous deploy

`deploy/deploy.sh` starts the same three containers with plain `docker run`, for hosts where
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
only that changed it replaces just the tunnel container, and feedback links are unreachable
for a few seconds. The poller also restarts the stack after a reboot if a container failed to come up. A deploy
that lands while the daily run is in progress interrupts it; articles already emailed are
recorded and not resent, and the rest go out with the next run.

## Web pages and readers

The web pages are where everything is set up: readers (who gets a book, at which Kindle
address, how many stories a day and what they are interested in), sources, and the install's
settings. They also show a newsstand of what was
delivered, why each article was picked and how you rated it, where ratings can be changed.
Everything is behind a login, and each account sees only its own reader's articles. They run
from their own image. Without at least one reader the daily run delivers nothing.

Keep the pages on your LAN or a VPN such as Tailscale for now, and do not point the
Cloudflare Tunnel at them: they have a login, but have not been hardened for the public
internet.

1. In `.env`, set `WEB_IMAGE=ghcr.io/<owner>/<repo>-web:latest` and `WEB_LISTEN` to a port
   (`8081`) or, to limit it to one of the host's addresses, `address:port`
   (`192.168.1.20:8081`). For docker compose, also add `web` to `COMPOSE_PROFILES`. The GHCR
   package must be public, like the others.
2. Start it: `sudo ONLY=web sh deploy.sh .env` (a full deploy starts it too), or
   `docker compose up -d web`.
3. So the pages can email you (to confirm your address and to reset a forgotten password),
   also set `FORENOON_WEB_URL` in `.env` to the address you open the pages at. They go
   through the mail server already set there. Without one these emails are written to the container's log (`docker logs forenoon-web`), which
   is enough on a one-person install.
4. Create your reader. You sign in with the email address; the id names you in the
   pipeline's records. You are asked for a password of at least 10 characters:
   `sudo docker exec -it forenoon-web forenoon-web add-user <id> --email <you>@example.com --kindle <you>@kindle.com`
   The first account is an admin. Dates are shown in the container's `TZ` unless you pass
   `--timezone`.
5. Open `http://<host>:8081/` and sign in. A banner asks you to confirm your address: press
   **Send it again** and open the link in the email. Everything works before you do, but a
   forgotten password can only be reset through a confirmed address.
6. On **Account**, write your interests and set the stories a day. Changes apply from the
   next run.
7. On **Sources** (admins only), add the feeds to read; without any, nothing is delivered.
   Hacker News and Lobsters are built in and can be switched off there. Calibre recipes are
   added there too once the recipes container runs ([setup-recipes.md](setup-recipes.md)).
8. On **Settings** (admins only), change the delivery time and anything else from its
   default. Nothing needs changing for a first run.

Other account commands, run the same way: `set-password <id>` (also signs the account out
everywhere) and `users`, which lists each account's email. `set-password` is the way back in
if the email route fails.

The mail password is the only secret the container is given by the deploy. Recipe logins
typed into the Sources page are written to the `forenoon-logins` Docker volume. Check that your router does not
forward the port from the internet.

## Backups

Everything is in the data folder: `forenoon.sqlite3` (plus its `-wal` file), which holds
readers, sources, settings and ratings, and what was fetched and sent. Back up the folder;
keep `.env` somewhere safe too. Recipe logins are in the `forenoon-logins` Docker volume and
are not part of that backup; they can be typed in again. The `forenoon-browser` volume only
holds the recipes container's browser sign-ins, which it makes again by itself.
