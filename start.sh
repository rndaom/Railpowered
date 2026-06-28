#!/bin/bash
set -euo pipefail

DATA_DIR="/server/data"
MIGRATION_MARKER="$DATA_DIR/.fabric-migration-complete"
BETA_ARCHIVE_ROOT="$DATA_DIR/archive/beta-1.7.3"

mkdir -p "$DATA_DIR"

if [ ! -f "$MIGRATION_MARKER" ]; then
  if [ -d "$DATA_DIR/plugins" ] || [ -f "$DATA_DIR/poseidon.yml" ] || [ -f "$DATA_DIR/ops.txt" ] || [ -f "$DATA_DIR/whitelist.txt" ]; then
    archive_dir="$BETA_ARCHIVE_ROOT/$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$archive_dir"

    for path in \
      world world_nether world_the_end \
      plugins \
      poseidon.yml server.properties ops.txt whitelist.txt \
      server.jar server.log banned-players.txt banned-ips.txt
    do
      if [ -e "$DATA_DIR/$path" ]; then
        mv "$DATA_DIR/$path" "$archive_dir/"
      fi
    done

    echo "[start.sh] Archived beta data to $archive_dir"
  fi

  {
    echo "minecraft_version=${MINECRAFT_VERSION:-unknown}"
    echo "fabric_loader_version=${FABRIC_LOADER_VERSION:-unknown}"
    echo "migrated_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$MIGRATION_MARKER"
fi

cp -f /server/fabric-server-launch.jar "$DATA_DIR/"
cp -f /server/server.jar "$DATA_DIR/"
rm -rf "$DATA_DIR/libraries"
cp -R /server/libraries "$DATA_DIR/libraries"

if [ -d /server/versions ]; then
  rm -rf "$DATA_DIR/versions"
  cp -R /server/versions "$DATA_DIR/versions"
fi

if [ -d /server/.fabric ]; then
  rm -rf "$DATA_DIR/.fabric"
  cp -R /server/.fabric "$DATA_DIR/.fabric"
fi

cp -n /server/server.properties "$DATA_DIR/server.properties" 2>/dev/null || true
mkdir -p "$DATA_DIR/mods"
if [ -d /server/mods ]; then
  rm -f "$DATA_DIR"/mods/fabric-api-*.jar
  rm -f "$DATA_DIR"/mods/vanilla-minions-*.jar
  rm -f "$DATA_DIR"/mods/ai-builder-*.jar
  cp -f /server/mods/*.jar "$DATA_DIR/mods/" 2>/dev/null || true
fi
printf "eula=true\n" > "$DATA_DIR/eula.txt"

echo "[start.sh] Starting Fabric server manager..."
exec python3 /server/manager.py
