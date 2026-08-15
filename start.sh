#!/bin/bash
set -euo pipefail

DATA_DIR="/server/data"
RESET_MARKER="$DATA_DIR/.vanilla-reset-complete"
RESET_ARCHIVE_ROOT="$DATA_DIR/archive/reset-to-vanilla"

mkdir -p "$DATA_DIR"

if [ ! -f "$RESET_MARKER" ]; then
  archive_dir="$RESET_ARCHIVE_ROOT/$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$archive_dir"
  archived_any=false

  for path in \
    world world_nether world_the_end \
    mods plugins config libraries versions .fabric \
    fabric-server-launch.jar server.jar server.properties eula.txt \
    server.log logs latest.log \
    poseidon.yml ops.txt ops.json whitelist.txt whitelist.json \
    banned-players.txt banned-players.json banned-ips.txt banned-ips.json \
    usercache.json .fabric-migration-complete
  do
    if [ -e "$DATA_DIR/$path" ]; then
      mv "$DATA_DIR/$path" "$archive_dir/"
      archived_any=true
    fi
  done

  if [ "$archived_any" = true ]; then
    echo "[start.sh] Archived previous server data to $archive_dir"
  else
    rmdir "$archive_dir"
  fi

  {
    echo "minecraft_version=${MINECRAFT_VERSION:-unknown}"
    echo "reset_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$RESET_MARKER"
fi

cp -f /server/server.jar "$DATA_DIR/"
cp -n /server/server.properties "$DATA_DIR/server.properties" 2>/dev/null || true
printf "eula=true\n" > "$DATA_DIR/eula.txt"

echo "[start.sh] Starting vanilla server manager..."
exec python3 /server/manager.py
