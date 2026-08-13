#!/usr/bin/env bash
# Start (or recreate) the Bulk Insert Docker container using the root `.env`.
#
# Usage:
#   ./docker-run.sh
#   IMAGE=ghcr.io/devitalia-dometria/dometria-mod-bulk-insert:1.2.3 ./docker-run.sh
#   PORT=8001 ./docker-run.sh
#   CONTAINER_NAME=bulk-insert-dev ./docker-run.sh
#
# Extra args after options are forwarded to `docker run` (before the image).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly ENV_FILE="${SCRIPT_DIR}/.env"
readonly DEFAULT_CONTAINER="dometria-mod-bulk-insert"
readonly DEFAULT_IMAGE="ghcr.io/devitalia-dometria/dometria-mod-bulk-insert:latest"
readonly DEFAULT_PORT="8000"

CONTAINER_NAME="${CONTAINER_NAME:-$DEFAULT_CONTAINER}"
IMAGE="${IMAGE:-$DEFAULT_IMAGE}"
PORT="${PORT:-$DEFAULT_PORT}"

log() {
    printf '[docker-run] %s\n' "$*"
}

if [[ ! -f "$ENV_FILE" ]]; then
    log "Missing env file: $ENV_FILE"
    log "Copy .env.example to .env and fill in the values."
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    log "docker is not installed or not on PATH."
    exit 1
fi

if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    log "Removing existing container: $CONTAINER_NAME"
    docker rm -f "$CONTAINER_NAME" >/dev/null
fi

log "Starting $CONTAINER_NAME from $IMAGE (port $PORT, env-file $ENV_FILE)"
docker run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    -p "${PORT}:8000" \
    --env-file "$ENV_FILE" \
    "$@" \
    "$IMAGE"

log "Started. Health: curl -s http://127.0.0.1:${PORT}/health"
docker ps --filter "name=^/${CONTAINER_NAME}$" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
