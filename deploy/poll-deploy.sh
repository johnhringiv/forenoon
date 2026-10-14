#!/bin/sh
# Poll the deploy image tag and redeploy when its digest changes, and
# self-heal: an unchanged tag with a container not running (typical after a
# host reboot, where cloudflared can start before the feedback container and
# fail to join its network namespace) also triggers a redeploy.
#
# Runs from a scheduler on the host (Synology DSM Task Scheduler, every 5
# minutes, as root; elsewhere: cron). Deploys are pull-based: CI publishes to
# GHCR and this poller converges on it, so the host needs no inbound access.
# The cloudflared image is polled the same way; when only it changed, only the
# tunnel container is replaced; the optional browser image likewise. Unchanged
# healthy tags cost a manifest check each and one /healthz probe.
#
#   sh poll-deploy.sh [env-file]
#
# Exit codes: 0 = no-op or successful deploy; 1 = unhealthy (a deploy ran but
# the edge never came up, or the containers are running yet the edge is
# unreachable: tunnel/DNS trouble a redeploy would not fix). Configure the
# scheduler to notify on abnormal termination.
set -e

ENV_FILE="${1:-.env}"
DIR=$(dirname "$0")

[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE"; exit 1; }

env_get() {
    grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '\r'
}

IMAGE="${IMAGE:-$(env_get IMAGE)}"
[ -n "$IMAGE" ] || { echo "IMAGE not set in $ENV_FILE"; exit 1; }
# The feedback service's public address, health-checked through the tunnel.
PUBLIC_URL="${PUBLIC_URL:-$(env_get FORENOON_PUBLIC_URL)}"
TUNNEL_TOKEN=$(env_get TUNNEL_TOKEN)
CLOUDFLARED_IMAGE="${CLOUDFLARED_IMAGE:-$(env_get CLOUDFLARED_IMAGE)}"
CLOUDFLARED_IMAGE="${CLOUDFLARED_IMAGE:-cloudflare/cloudflared:latest}"

BROWSER_IMAGE="${BROWSER_IMAGE:-$(env_get BROWSER_IMAGE)}"

CONTAINERS="forenoon forenoon-feedback"
[ -z "$TUNNEL_TOKEN" ] || CONTAINERS="$CONTAINERS forenoon-cloudflared"
WEB_LISTEN="${WEB_LISTEN:-$(env_get WEB_LISTEN)}"
WEB_IMAGE="${WEB_IMAGE:-$(env_get WEB_IMAGE)}"
# The browser container is healed on its own below: its trouble must never bounce the app.

all_running() {
    for c in $CONTAINERS; do
        [ "$(docker inspect "$c" --format '{{.State.Running}}' 2>/dev/null)" = "true" ] || return 1
    done
}

# Three attempts, 5s apart: one blip should not page or churn containers.
edge_healthy() {
    [ -n "$PUBLIC_URL" ] || return 0
    i=0
    while [ $i -lt 3 ]; do
        curl -sf -m 5 "$PUBLIC_URL/healthz" >/dev/null 2>&1 && return 0
        i=$((i + 1))
        sleep 5
    done
    return 1
}

# Before CI's first publish the tag may not exist; that is not an error.
if ! docker pull -q "$IMAGE" >/dev/null 2>&1; then
    echo "$(date -u +%FT%TZ) pull failed for $IMAGE (tag not published yet?); skipping"
    exit 0
fi

latest=$(docker image inspect "$IMAGE" --format '{{.Id}}')
running=$(docker inspect forenoon --format '{{.Image}}' 2>/dev/null || echo none)

# The tunnel image: a failed pull here is not fatal, the running one keeps working.
tunnel_changed=false
if [ -n "$TUNNEL_TOKEN" ] && docker pull -q "$CLOUDFLARED_IMAGE" >/dev/null 2>&1; then
    tunnel_latest=$(docker image inspect "$CLOUDFLARED_IMAGE" --format '{{.Id}}')
    tunnel_running=$(docker inspect forenoon-cloudflared --format '{{.Image}}' 2>/dev/null || echo none)
    [ "$tunnel_latest" = "$tunnel_running" ] || tunnel_changed=true
fi

# The browser image likewise: when it changed, or its container stopped, only that
# container is replaced.
browser_changed=false
if [ -n "$BROWSER_IMAGE" ] && docker pull -q "$BROWSER_IMAGE" >/dev/null 2>&1; then
    browser_latest=$(docker image inspect "$BROWSER_IMAGE" --format '{{.Id}}')
    browser_running=$(docker inspect forenoon-browser --format '{{.Image}}' 2>/dev/null || echo none)
    browser_up=$(docker inspect forenoon-browser --format '{{.State.Running}}' 2>/dev/null || echo false)
    if [ "$browser_latest" != "$browser_running" ] || [ "$browser_up" != "true" ]; then
        browser_changed=true
    fi
fi

# The web pages image, the same way. A failed pull is logged: a newly published package is
# private until made public, and the container is then neither updated nor restarted.
web_changed=false
if [ -n "$WEB_LISTEN" ] && [ -n "$WEB_IMAGE" ]; then
    if docker pull -q "$WEB_IMAGE" >/dev/null 2>&1; then
        web_latest=$(docker image inspect "$WEB_IMAGE" --format '{{.Id}}')
        web_running=$(docker inspect forenoon-web --format '{{.Image}}' 2>/dev/null || echo none)
        web_up=$(docker inspect forenoon-web --format '{{.State.Running}}' 2>/dev/null || echo false)
        if [ "$web_latest" != "$web_running" ] || [ "$web_up" != "true" ]; then
            web_changed=true
        fi
    else
        echo "$(date -u +%FT%TZ) pull failed for $WEB_IMAGE (package not public yet?); web container left as it is"
    fi
fi

if [ "$latest" = "$running" ]; then
    if all_running; then
        if [ "$web_changed" = true ]; then
            echo "$(date -u +%FT%TZ) web container is stale or stopped; replacing it only"
            ONLY=web sh "$DIR/deploy.sh" "$ENV_FILE" || echo "$(date -u +%FT%TZ) web container did not start"
            docker image prune -f >/dev/null
        fi
        if [ "$browser_changed" = true ]; then
            echo "$(date -u +%FT%TZ) browser container is stale or stopped; replacing it only"
            ONLY=browser sh "$DIR/deploy.sh" "$ENV_FILE" || echo "$(date -u +%FT%TZ) browser container did not start"
            docker image prune -f >/dev/null
        fi
        if [ "$tunnel_changed" = true ]; then
            echo "$(date -u +%FT%TZ) new image for $CLOUDFLARED_IMAGE; replacing the tunnel container only"
            ONLY=tunnel sh "$DIR/deploy.sh" "$ENV_FILE"
            sleep 10
            if edge_healthy; then
                echo "$(date -u +%FT%TZ) tunnel healthy at edge"
                docker image prune -f >/dev/null
                exit 0
            fi
            echo "$(date -u +%FT%TZ) TUNNEL UNHEALTHY after update: $PUBLIC_URL/healthz not responding"
            exit 1
        fi
        if edge_healthy; then
            exit 0
        fi
        echo "$(date -u +%FT%TZ) UNHEALTHY: containers running but $PUBLIC_URL/healthz unreachable (tunnel/DNS?); not redeploying"
        exit 1
    fi
    echo "$(date -u +%FT%TZ) containers not running on unchanged image (host rebooted?); redeploying"
else
    echo "$(date -u +%FT%TZ) new image for $IMAGE (running=$running -> $latest); deploying"
fi

# deploy.sh health-gates the feedback service from inside its container.
sh "$DIR/deploy.sh" "$ENV_FILE"

if [ -n "$PUBLIC_URL" ]; then
    i=0
    while [ $i -lt 12 ]; do
        if curl -sf -m 5 "$PUBLIC_URL/healthz" >/dev/null 2>&1; then
            echo "$(date -u +%FT%TZ) deploy healthy at edge"
            docker image prune -f >/dev/null
            exit 0
        fi
        i=$((i + 1))
        sleep 5
    done
    echo "$(date -u +%FT%TZ) DEPLOY UNHEALTHY: $PUBLIC_URL/healthz not responding"
    exit 1
fi
echo "$(date -u +%FT%TZ) deployed (no PUBLIC_URL; edge check skipped)"
