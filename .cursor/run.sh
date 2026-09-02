#!/usr/bin/env bash
# Launch the vanilla 1.2.5 server manager (admin panel + sleep proxy).
# Runs as a long-lived foreground process in a Cloud Agent terminal.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Reflect the current branch's runtime files before starting.
bash "$REPO_DIR/.cursor/sync-server.sh"

# Defaults match the production Dockerfile; override via env/secrets.
export MINECRAFT_VERSION="${MINECRAFT_VERSION:-1.2.5}"
export SERVER_TYPE="${SERVER_TYPE:-vanilla}"
export JAVA_8_HOME="${JAVA_8_HOME:-/opt/java/8}"
export JAVA_21_HOME="${JAVA_21_HOME:-/opt/java/21}"
export JAVA_25_HOME="${JAVA_25_HOME:-/opt/java/25}"
export JAVA_HOME="${JAVA_HOME:-/opt/java/8}"
export PATH="${JAVA_HOME}/bin:${PATH}"
export PORT="${PORT:-8080}"
# Dev-only default admin key for the local panel; set ADMIN_KEY (secret) to override.
export ADMIN_KEY="${ADMIN_KEY:-admin}"
# Start the sleep proxy by default (production behavior); set AUTO_START=true to
# boot the Minecraft server immediately.
export AUTO_START="${AUTO_START:-false}"

cd /server
exec ./start.sh
