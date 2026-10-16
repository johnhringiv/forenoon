#!/bin/sh
# Plain-docker deploy for Forenoon + cloudflared (no compose needed).
# Same containers and settings as docker-compose.yml, for hosts where a script
# run from a scheduler or over SSH is more convenient (e.g. Synology DSM).
#
# Usage:
#   sudo sh deploy.sh [env-file]                  # pull IMAGE and replace the containers
#   sudo sh deploy.sh [env-file] <image-tarball>  # load a saved image instead (offline)
#
#   sudo ONLY=tunnel sh deploy.sh [env-file]      # replace just the cloudflared container
#   sudo ONLY=browser sh deploy.sh [env-file]     # replace just the browser container
#   sudo ONLY=web sh deploy.sh [env-file]         # replace the web container (and the tunnel in it)
#
# IMAGE comes from the environment, then the env file, then forenoon:local. WEB_IMAGE likewise,
# with no default.
# CLOUDFLARED_IMAGE likewise, defaulting to cloudflare/cloudflared:latest.
# Roll back with:  sudo IMAGE=ghcr.io/<owner>/<repo>:sha-abc123 sh deploy.sh
#
# Subscriber sessions (optional): with BROWSER_IMAGE set, a fourth container runs Chrome to
# read article pages signed in as the reader, for the sites given a session on the web pages.
# It exchanges requests and answers with the pipeline in <data>/sessions; the cookies are
# kept in the Docker volume forenoon-logins, which only it and the web pages mount.
#
# Web pages: the forenoon-web container serves the pages, behind a login, and answers the
# rating links in each book, which need none. Create the first account once it is up:
#   sudo docker exec -it forenoon-web forenoon-web add-user <id> --email <address>
# With WEB_LISTEN (a port, or address:port) it is also published on the host, for a reverse
# proxy of your own or the LAN. With FORENOON_WEB_URL set it emails links to confirm an address
# and reset a password.
#
# The env file is .env.example filled in: the data folder, the mail server, the public
# address and the keys. Everything else is set on the web pages. FORENOON_HOST_DATA is the
# data folder; if forenoon.sqlite3 sits next to this script and the data folder does not
# have one yet, it is copied in. An existing file is never overwritten.
#
# Network layout: the containers run on Docker's default bridge; cloudflared joins the web
# container's network namespace and reaches it as localhost:8081, so the tunnel's public
# hostname must target http://localhost:8081. With a tunnel, the web pages default to secure
# cookies and take the client's address from CF-Connecting-IP (FORENOON_WEB_SECURE_COOKIES,
# FORENOON_WEB_CLIENT_IP_HEADER and FORENOON_WEB_TRUSTED_PROXIES override that).
# Custom bridge networks are avoided deliberately: Synology's DSM firewall
# silently drops their egress.
#
# Idempotent: re-running replaces the containers; the data folder persists.
set -e

ENV_FILE="${1:-.env}"
IMAGE_TAR="${2:-}"
DIR=$(dirname "$0")

