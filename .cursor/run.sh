#!/usr/bin/env bash
# Launch the Roundhouse manager (dashboard + sleep proxy).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

bash "$REPO_DIR/.cursor/sync-server.sh"

export MINECRAFT_VERSION="${MINECRAFT_VERSION:-latest}"
export SERVER_TYPE="${SERVER_TYPE:-vanilla}"
export JAVA_8_HOME="${JAVA_8_HOME:-/opt/java/8}"
export JAVA_21_HOME="${JAVA_21_HOME:-/opt/java/21}"
export JAVA_25_HOME="${JAVA_25_HOME:-/opt/java/25}"
export JAVA_HOME="${JAVA_HOME:-/opt/java/25}"
export PATH="${JAVA_HOME}/bin:${PATH}"
export PORT="${PORT:-8080}"
export ADMIN_KEY="${ADMIN_KEY:-admin}"
export AUTO_START="${AUTO_START:-false}"

cd /server
exec ./start.sh
