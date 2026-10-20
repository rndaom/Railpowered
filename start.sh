#!/usr/bin/env bash
# Railpowered container entrypoint.
#
# 1. Makes sure the data volume exists and seeds what is missing. It only ever
#    creates files; it never moves, renames or deletes anything on the volume.
# 2. Accepts the Minecraft EULA on the deployer's behalf and says so.
# 3. Hands the volume to the unprivileged "minecraft" user and starts the
#    manager (dashboard, sleep proxy and Minecraft) as that user.
#
# Python owns everything else (folder layout, setups, server.properties keys).
set -euo pipefail

DATA_DIR="${MC_DIR:-/server/data}"
APP_DIR="${RAILPOWERED_APP_DIR:-/server}"
RUN_USER="${RAILPOWERED_USER:-minecraft}"
EULA_URL="https://www.minecraft.net/eula"

log() {
  printf '[start.sh] %s\n' "$*"
}

# Run under tini when started as PID 1. The image's ENTRYPOINT already does
# this; a platform start command (for example Railway's) replaces ENTRYPOINT,
# so start.sh covers that case itself.
if [ "$$" -eq 1 ] && command -v tini >/dev/null 2>&1; then
  exec tini -- "$0" "$@"
fi

mkdir -p "$DATA_DIR"

if [ -z "${MC_DIR:-}" ] && command -v mountpoint >/dev/null 2>&1 && ! mountpoint -q "$DATA_DIR"; then
  log "Note: $DATA_DIR is not a mounted volume. Worlds will be lost when the container is replaced."
fi

# First-run seed: copy the default server.properties only when there is none.
if [ ! -e "$DATA_DIR/server.properties" ] && [ -f "$APP_DIR/server.properties" ]; then
  cp "$APP_DIR/server.properties" "$DATA_DIR/server.properties"
  log "Created $DATA_DIR/server.properties with Railpowered defaults."
fi

# Point out data that Railpowered will not touch, so nothing looks lost.
if [ -f "$DATA_DIR/world/level.dat" ]; then
  log "Found a world at $DATA_DIR/world (from another server image?). Railpowered keeps worlds in $DATA_DIR/worlds/<name>/ and leaves this folder as it is. Move it into worlds/ to play it."
fi
if [ -d "$DATA_DIR/archive" ]; then
  log "Found $DATA_DIR/archive from an earlier Railpowered version. It is left untouched."
fi

log "Minecraft EULA: by running this server you agree to the Minecraft EULA ($EULA_URL). Railpowered writes eula=true for you."
printf 'eula=true\n' > "$DATA_DIR/eula.txt"

start_manager() {
  exec python3 "$APP_DIR/manager.py"
}

# Not root (for example docker run --user, or RAILWAY_RUN_UID): run as-is.
if [ "$(id -u)" -ne 0 ]; then
  log "Starting Railpowered as uid $(id -u)."
  start_manager
fi

if ! id -u "$RUN_USER" >/dev/null 2>&1 || ! command -v setpriv >/dev/null 2>&1; then
  log "Warning: user '$RUN_USER' or setpriv is missing, so Railpowered runs as root."
  start_manager
fi

run_uid="$(id -u "$RUN_USER")"
run_gid="$(id -g "$RUN_USER")"

# Railway mounts volumes owned by root, and volumes from older Railpowered
# images were written by root. Hand everything to the run user, but only when
# something is not already theirs, so normal boots skip the walk.
if [ -n "$(find "$DATA_DIR" -xdev \( ! -user "$run_uid" -o ! -group "$run_gid" \) -print -quit 2>/dev/null)" ]; then
  log "Giving $RUN_USER ownership of $DATA_DIR (only needed once per volume)..."
  if ! chown -R -h "$run_uid:$run_gid" "$DATA_DIR"; then
    log "Warning: could not change ownership of every file in $DATA_DIR."
  fi
fi

as_run_user=(setpriv --reuid="$run_uid" --regid="$run_gid" --clear-groups --no-new-privs --)

probe="$DATA_DIR/.railpowered-write-test"
# shellcheck disable=SC2016  # $1 is expanded by the inner sh, not here.
if "${as_run_user[@]}" sh -c 'touch "$1" && rm -f "$1"' sh "$probe" 2>/dev/null; then
  home_dir="$(getent passwd "$RUN_USER" | cut -d: -f6 || true)"
  export HOME="${home_dir:-/home/$RUN_USER}" USER="$RUN_USER" LOGNAME="$RUN_USER"
  log "Starting Railpowered as $RUN_USER."
  exec "${as_run_user[@]}" python3 "$APP_DIR/manager.py"
fi

rm -f "$probe" 2>/dev/null || true
log "Warning: $RUN_USER cannot write to $DATA_DIR, so Railpowered runs as root. Check the volume's permissions."
start_manager