[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (copy .env.example and fill it in)"; exit 1; }

env_get() {
    grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '\r'
}

IMAGE="${IMAGE:-$(env_get IMAGE)}"
IMAGE="${IMAGE:-forenoon:local}"
CLOUDFLARED_IMAGE="${CLOUDFLARED_IMAGE:-$(env_get CLOUDFLARED_IMAGE)}"
CLOUDFLARED_IMAGE="${CLOUDFLARED_IMAGE:-cloudflare/cloudflared:latest}"
BROWSER_IMAGE="${BROWSER_IMAGE:-$(env_get BROWSER_IMAGE)}"
WEB_IMAGE="${WEB_IMAGE:-$(env_get WEB_IMAGE)}"
[ -n "$WEB_IMAGE" ] || { echo "WEB_IMAGE is not set in $ENV_FILE"; exit 1; }
WEB_LISTEN="${WEB_LISTEN:-$(env_get WEB_LISTEN)}"
# Where the web pages are opened, for the links in their account emails.
FORENOON_WEB_URL=$(env_get FORENOON_WEB_URL)
# Where rating links point, and the mail server books, alerts and account emails go through.
FORENOON_PUBLIC_URL=$(env_get FORENOON_PUBLIC_URL)
FORENOON_SMTP_MAX_MESSAGE_BYTES=$(env_get FORENOON_SMTP_MAX_MESSAGE_BYTES)
FORENOON_SMTP_HOST=$(env_get FORENOON_SMTP_HOST)
FORENOON_SMTP_PORT=$(env_get FORENOON_SMTP_PORT)
FORENOON_SMTP_USERNAME=$(env_get FORENOON_SMTP_USERNAME)
FORENOON_SMTP_SENDER=$(env_get FORENOON_SMTP_SENDER)
export FORENOON_WEB_URL FORENOON_SMTP_HOST FORENOON_SMTP_USERNAME FORENOON_SMTP_SENDER
export FORENOON_PUBLIC_URL FORENOON_SMTP_MAX_MESSAGE_BYTES
export FORENOON_SMTP_PORT="${FORENOON_SMTP_PORT:-587}"
DATA_DIR=$(env_get FORENOON_HOST_DATA)
[ -n "$DATA_DIR" ] || { echo "FORENOON_HOST_DATA is not set in $ENV_FILE"; exit 1; }
TZ=$(env_get TZ)
export TZ="${TZ:-UTC}"
FORENOON_FEEDBACK_SECRET=$(env_get FORENOON_FEEDBACK_SECRET)
[ -n "$FORENOON_FEEDBACK_SECRET" ] || { echo "FORENOON_FEEDBACK_SECRET is not set in $ENV_FILE"; exit 1; }
# Checked before anything is replaced: the pipeline refuses a short one, and the pages would
# answer no rating link, so a bad value must not take a working install down.
[ "${#FORENOON_FEEDBACK_SECRET}" -ge 32 ] ||
    { echo "FORENOON_FEEDBACK_SECRET must be at least 32 characters (openssl rand -hex 32)"; exit 1; }
FORENOON_SMTP_PASSWORD=$(env_get FORENOON_SMTP_PASSWORD)
FORENOON_ANTHROPIC_API_KEY=$(env_get FORENOON_ANTHROPIC_API_KEY)
TUNNEL_TOKEN=$(env_get TUNNEL_TOKEN)
# Behind the tunnel the pages are https and every request arrives from cloudflared.
FORENOON_WEB_SECURE_COOKIES=$(env_get FORENOON_WEB_SECURE_COOKIES)
FORENOON_WEB_CLIENT_IP_HEADER=$(env_get FORENOON_WEB_CLIENT_IP_HEADER)
FORENOON_WEB_TRUSTED_PROXIES=$(env_get FORENOON_WEB_TRUSTED_PROXIES)
if [ -n "$TUNNEL_TOKEN" ]; then
    FORENOON_WEB_SECURE_COOKIES="${FORENOON_WEB_SECURE_COOKIES:-true}"
    FORENOON_WEB_CLIENT_IP_HEADER="${FORENOON_WEB_CLIENT_IP_HEADER:-CF-Connecting-IP}"
fi
export FORENOON_WEB_SECURE_COOKIES="${FORENOON_WEB_SECURE_COOKIES:-false}"
export FORENOON_WEB_CLIENT_IP_HEADER FORENOON_WEB_TRUSTED_PROXIES="${FORENOON_WEB_TRUSTED_PROXIES:-127.0.0.1,::1}"
# Exported so `docker run -e NAME` can pass them without the values appearing in `ps`.
export FORENOON_FEEDBACK_SECRET FORENOON_SMTP_PASSWORD FORENOON_ANTHROPIC_API_KEY TUNNEL_TOKEN

start_tunnel() {
    docker stop -t 30 forenoon-cloudflared >/dev/null 2>&1 || true
    docker rm -f forenoon-cloudflared >/dev/null 2>&1 || true
    if [ -z "$TUNNEL_TOKEN" ]; then
        echo "TUNNEL_TOKEN is empty: not starting cloudflared"
        return 0
    fi
    docker pull -q "$CLOUDFLARED_IMAGE" >/dev/null ||
        echo "pull failed; will use local image $CLOUDFLARED_IMAGE if present"
    docker run -d --name forenoon-cloudflared --restart unless-stopped \
        --network container:forenoon-web \
        -e TUNNEL_TOKEN \
        "$CLOUDFLARED_IMAGE" tunnel --no-autoupdate run >/dev/null
}

start_browser() {
    # forenoon-recipes was this container's name while it also ran Calibre.
    for c in forenoon-browser forenoon-recipes; do
        docker stop -t 30 "$c" >/dev/null 2>&1 || true
        docker rm -f "$c" >/dev/null 2>&1 || true
    done
    [ -n "$BROWSER_IMAGE" ] || return 0
    docker pull -q "$BROWSER_IMAGE" >/dev/null ||
        echo "pull failed; will use local image $BROWSER_IMAGE if present"
    if [ ! -d "$DATA_DIR/sessions" ]; then
        mkdir -p "$DATA_DIR/sessions"
        chown 1000:1000 "$DATA_DIR/sessions"
    fi
    # Only the sessions folder and the logins volume are mounted: this container never sees
    # the database. The web pages write the cookies. The forenoon-browser volume holds what
    # its browser keeps of a sign-in, and nothing else mounts it.
    docker run -d --name forenoon-browser --restart unless-stopped \
        --stop-timeout 30 \
        --read-only --tmpfs /tmp \
        --cap-drop ALL --security-opt no-new-privileges:true \
        -v "$DATA_DIR/sessions:/sessions" \
        -v forenoon-logins:/logins:ro \
        -v forenoon-browser:/browser \
        -e TZ \
        "$BROWSER_IMAGE" >/dev/null ||
        echo "browser container did not start; the rest of the deploy is unaffected"
}

start_web() {
    # cloudflared lives in this container's network namespace, so it goes too.
    for c in forenoon-cloudflared forenoon-web; do
        docker stop -t 30 "$c" >/dev/null 2>&1 || true
        docker rm -f "$c" >/dev/null 2>&1 || true
    done
    docker pull -q "$WEB_IMAGE" >/dev/null ||
        echo "pull failed; will use local image $WEB_IMAGE if present"
    # It shares the data folder for the database: it reads what was delivered, and writes
    # accounts, sessions, sources, settings and ratings. Its secrets from here are the mail
    # password, for the account emails (without FORENOON_SMTP_HOST those are written to its
    # log instead), and the feedback secret, to check rating links. Session cookies pasted
    # into it go to the logins volume.
    set --
    [ -z "$WEB_LISTEN" ] || set -- -p "$WEB_LISTEN:8081"
    docker run -d --name forenoon-web --restart unless-stopped \
        --stop-timeout 30 \
        --read-only --tmpfs /tmp \
        --cap-drop ALL --security-opt no-new-privileges:true \
        -v "$DATA_DIR:/data" \
        -v forenoon-logins:/logins \
        "$@" \
        -e TZ -e FORENOON_WEB_URL -e FORENOON_FEEDBACK_SECRET \
        -e FORENOON_WEB_SECURE_COOKIES -e FORENOON_WEB_CLIENT_IP_HEADER -e FORENOON_WEB_TRUSTED_PROXIES \
        -e FORENOON_SMTP_HOST -e FORENOON_SMTP_PORT -e FORENOON_SMTP_USERNAME -e FORENOON_SMTP_SENDER \
        -e FORENOON_SMTP_PASSWORD \
        "$WEB_IMAGE" serve >/dev/null
}

# Healthy means migrated too: the pages serve only after applying the database migrations.
wait_web() {
    i=0
    until docker exec forenoon-web forenoon-web health >/dev/null 2>&1; do
        i=$((i + 1))
        if [ $i -ge 60 ]; then
            echo "web pages never became healthy; the database may not be migrated"
            docker logs forenoon-web 2>&1 | tail -20
            return 1
        fi
        sleep 1
    done
}

if [ "${ONLY:-}" = "web" ]; then
    echo "$(date -u +%FT%TZ) recreating forenoon-web from $WEB_IMAGE"
    start_web
    # The tunnel went with the old web container and comes back whether or not the new one is
    # healthy: the public hostname must not stay dark over a bad web image.
    web_healthy=true
    wait_web || web_healthy=false
    start_tunnel
    docker ps --filter name=forenoon-web --filter name=forenoon-cloudflared --format '{{.Names}}: {{.Status}}'
    # Fail loudly when the pages did not come up, so a scheduler's failure notice fires.
    [ "$web_healthy" = true ] || exit 1
    exit 0
fi

if [ "${ONLY:-}" = "browser" ]; then
    echo "$(date -u +%FT%TZ) recreating forenoon-browser from ${BROWSER_IMAGE:-(none set)}"
    start_browser
    sleep 3
    docker ps --filter name=forenoon-browser --format '{{.Names}}: {{.Status}}'
    exit 0
fi

if [ "${ONLY:-}" = "tunnel" ]; then
    echo "$(date -u +%FT%TZ) recreating forenoon-cloudflared from $CLOUDFLARED_IMAGE"
    start_tunnel
    sleep 5
    docker ps --filter name=forenoon-cloudflared --format '{{.Names}}: {{.Status}}'
    exit 0
fi

if [ -n "$IMAGE_TAR" ]; then
    docker load -i "$IMAGE_TAR"
else
    docker pull "$IMAGE" || echo "pull failed; will use local image $IMAGE if present"
fi

# The containers run as UID/GID 1000. Only a newly created folder is chowned;
# an existing one already carries live data with its own ownership.
if [ ! -d "$DATA_DIR" ]; then
    mkdir -p "$DATA_DIR"
    chown 1000:1000 "$DATA_DIR"
fi
if [ -f "$DIR/forenoon.sqlite3" ] && [ ! -e "$DATA_DIR/forenoon.sqlite3" ]; then
    cp "$DIR/forenoon.sqlite3" "$DATA_DIR/forenoon.sqlite3"
    chown 1000:1000 "$DATA_DIR/forenoon.sqlite3"
    echo "seeded $DATA_DIR/forenoon.sqlite3"
fi
[ -n "$FORENOON_SMTP_HOST" ] || echo "FORENOON_SMTP_HOST is not set in $ENV_FILE: nothing can be delivered"

# The web pages first: they apply the database migrations, and the pipeline reads the new
# columns as soon as it starts.
echo "$(date -u +%FT%TZ) recreating forenoon-web from $WEB_IMAGE"
start_web
web_healthy=true
wait_web || web_healthy=false

echo "$(date -u +%FT%TZ) recreating forenoon from $IMAGE"
docker stop -t 30 forenoon >/dev/null 2>&1 || true
docker rm -f forenoon >/dev/null 2>&1 || true

run_app() {
    name="$1"
    shift
    docker run -d --name "$name" --restart unless-stopped \
        `# no --cpus: DSM kernels lack the CFS scheduler` \
        --stop-timeout 30 \
        --read-only --tmpfs /tmp \
        --cap-drop ALL --security-opt no-new-privileges:true \
        -v "$DATA_DIR:/data" \
        -e TZ -e FORENOON_FEEDBACK_SECRET "$@" >/dev/null
}

# The pipeline signs the rating links with the feedback secret; the web pages check them.
run_app forenoon -e FORENOON_SMTP_PASSWORD -e FORENOON_ANTHROPIC_API_KEY -e FORENOON_PUBLIC_URL \
    -e FORENOON_SMTP_HOST -e FORENOON_SMTP_PORT -e FORENOON_SMTP_USERNAME -e FORENOON_SMTP_SENDER \
    -e FORENOON_SMTP_MAX_MESSAGE_BYTES \
    "$IMAGE" schedule

start_tunnel
start_browser

sleep 5
docker ps --filter name=forenoon --format '{{.Names}}: {{.Status}}'
docker logs forenoon 2>&1 | tail -3
[ "$web_healthy" = true ] || exit 1
echo "verify: curl ${FORENOON_PUBLIC_URL:-https://your-public-hostname}/healthz"
