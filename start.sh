#!/bin/bash
set -euo pipefail

DATA_DIR="/server/data"
RESET_MARKER="$DATA_DIR/.legacy-125-reset-complete"
ARCHIVE_ROOT="$DATA_DIR/archive/fabric-26.2"

mkdir -p "$DATA_DIR" "$DATA_DIR/worlds" "$DATA_DIR/jars" "$DATA_DIR/backups" \
  "$DATA_DIR/instances" "$DATA_DIR/modpacks"

needs_archive() {
  if [ -f "$DATA_DIR/.fabric-migration-complete" ]; then
    return 0
  fi
  if [ -d "$DATA_DIR/mods" ] || [ -d "$DATA_DIR/libraries" ] || [ -d "$DATA_DIR/.fabric" ]; then
    return 0
  fi
  if [ -f "$DATA_DIR/fabric-server-launch.jar" ] || [ -f "$DATA_DIR/poseidon.yml" ] || [ -d "$DATA_DIR/plugins" ]; then
    return 0
  fi
  if [ -f "$DATA_DIR/server.properties" ] && grep -Eq "simulation-distance|enforce-secure-profile|Fabric Minecraft" "$DATA_DIR/server.properties"; then
    return 0
  fi
  return 1
}

if [ ! -f "$RESET_MARKER" ]; then
  if needs_archive; then
    archive_dir="$ARCHIVE_ROOT/$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$archive_dir"
    for path in \
      world world_nether world_the_end \
      plugins mods libraries versions .fabric \
      poseidon.yml server.properties ops.txt whitelist.txt white-list.txt \
      ops.json whitelist.json banned-players.json banned-ips.json \
      banned-players.txt banned-ips.txt usercache.json eula.txt \
      fabric-server-launch.jar server.jar server.log logs \
      .fabric-migration-complete
    do
      if [ -e "$DATA_DIR/$path" ]; then
        mv "$DATA_DIR/$path" "$archive_dir/"
      fi
    done
    echo "[start.sh] Archived previous runtime to $archive_dir"
  fi

  cp -f /server/server.properties "$DATA_DIR/server.properties"
  if [ ! -f "$DATA_DIR/manager.json" ]; then
    cp /server/manager.json "$DATA_DIR/manager.json"
  fi
  : >> "$DATA_DIR/ops.txt"
  : >> "$DATA_DIR/white-list.txt"
  mkdir -p "$DATA_DIR/worlds/world"
  {
    echo "reset=1.2.5-vanilla"
    echo "reset_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$RESET_MARKER"
  echo "[start.sh] Seeded vanilla 1.2.5 runtime files"
fi

if [ -f /server/jars/minecraft_server.1.2.5.jar ]; then
  cp -f /server/jars/minecraft_server.1.2.5.jar "$DATA_DIR/jars/minecraft_server.1.2.5.jar"
fi

if [ ! -f "$DATA_DIR/server.properties" ]; then
  cp /server/server.properties "$DATA_DIR/server.properties"
fi
if [ ! -f "$DATA_DIR/manager.json" ]; then
  cp /server/manager.json "$DATA_DIR/manager.json"
fi

mkdir -p "$DATA_DIR/worlds/world"
printf "eula=true\n" > "$DATA_DIR/eula.txt"

echo "[start.sh] Starting Minecraft server manager..."
exec python3 /server/manager.py
