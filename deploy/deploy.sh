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
#
# IMAGE comes from the environment, then the env file, then forenoon:local.
# CLOUDFLARED_IMAGE likewise, defaulting to cloudflare/cloudflared:latest.
# Roll back with:  sudo IMAGE=ghcr.io/<owner>/<repo>:sha-abc123 sh deploy.sh
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
DATA_DIR=$(env_get FORENOON_HOST_DATA)
[ -n "$DATA_DIR" ] || { echo "FORENOON_HOST_DATA is not set in $ENV_FILE"; exit 1; }
TZ=$(env_get TZ)
export TZ="${TZ:-UTC}"
FORENOON_FEEDBACK_SECRET=$(env_get FORENOON_FEEDBACK_SECRET)
[ -n "$FORENOON_FEEDBACK_SECRET" ] || { echo "FORENOON_FEEDBACK_SECRET is not set in $ENV_FILE"; exit 1; }
FORENOON_SMTP_PASSWORD=$(env_get FORENOON_SMTP_PASSWORD)
TUNNEL_TOKEN=$(env_get TUNNEL_TOKEN)
# Exported so `docker run -e NAME` can pass them without the values appearing in `ps`.
export FORENOON_FEEDBACK_SECRET FORENOON_SMTP_PASSWORD TUNNEL_TOKEN

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

run_app forenoon -e FORENOON_SMTP_PASSWORD "$IMAGE" schedule
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

sleep 5
docker ps --filter name=forenoon --format '{{.Names}}: {{.Status}}'
docker logs forenoon 2>&1 | tail -3
PUBLIC_URL=$(grep -E '^public_base_url:' "$DATA_DIR/config.yaml" | head -1 | sed -E 's/^public_base_url:[[:space:]]*//; s/[[:space:]]*(#.*)?$//; s/^"//; s/"$//')
echo "verify: curl ${PUBLIC_URL:-https://your-feedback-hostname}/healthz"
