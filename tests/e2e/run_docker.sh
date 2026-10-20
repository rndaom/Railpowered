#!/usr/bin/env bash
# End-to-end check of the Docker image, run the way Railway runs it: a fresh
# volume, ADMIN_KEY set, AUTO_START off, the dashboard and Minecraft ports
# published (here on loopback, on free host ports).
#
#   1. Boot the image and run driver.py's "full" scenario. It ends with
#      Minecraft online.
#   2. docker stop -t 60 (Railway's drainingSeconds): the manager must save,
#      stop Minecraft and exit 0 without needing SIGKILL.
#   3. Boot a new container on the same volume (a redeploy) and run the
#      "redeploy" scenario, then stop that one the same way.
#
# Usage: tests/e2e/run_docker.sh [image]   (default: railpowered:e2e)
# Build the image first, e.g. docker build -t railpowered:e2e .
#
# Container logs and the driver's state file go to $E2E_OUT (default
# ./e2e-output). Set E2E_KEEP=1 to keep the volume afterwards.
set -euo pipefail

IMAGE="${1:-railpowered:e2e}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${E2E_OUT:-$PWD/e2e-output}"
PYTHON="${PYTHON:-python3}"
DEADLINE="${E2E_DEADLINE_MINUTES:-16}"
NAME="railpowered-e2e"
VOLUME="railpowered-e2e-data"
KEY="railpowered-e2e-check-passphrase"
current=""

save_logs() {
  if [ -n "$current" ] && docker inspect "$NAME" >/dev/null 2>&1; then
    docker logs "$NAME" >"$OUT/container-$current.log" 2>&1 || true
  fi
}

cleanup() {
  save_logs
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  if [ "${E2E_KEEP:-0}" != "1" ]; then
    docker volume rm -f "$VOLUME" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  save_logs
  if [ -n "$current" ] && [ -f "$OUT/container-$current.log" ]; then
    echo "--- last 60 lines of the $current container's log ---" >&2
    tail -n 60 "$OUT/container-$current.log" >&2
  fi
  exit 1
}

host_port() {
  local mapping
  mapping="$(docker port "$NAME" "$1/tcp" | head -n 1)"
  [ -n "$mapping" ] || fail "port $1 is not published"
  echo "${mapping##*:}"
}

boot() {
  current="$1"
  docker run -d --name "$NAME" \
    -p 127.0.0.1::8080 \
    -p 127.0.0.1::25565 \
    --memory 3g \
    -e ADMIN_KEY="$KEY" \
    -e AUTO_START=false \
    -v "$VOLUME:/server/data" \
    "$IMAGE" >/dev/null
  web_port="$(host_port 8080)"
  mc_port="$(host_port 25565)"
  echo "Container '$current' is up: dashboard on 127.0.0.1:$web_port, Minecraft on 127.0.0.1:$mc_port."
}

drive() {
  "$PYTHON" "$HERE/driver.py" \
    --url "http://127.0.0.1:$web_port" \
    --mc-host 127.0.0.1 \
    --mc-port "$mc_port" \
    --key "$KEY" \
    --state "$OUT/state.json" \
    --deadline-minutes "$DEADLINE" \
    "$@"
}

# Stop like Railway does and check the shutdown was clean. With Minecraft
# online ($1 = online) the log must also show it being saved and stopped.
stop_container() {
  local started code log
  echo "Stopping the '$current' container (SIGTERM, 60 s grace)..."
  started="$(date +%s)"
  docker stop -t 60 "$NAME" >/dev/null
  code="$(docker inspect -f '{{.State.ExitCode}}' "$NAME")"
  save_logs
  log="$OUT/container-$current.log"
  echo "Stopped in $(( $(date +%s) - started ))s with exit code $code."
  [ "$code" != "137" ] || fail "the container ignored SIGTERM and was killed"
  [ "$code" = "0" ] || fail "the manager exited with code $code"
  grep -q "Railpowered stopped." "$log" || fail "the manager never logged that it stopped"
  if [ "${1:-}" = "online" ]; then
    sed -n '/Shutdown requested/,$p' "$log" | grep -q "Minecraft stopped" \
      || fail "Minecraft was not stopped cleanly on SIGTERM"
  fi
  if grep -n "Traceback (most recent call last)" "$log" >&2; then
    fail "the '$current' container logged a Python traceback"
  fi
  if grep -nE "could not listen on port|Could not open the dashboard port" "$log" >&2; then
    fail "a port could not be bound"
  fi
}

mkdir -p "$OUT"
rm -f "$OUT"/container-*.log "$OUT/state.json"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker volume rm -f "$VOLUME" >/dev/null 2>&1 || true
docker volume create "$VOLUME" >/dev/null

boot first
drive --scenario full || fail "the full scenario failed"
stop_container online
docker rm "$NAME" >/dev/null

echo
boot redeploy
drive --scenario redeploy || fail "the redeploy scenario failed"
stop_container
echo
echo "End-to-end check passed."
