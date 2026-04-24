#!/bin/bash
set -e

DATA_DIR="/server/data"
mkdir -p "$DATA_DIR"
mkdir -p "$DATA_DIR/plugins"

# Copy server files to persistent data directory while preserving user-edited config
# Always update server JAR so Poseidon replaces vanilla on upgrade
cp -f /server/server.jar "$DATA_DIR/" 2>/dev/null || true
cp -n /server/server.properties "$DATA_DIR/" 2>/dev/null || true
cp -n /server/poseidon.yml "$DATA_DIR/" 2>/dev/null || true
cp -n /server/ops.txt "$DATA_DIR/" 2>/dev/null || true
cp -n /server/whitelist.txt "$DATA_DIR/" 2>/dev/null || true

# Ensure Poseidon's tree-growth blocker always protects cobblestone as well.
if [ -f "$DATA_DIR/poseidon.yml" ]; then
python3 - "$DATA_DIR/poseidon.yml" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
block_match = re.search(
    r"(?ms)^(\s*block-tree-growth:\s*\n(?:(?:\s{12}|\t).*\n?)*)",
    text,
)
if not block_match:
    raise SystemExit(0)

block = block_match.group(1)
list_match = re.search(r"(?m)^(\s*list:\s*)([0-9,\s]+)$", block)
if not list_match:
    raise SystemExit(0)

values = [value.strip() for value in list_match.group(2).split(",") if value.strip()]
if "4" in values:
    raise SystemExit(0)

values.insert(0, "4")
updated_block = (
    block[:list_match.start(2)]
    + ",".join(values)
    + block[list_match.end(2):]
)
path.write_text(
    text[:block_match.start(1)] + updated_block + text[block_match.end(1):],
    encoding="utf-8",
    newline="\n",
)
PY
fi

# Always update plugins to latest build
cp -f /server/plugins/*.jar "$DATA_DIR/plugins/" 2>/dev/null || true

echo "[start.sh] Starting server manager..."
exec python3 /server/manager.py
