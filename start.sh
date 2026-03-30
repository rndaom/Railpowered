#!/bin/bash
set -e

DATA_DIR="/server/data"
mkdir -p "$DATA_DIR"

# Copy server files to persistent data directory (don't overwrite existing)
cp -n /server/server.jar "$DATA_DIR/" 2>/dev/null || true
cp -n /server/server.properties "$DATA_DIR/" 2>/dev/null || true
cp -n /server/ops.txt "$DATA_DIR/" 2>/dev/null || true
cp -n /server/whitelist.txt "$DATA_DIR/" 2>/dev/null || true

echo "[start.sh] Starting server manager..."
exec python3 /server/manager.py
