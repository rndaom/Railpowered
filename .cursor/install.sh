#!/usr/bin/env bash
# Idempotent setup for the development environment.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[install] Java version:" && java -version
echo "[install] Python version:" && python3 --version

python3 -m py_compile "$REPO_DIR/manager.py" "$REPO_DIR/installer.py"
python3 -m compileall -q "$REPO_DIR/mc_host"
python3 -m unittest "$REPO_DIR/tests/test_runtime.py"

bash "$REPO_DIR/.cursor/sync-server.sh"

echo "[install] Roundhouse development environment ready."
