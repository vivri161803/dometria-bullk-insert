#!/usr/bin/env bash
# Tail stdout/stderr logs of the Bulk Insert Docker container.
#
# Usage:
#   ./docker-logs.sh
#   ./docker-logs.sh my-container-name
#   CONTAINER_NAME=bulk-insert ./docker-logs.sh
#   ./docker-logs.sh --since 10m
#   ./docker-logs.sh --tail 200
#
# Without an explicit name, the script auto-detects a running container whose
# name or image matches *bulk-insert* (prefers running; falls back to exited).
# Extra args after the optional container name are forwarded to `docker logs`.

set -euo pipefail

readonly NAME_PATTERN='bulk-insert'

discover_container() {
    local -a matches=()
    local name image

    while IFS=$'\t' read -r name image; do
        [[ -z "$name" ]] && continue
        if [[ "$name" == *"$NAME_PATTERN"* || "$image" == *"$NAME_PATTERN"* ]]; then
            matches+=("$name")
        fi
    done < <(docker ps --format '{{.Names}}\t{{.Image}}' 2>/dev/null)

    if [[ ${#matches[@]} -eq 0 ]]; then
        while IFS=$'\t' read -r name image; do
            [[ -z "$name" ]] && continue
            if [[ "$name" == *"$NAME_PATTERN"* || "$image" == *"$NAME_PATTERN"* ]]; then
                matches+=("$name")
            fi
        done < <(docker ps -a --format '{{.Names}}\t{{.Image}}' 2>/dev/null)
    fi

    if [[ ${#matches[@]} -eq 0 ]]; then
        printf 'No container matching *%s* found.\n' "$NAME_PATTERN" >&2
        printf 'Hint: set CONTAINER_NAME or pass the name as the first argument.\n' >&2
        return 1
    fi

    if [[ ${#matches[@]} -gt 1 ]]; then
        printf 'Multiple containers match *%s*:\n' "$NAME_PATTERN" >&2
        printf '  - %s\n' "${matches[@]}" >&2
        printf 'Hint: set CONTAINER_NAME or pass the name as the first argument.\n' >&2
        return 1
    fi

    printf '%s\n' "${matches[0]}"
}

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
    discover_container
}

NAME_ARG=""
if [[ $# -gt 0 && "${1}" != --* ]]; then
    NAME_ARG="$1"
    shift
fi
CONTAINER="$(resolve_container "$NAME_ARG")"

if ! docker container inspect "$CONTAINER" >/dev/null 2>&1; then
    printf 'Container not found: %s\n' "$CONTAINER" >&2
    printf 'Hint: set CONTAINER_NAME or pass the name as the first argument.\n' >&2
    exit 1
fi

printf 'Using container: %s\n' "$CONTAINER" >&2
exec docker logs -f --tail 200 "$CONTAINER" "$@"
