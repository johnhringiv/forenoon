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
#   sudo ONLY=web sh deploy.sh [env-file]         # replace just the web pages container
#
# IMAGE comes from the environment, then the env file, then forenoon:local.
# CLOUDFLARED_IMAGE likewise, defaulting to cloudflare/cloudflared:latest.
# Roll back with:  sudo IMAGE=ghcr.io/<owner>/<repo>:sha-abc123 sh deploy.sh
#
# Subscriber sessions (optional): with BROWSER_IMAGE set, a fourth container runs Chrome to
# read article pages signed in as the reader, for the sites given a session on the web pages.
# It exchanges requests and answers with the pipeline in <data>/sessions; the cookies are
# kept in the Docker volume forenoon-logins, which only it and the web pages mount.
#
# Web pages (optional): with WEB_IMAGE and WEB_LISTEN (a port, or address:port) set, a
# container serves the newsstand there (what was delivered; ratings can be changed), behind
# a login. Create the first account once it is up:
#   sudo docker exec -it forenoon-web forenoon-web add-user <id> --email <address>
# Publish it only on a trusted network for now, and never route the tunnel to it. With
# FORENOON_WEB_URL set it emails links to confirm an address and reset a password; the mail
# password is the only secret it is given.
#
# The env file is .env.example filled in: the data folder, the mail server, the public
# address and the keys. Everything else is set on the web pages. FORENOON_HOST_DATA is the
# data folder; if forenoon.sqlite3 sits next to this script and the data folder does not
# have one yet, it is copied in. An existing file is never overwritten.
#
# Network layout: both app containers run on Docker's default bridge;
# cloudflared joins the feedback container's network namespace and reaches it
# as localhost:8080, so the tunnel ingress must target http://localhost:8080.
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
FORENOON_SMTP_PASSWORD=$(env_get FORENOON_SMTP_PASSWORD)
FORENOON_ANTHROPIC_API_KEY=$(env_get FORENOON_ANTHROPIC_API_KEY)
TUNNEL_TOKEN=$(env_get TUNNEL_TOKEN)
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
        --network container:forenoon-feedback \
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
    docker stop -t 30 forenoon-web >/dev/null 2>&1 || true
    docker rm -f forenoon-web >/dev/null 2>&1 || true
    [ -n "$WEB_LISTEN" ] || return 0
    if [ -z "$WEB_IMAGE" ]; then
        echo "WEB_LISTEN is set but WEB_IMAGE is not: not starting the web pages"
        return 0
    fi
    docker pull -q "$WEB_IMAGE" >/dev/null ||
        echo "pull failed; will use local image $WEB_IMAGE if present"
    # It shares the data folder for the database: it reads what was delivered, and writes
    # accounts, sessions, sources, settings and rating changes. Its one secret from here is
    # the mail password, for the account emails; without FORENOON_SMTP_HOST those are
    # written to its log instead. Session cookies pasted into it go to the logins volume.
    docker run -d --name forenoon-web --restart unless-stopped \
        --stop-timeout 30 \
        --read-only --tmpfs /tmp \
        --cap-drop ALL --security-opt no-new-privileges:true \
        -v "$DATA_DIR:/data" \
        -v forenoon-logins:/logins \
        -p "$WEB_LISTEN:8081" \
        -e TZ -e FORENOON_WEB_URL \
        -e FORENOON_SMTP_HOST -e FORENOON_SMTP_PORT -e FORENOON_SMTP_USERNAME -e FORENOON_SMTP_SENDER \
        -e FORENOON_SMTP_PASSWORD \
        "$WEB_IMAGE" serve >/dev/null ||
        echo "web container did not start; the rest of the deploy is unaffected"
}

if [ "${ONLY:-}" = "web" ]; then
    echo "$(date -u +%FT%TZ) recreating forenoon-web from ${WEB_IMAGE:-(WEB_IMAGE not set)} on ${WEB_LISTEN:-(WEB_LISTEN not set)}"
    start_web
    sleep 3
    docker ps --filter name=forenoon-web --format '{{.Names}}: {{.Status}}'
    # Fail loudly when it should be up and is not, so a scheduler's failure notice fires.
    if [ -n "$WEB_LISTEN" ] && [ -n "$WEB_IMAGE" ] &&
        [ "$(docker inspect forenoon-web --format '{{.State.Running}}' 2>/dev/null)" != "true" ]; then
        echo "forenoon-web is not running"
        docker logs forenoon-web 2>&1 | tail -5
        exit 1
    fi
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
# columns as soon as it starts. Healthy means migrated, since they serve only afterwards.
start_web
if [ "$(docker inspect forenoon-web --format '{{.State.Running}}' 2>/dev/null)" = "true" ]; then
    i=0
    until docker exec forenoon-web forenoon-web health >/dev/null 2>&1; do
        i=$((i + 1))
        if [ $i -ge 60 ]; then
            echo "web pages never became healthy; the database may not be migrated"
            docker logs forenoon-web 2>&1 | tail -5
            break
        fi
        sleep 1
    done
fi

echo "$(date -u +%FT%TZ) recreating forenoon containers from $IMAGE"
# cloudflared first: it lives in the feedback container's network namespace.
for c in forenoon-cloudflared forenoon-feedback forenoon; do
    docker stop -t 30 "$c" >/dev/null 2>&1 || true
    docker rm -f "$c" >/dev/null 2>&1 || true
done

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

run_app forenoon -e FORENOON_SMTP_PASSWORD -e FORENOON_ANTHROPIC_API_KEY -e FORENOON_PUBLIC_URL \
    -e FORENOON_SMTP_HOST -e FORENOON_SMTP_PORT -e FORENOON_SMTP_USERNAME -e FORENOON_SMTP_SENDER \
    -e FORENOON_SMTP_MAX_MESSAGE_BYTES \
    "$IMAGE" schedule
run_app forenoon-feedback "$IMAGE" feedback --port 8080

# Health-gate the feedback service from inside its own container (no host port).
i=0
until docker exec forenoon-feedback python -c \
    "import urllib.request as u; u.urlopen('http://localhost:8080/healthz', timeout=3)" \
    >/dev/null 2>&1; do
    i=$((i + 1))
    if [ $i -ge 20 ]; then
        echo "feedback service never became healthy"
        docker logs forenoon-feedback 2>&1 | tail -20
        exit 1
    fi
    sleep 1
done

start_tunnel
start_browser

sleep 5
docker ps --filter name=forenoon --format '{{.Names}}: {{.Status}}'
docker logs forenoon 2>&1 | tail -3
echo "verify: curl ${FORENOON_PUBLIC_URL:-https://your-feedback-hostname}/healthz"
