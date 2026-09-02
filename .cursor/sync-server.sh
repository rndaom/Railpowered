#!/usr/bin/env bash
# Sync the checked-out repository's runtime files into /server so the running
# manager reflects the current branch. Mirrors the COPY steps in the
# repository-root (production) Dockerfile. Safe to run repeatedly.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_DIR="/server"

mkdir -p "$SERVER_DIR/mods"
cp -f "$REPO_DIR/manager.py" "$SERVER_DIR/manager.py"
cp -f "$REPO_DIR/installer.py" "$SERVER_DIR/installer.py"
rm -rf "$SERVER_DIR/mc_host"
cp -R "$REPO_DIR/mc_host" "$SERVER_DIR/mc_host"
cp -f "$REPO_DIR/server.properties" "$SERVER_DIR/server.properties"
cp -f "$REPO_DIR/start.sh" "$SERVER_DIR/start.sh"
chmod +x "$SERVER_DIR/start.sh"

rm -rf "$SERVER_DIR/templates"
cp -R "$REPO_DIR/templates" "$SERVER_DIR/templates"

# Managed server mods shipped in the repo (fabric-api comes from the image).
cp -f "$REPO_DIR"/server-mods/*.jar "$SERVER_DIR/mods/" 2>/dev/null || true

echo "[sync-server] Synced repo runtime files into $SERVER_DIR"
