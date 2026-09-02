#!/bin/bash
set -euo pipefail

DATA_DIR="/server/data"
RESET_MARKER="$DATA_DIR/.roundhouse-v1"
ARCHIVE_ROOT="$DATA_DIR/archive/pre-roundhouse"

mkdir -p "$DATA_DIR" "$DATA_DIR/worlds" "$DATA_DIR/jars" "$DATA_DIR/backups" \
  "$DATA_DIR/instances" "$DATA_DIR/modpacks"

has_previous_runtime() {
  if [ -f "$DATA_DIR/manager.json" ] || [ -f "$DATA_DIR/server.properties" ]; then
    return 0
  fi
  if [ -d "$DATA_DIR/world" ] || [ -d "$DATA_DIR/mods" ] || [ -d "$DATA_DIR/plugins" ]; then
    return 0
  fi
  if [ -d "$DATA_DIR/libraries" ] || [ -d "$DATA_DIR/.fabric" ]; then
    return 0
  fi
  if [ -f "$DATA_DIR/ops.txt" ] || [ -f "$DATA_DIR/ops.json" ]; then
    return 0
  fi
  if [ -f "$DATA_DIR/whitelist.json" ] || [ -f "$DATA_DIR/white-list.txt" ]; then
    return 0
  fi
  return 1
}

if [ ! -f "$RESET_MARKER" ]; then
  if has_previous_runtime; then
    archive_dir="$ARCHIVE_ROOT/$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$archive_dir"
    for path in \
      world world_nether world_the_end worlds \
      plugins mods libraries versions .fabric instances modpacks \
      poseidon.yml server.properties ops.txt whitelist.txt white-list.txt \
      ops.json whitelist.json banned-players.json banned-ips.json \
      banned-players.txt banned-ips.txt usercache.json eula.txt \
      fabric-server-launch.jar server.jar server.log logs manager.json \
      .fabric-migration-complete .legacy-125-reset-complete
    do
      if [ -e "$DATA_DIR/$path" ]; then
        mv "$DATA_DIR/$path" "$archive_dir/"
      fi
    done
    echo "[start.sh] Archived previous runtime to $archive_dir"
  fi

  mkdir -p "$DATA_DIR/worlds/world" "$DATA_DIR/jars" "$DATA_DIR/backups" \
    "$DATA_DIR/instances" "$DATA_DIR/modpacks"
  cp -f /server/server.properties "$DATA_DIR/server.properties"
  cp -f /server/manager.json "$DATA_DIR/manager.json"
  : > "$DATA_DIR/ops.txt"
  : > "$DATA_DIR/white-list.txt"
  printf '%s\n' '[]' > "$DATA_DIR/ops.json"
  printf '%s\n' '[]' > "$DATA_DIR/whitelist.json"
  {
    echo "product=roundhouse"
    echo "default=latest-vanilla"
    echo "reset_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$RESET_MARKER"
  echo "[start.sh] Seeded latest vanilla defaults"
fi

if [ ! -f "$DATA_DIR/server.properties" ]; then
  cp /server/server.properties "$DATA_DIR/server.properties"
fi
if [ ! -f "$DATA_DIR/manager.json" ]; then
  cp /server/manager.json "$DATA_DIR/manager.json"
fi

mkdir -p "$DATA_DIR/worlds/world"
printf "eula=true\n" > "$DATA_DIR/eula.txt"

echo "[start.sh] Starting Roundhouse..."
exec python3 /server/manager.py
