#!/usr/bin/env bash
# Idempotent dependency/setup refresh for the Cloud Agent environment.
# The app uses the Python standard library plus Java 8 and the pinned 1.2.5
# jar baked into the image. This validates sources and syncs them into /server.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[install] Java version:" && java -version
echo "[install] Python version:" && python3 --version

# Fail fast if manager or installer has a syntax error.
python3 -m py_compile "$REPO_DIR/manager.py" "$REPO_DIR/installer.py"

# Place current sources into /server so the app is ready to launch.
bash "$REPO_DIR/.cursor/sync-server.sh"

if [ ! -f /server/jars/minecraft_server.1.2.5.jar ]; then
  echo "[install] missing /server/jars/minecraft_server.1.2.5.jar" >&2
  exit 1
fi

echo "[install] Vanilla 1.2.5 Minecraft dev environment ready."
