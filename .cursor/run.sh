#!/usr/bin/env bash
# Launch the Fabric server manager (admin panel + sleep proxy).
# Runs as a long-lived foreground process in a Cloud Agent terminal.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Reflect the current branch's runtime files before starting.
bash "$REPO_DIR/.cursor/sync-server.sh"

# Defaults mirror the production Dockerfile ARGs; override via env/secrets.
export MINECRAFT_VERSION="${MINECRAFT_VERSION:-26.2}"
export FABRIC_LOADER_VERSION="${FABRIC_LOADER_VERSION:-0.19.3}"
export FABRIC_API_VERSION="${FABRIC_API_VERSION:-0.153.0+26.2}"
export PORT="${PORT:-8080}"
# Dev-only default admin key for the local panel; set ADMIN_KEY (secret) to override.
export ADMIN_KEY="${ADMIN_KEY:-admin}"
# Start the sleep proxy by default (production behavior); set AUTO_START=true to
# boot the Minecraft server immediately.
export AUTO_START="${AUTO_START:-false}"

cd /server
exec ./start.sh
