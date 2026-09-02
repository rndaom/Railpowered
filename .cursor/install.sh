#!/usr/bin/env bash
# Idempotent dependency/setup refresh for the Cloud Agent environment.
# The app relies only on the Python standard library and the Java + Fabric
# server baked into the image, so this mostly validates and syncs sources.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[install] Java version:" && java -version
echo "[install] Python version:" && python3 --version

# Fail fast if the manager has a syntax error.
python3 -m py_compile "$REPO_DIR/manager.py" "$REPO_DIR/installer.py"
python3 -m compileall -q "$REPO_DIR/mc_host"

# Place current sources into /server so the app is ready to launch.
bash "$REPO_DIR/.cursor/sync-server.sh"

echo "[install] Fabric Minecraft dev environment ready."
