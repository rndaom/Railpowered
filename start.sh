#!/bin/bash
set -e

DATA_DIR="/server/data"
mkdir -p "$DATA_DIR"
mkdir -p "$DATA_DIR/plugins"

# Copy server files to persistent data directory while preserving user-edited config
# Always update server JAR so Poseidon replaces vanilla on upgrade
cp -f /server/server.jar "$DATA_DIR/" 2>/dev/null || true
cp -n /server/server.properties "$DATA_DIR/" 2>/dev/null || true
cp -n /server/ops.txt "$DATA_DIR/" 2>/dev/null || true
cp -n /server/whitelist.txt "$DATA_DIR/" 2>/dev/null || true

# Always update plugins to latest build
cp -f /server/plugins/*.jar "$DATA_DIR/plugins/" 2>/dev/null || true

echo "[start.sh] Starting server manager..."
exec python3 /server/manager.py
