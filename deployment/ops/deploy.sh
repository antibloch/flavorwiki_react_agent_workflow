#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${DEPLOY_PATH:-/opt/herbalife-agent}"
COMPOSE_FILE="$APP_DIR/compose.production.yaml"
COMPOSE_ENV="$APP_DIR/.compose.env"
CANDIDATE_ENV="$APP_DIR/.compose.candidate.env"
APP_ENV="$APP_DIR/app.env"
NEW_IMAGE="${1:?usage: deploy.sh REGISTRY_IMAGE:IMMUTABLE_TAG}"

case "$NEW_IMAGE" in
  *[!a-zA-Z0-9._:/@-]*)
    echo "invalid image reference" >&2
    exit 2
    ;;
esac

if [[ ! -r "$APP_ENV" ]]; then
  echo "$APP_ENV is missing or unreadable; create it on the server before deploying" >&2
  exit 1
fi

cd "$APP_DIR"

PREVIOUS_IMAGE=""
if [[ -f "$COMPOSE_ENV" ]]; then
  PREVIOUS_IMAGE="$(sed -n 's/^APP_IMAGE=//p' "$COMPOSE_ENV" | tail -n 1)"
fi

write_image() {
  local image="$1"
  local destination="$2"
  local temporary="$destination.tmp"
  umask 077
  printf 'APP_IMAGE=%s\n' "$image" > "$temporary"
  mv "$temporary" "$destination"
}

rollback() {
  local exit_code=$?
  trap - ERR
  echo "deployment failed for $NEW_IMAGE" >&2
  rm -f "$CANDIDATE_ENV" "$CANDIDATE_ENV.tmp"
  if [[ -n "$PREVIOUS_IMAGE" && "$PREVIOUS_IMAGE" != "$NEW_IMAGE" ]]; then
    echo "rolling back to $PREVIOUS_IMAGE" >&2
    docker compose --env-file "$COMPOSE_ENV" -f "$COMPOSE_FILE" pull
    docker compose --env-file "$COMPOSE_ENV" -f "$COMPOSE_FILE" \
      up -d --remove-orphans --wait --wait-timeout 180
  else
    echo "no different previously healthy image is recorded; automatic rollback is unavailable" >&2
  fi
  exit "$exit_code"
}
trap rollback ERR

write_image "$NEW_IMAGE" "$CANDIDATE_ENV"
docker compose --env-file "$CANDIDATE_ENV" -f "$COMPOSE_FILE" config --quiet
docker compose --env-file "$CANDIDATE_ENV" -f "$COMPOSE_FILE" pull
docker compose --env-file "$CANDIDATE_ENV" -f "$COMPOSE_FILE" \
  up -d --remove-orphans --wait --wait-timeout 180
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8000/health >/dev/null
mv "$CANDIDATE_ENV" "$COMPOSE_ENV"

trap - ERR
docker compose --env-file "$COMPOSE_ENV" -f "$COMPOSE_FILE" ps
echo "deployment healthy: $NEW_IMAGE"
