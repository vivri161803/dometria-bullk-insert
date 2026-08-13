#!/usr/bin/env bash
# Run scripts/test_connect_connection.py inside the Bulk Insert Docker container.
#
# The image does not bake scripts/; this wrapper feeds the host copy into the
# container Python (or runs /app/scripts/... if that path already exists).
# Connect credentials come from the container env (CONNECT_BASE_URL,
# CONNECT_CALLBACK_TOKEN, …).
#
# Usage:
#   ./docker-test-connect.sh
#   ./docker-test-connect.sh my-container-name
#   CONTAINER_NAME=bulk-insert ./docker-test-connect.sh
#   ./docker-test-connect.sh -- --base-url https://connect.dometria.ai -v
#
# Extra args after the optional container name (or after `--`) are forwarded
# to the Python script.

set -euo pipefail

readonly DEFAULT_CONTAINER="dometria-mod-bulk-insert"
readonly CONTAINER_SCRIPT="/app/scripts/test_connect_connection.py"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly HOST_SCRIPT="${SCRIPT_DIR}/scripts/test_connect_connection.py"

resolve_container() {
    local name="${1:-}"
    if [[ -n "$name" ]]; then
        printf '%s\n' "$name"
        return
    fi
    if [[ -n "${CONTAINER_NAME:-}" ]]; then
        printf '%s\n' "$CONTAINER_NAME"
        return
    fi
    printf '%s\n' "$DEFAULT_CONTAINER"
}

NAME_ARG=""
if [[ $# -gt 0 && "${1}" != --* ]]; then
    NAME_ARG="$1"
    shift
fi
# Allow an explicit `--` separator before Python args.
if [[ $# -gt 0 && "${1}" == "--" ]]; then
    shift
fi
CONTAINER="$(resolve_container "$NAME_ARG")"

if [[ ! -f "$HOST_SCRIPT" ]]; then
    printf 'Host script not found: %s\n' "$HOST_SCRIPT" >&2
    exit 1
fi

if ! docker container inspect "$CONTAINER" >/dev/null 2>&1; then
    printf 'Container not found: %s\n' "$CONTAINER" >&2
    printf 'Hint: set CONTAINER_NAME or pass the name as the first argument.\n' >&2
    exit 1
fi

if docker exec "$CONTAINER" test -f "$CONTAINER_SCRIPT"; then
    exec docker exec "$CONTAINER" python "$CONTAINER_SCRIPT" "$@"
fi

# Script not in the image: stream the host file into `python -`.
exec docker exec -i "$CONTAINER" python - "$@" <"$HOST_SCRIPT"
