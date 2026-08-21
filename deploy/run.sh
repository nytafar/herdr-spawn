#!/usr/bin/env bash
# Launch herdr-spawn over streamable HTTP, bound to one private interface.
#
# When it sits behind a Docker-hosted MCP gateway, the right bind address is that
# gateway network's bridge IP and nothing else. Discover it at start rather than
# hardcoding it: compose files usually do not pin the IPAM subnet, so the address
# moves if the network is ever recreated.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

: "${HERDR_SPAWN_TRANSPORT:=streamable-http}"
: "${HERDR_SPAWN_PORT:=8230}"
: "${HERDR_SPAWN_DOCKER_NETWORK:=}"
: "${HERDR_SPAWN_BIND:=}"

if [ -z "$HERDR_SPAWN_BIND" ] && [ -n "$HERDR_SPAWN_DOCKER_NETWORK" ]; then
  HERDR_SPAWN_BIND="$(docker network inspect "$HERDR_SPAWN_DOCKER_NETWORK" \
      -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)"
  [ -n "$HERDR_SPAWN_BIND" ] ||
    echo "warn: could not read a gateway IP from docker network '$HERDR_SPAWN_DOCKER_NETWORK'" >&2
fi

# Loopback is the fallback, never 0.0.0.0. This tool starts autonomous agents, and
# on a host with no firewall the bind address is the entire security boundary — so
# a failed discovery must fail closed (gateway gets 502) rather than open.
: "${HERDR_SPAWN_BIND:=127.0.0.1}"

export HERDR_SPAWN_TRANSPORT HERDR_SPAWN_BIND HERDR_SPAWN_PORT
exec uv run --directory "$ROOT" python -m herdr_spawn.server
