#!/bin/sh
# Continuous deploy for a docker compose install: pull the images the compose file names and
# apply them. `docker compose up -d` replaces only the containers whose image changed (the
# tunnel along with the web container it lives in) and starts any that stopped, so an
# unchanged install costs a manifest check per image. After a host reboot it also brings back
# a tunnel that started before the web container and failed to join it.
#
# Runs from a scheduler on the host, in the folder with docker-compose.yml and .env (Synology
# DSM Task Scheduler, every 5 minutes, as root; elsewhere: cron):
#
#   cd /path/to/compose-folder && sh poll-compose.sh >> poll-compose.log 2>&1
#
# For the images to come from CI, set IMAGE, WEB_IMAGE (and BROWSER_IMAGE) in .env to their
# ghcr.io :latest tags; a service built here has nothing to pull and is left as it is.
#
# Exit codes: 0 = nothing to do, or updated and healthy; 1 = a container is not running after
# `up`, or FORENOON_PUBLIC_URL's /healthz does not answer. Configure the scheduler to notify
# on abnormal termination.
set -e

env_get() {
    [ -f .env ] && grep -E "^$1=" .env | tail -1 | cut -d= -f2- | tr -d '\r'
}
PUBLIC_URL="${PUBLIC_URL:-$(env_get FORENOON_PUBLIC_URL)}"

# A service built here, not pulled, fails to pull; the others are still updated.
docker compose pull -q --ignore-pull-failures >/dev/null 2>&1 || true

before=$(docker compose ps -q | sort)
out=$(docker compose up -d 2>&1) || { echo "$(date -u +%FT%TZ) up failed:"; echo "$out"; exit 1; }
after=$(docker compose ps -q | sort)

if [ "$before" != "$after" ]; then
    echo "$(date -u +%FT%TZ) containers replaced or started:"
    echo "$out" | grep -v -E ' (Running|Started)$' || true
    docker image prune -f >/dev/null
fi

down=$(docker compose ps -a --format '{{.Name}} {{.State}}' | grep -v ' running$' || true)
if [ -n "$down" ]; then
    echo "$(date -u +%FT%TZ) NOT RUNNING: $down"
    exit 1
fi

# Through the tunnel: three tries, 5s apart, so one blip does not page. Longer right after a
# change, while the web container starts.
[ -n "$PUBLIC_URL" ] || exit 0
tries=3
[ "$before" = "$after" ] || tries=12
i=0
while [ $i -lt $tries ]; do
    curl -sf -m 5 "$PUBLIC_URL/healthz" >/dev/null 2>&1 && exit 0
    i=$((i + 1))
    sleep 5
done
echo "$(date -u +%FT%TZ) UNHEALTHY: $PUBLIC_URL/healthz not responding"
exit 1
