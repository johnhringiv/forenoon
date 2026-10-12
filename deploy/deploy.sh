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
#   sudo ONLY=recipes sh deploy.sh [env-file]     # replace just the recipes container
#   sudo ONLY=web sh deploy.sh [env-file]         # replace just the web pages container
#
# IMAGE comes from the environment, then the env file, then forenoon:local.
# CLOUDFLARED_IMAGE likewise, defaulting to cloudflare/cloudflared:latest.
# Roll back with:  sudo IMAGE=ghcr.io/<owner>/<repo>:sha-abc123 sh deploy.sh
#
# Recipes (optional): with RECIPES_IMAGE set, a fourth container runs Calibre news recipes
# into <data>/recipes. Its logins come from recipes.env beside the env file (or
# RECIPES_ENV_FILE) and reach no other container.
#
# Web pages (optional): with WEB_IMAGE and WEB_LISTEN (a port, or address:port) set, a
# container serves the newsstand there (what was delivered; ratings can be changed), behind
# a login. Create the first account once it is up:
#   sudo docker exec -it forenoon-web forenoon-web add-user <id>
# Publish it only on a trusted network for now, and never route the tunnel to it. The
# container is given no secrets.
#
# The env file is .env.example filled in. FORENOON_HOST_DATA is the data folder; if
# config.yaml / sources.yaml (and optionally forenoon.sqlite3) sit next
# to this script and the data folder does not have them yet, they are copied
# in. Existing files are never overwritten.
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
RECIPES_IMAGE="${RECIPES_IMAGE:-$(env_get RECIPES_IMAGE)}"
RECIPES_ENV_FILE="${RECIPES_ENV_FILE:-$(env_get RECIPES_ENV_FILE)}"
RECIPES_ENV_FILE="${RECIPES_ENV_FILE:-$(dirname "$ENV_FILE")/recipes.env}"
WEB_IMAGE="${WEB_IMAGE:-$(env_get WEB_IMAGE)}"
WEB_LISTEN="${WEB_LISTEN:-$(env_get WEB_LISTEN)}"
RECIPES_TIME=$(env_get RECIPES_TIME)
export RECIPES_TIME="${RECIPES_TIME:-05:00}"
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

start_recipes() {
    docker stop -t 30 forenoon-recipes >/dev/null 2>&1 || true
    docker rm -f forenoon-recipes >/dev/null 2>&1 || true
    [ -n "$RECIPES_IMAGE" ] || return 0
    docker pull -q "$RECIPES_IMAGE" >/dev/null ||
        echo "pull failed; will use local image $RECIPES_IMAGE if present"
    if [ ! -d "$DATA_DIR/recipes" ]; then
        mkdir -p "$DATA_DIR/recipes"
        chown 1000:1000 "$DATA_DIR/recipes"
    fi
    if [ -f "$DIR/recipes.conf" ] && [ ! -e "$DATA_DIR/recipes/recipes.conf" ]; then
        cp "$DIR/recipes.conf" "$DATA_DIR/recipes/recipes.conf"
        chown 1000:1000 "$DATA_DIR/recipes/recipes.conf"
        echo "seeded $DATA_DIR/recipes/recipes.conf"
    fi
    logins=""
    [ ! -f "$RECIPES_ENV_FILE" ] || logins="--env-file $RECIPES_ENV_FILE"
    # Only the recipes folder is mounted: this container never sees the database or config.
    # shellcheck disable=SC2086
    docker run -d --name forenoon-recipes --restart unless-stopped \
        --stop-timeout 30 \
        --read-only --tmpfs /tmp \
        --cap-drop ALL --security-opt no-new-privileges:true \
        -v "$DATA_DIR/recipes:/recipes" \
        -e TZ -e RECIPES_TIME $logins \
        "$RECIPES_IMAGE" >/dev/null ||
        echo "recipes container did not start; the rest of the deploy is unaffected"
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
    # No secrets are passed. It shares the data folder for the database: it reads what was
    # delivered, and writes accounts, sessions and rating changes.
    docker run -d --name forenoon-web --restart unless-stopped \
        --stop-timeout 30 \
        --read-only --tmpfs /tmp \
        --cap-drop ALL --security-opt no-new-privileges:true \
        -v "$DATA_DIR:/data" \
        -p "$WEB_LISTEN:8081" \
        -e TZ \
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

if [ "${ONLY:-}" = "recipes" ]; then
    echo "$(date -u +%FT%TZ) recreating forenoon-recipes from ${RECIPES_IMAGE:-(none set)}"
    start_recipes
    sleep 3
    docker ps --filter name=forenoon-recipes --format '{{.Names}}: {{.Status}}'
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
for f in config.yaml sources.yaml forenoon.sqlite3; do
    if [ -f "$DIR/$f" ] && [ ! -e "$DATA_DIR/$f" ]; then
        cp "$DIR/$f" "$DATA_DIR/$f"
        chown 1000:1000 "$DATA_DIR/$f"
        echo "seeded $DATA_DIR/$f"
    fi
done
[ -f "$DATA_DIR/config.yaml" ] || { echo "missing $DATA_DIR/config.yaml"; exit 1; }

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

run_app forenoon -e FORENOON_SMTP_PASSWORD -e FORENOON_ANTHROPIC_API_KEY "$IMAGE" schedule
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
start_recipes
start_web

sleep 5
docker ps --filter name=forenoon --format '{{.Names}}: {{.Status}}'
docker logs forenoon 2>&1 | tail -3
PUBLIC_URL=$(grep -E '^public_base_url:' "$DATA_DIR/config.yaml" | head -1 | sed -E 's/^public_base_url:[[:space:]]*//; s/[[:space:]]*(#.*)?$//; s/^"//; s/"$//')
echo "verify: curl ${PUBLIC_URL:-https://your-feedback-hostname}/healthz"
