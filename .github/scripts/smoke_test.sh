#!/usr/bin/env bash
# Boot the built image the way Railway does (fresh root-owned volume, ADMIN_KEY
# set, nobody playing) and check the things a deploy depends on.
#
# Usage: .github/scripts/smoke_test.sh [image]   (default: railpowered:ci)
set -euo pipefail

IMAGE="${1:-railpowered:ci}"
NAME="railpowered-smoke"
VOLUME="railpowered-smoke-data"
PORT=18080

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker volume rm -f "$VOLUME" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

fail() {
  echo "FAIL: $*" >&2
  echo "--- container logs ---" >&2
  docker logs "$NAME" >&2 || true
  exit 1
}

docker volume create "$VOLUME" >/dev/null
docker run -d --name "$NAME" \
  -p "127.0.0.1:$PORT:8080" \
  -e ADMIN_KEY=ci-smoke-test-passphrase \
  -e AUTO_START=false \
  -v "$VOLUME:/server/data" \
  "$IMAGE" >/dev/null

echo "Waiting for /health..."
healthy=""
for _ in $(seq 1 90); do
  if body="$(curl -fsS "http://127.0.0.1:$PORT/health" 2>/dev/null)"; then
    healthy="$body"
    break
  fi
  sleep 1
done
[ -n "$healthy" ] || fail "/health did not answer within 90 seconds"
echo "/health: $healthy"
grep -q '"ok"' <<<"$healthy" || fail "/health did not report ok"

login_page="$(curl -fsS "http://127.0.0.1:$PORT/login")" || fail "/login did not load"
# The real sign-in page from /server/web (not the built-in fallback), with
# content-hashed asset links so a redeploy never serves a stale script.
grep -Eq 'src="/static/login\.js\?v=[0-9a-f]{12}"' <<<"$login_page" \
  || fail "/login is not the web/ sign-in page (is web/ missing from the image?)"
curl -fsS -o /dev/null "http://127.0.0.1:$PORT/static/app.js" || fail "/static/app.js did not load"

[ "$(docker exec "$NAME" cat /proc/1/comm)" = "tini" ] || fail "PID 1 is not tini"

# The manager must run as the unprivileged minecraft user.
docker exec "$NAME" python3 -c '
import os, pwd, sys
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            argv = handle.read().split(b"\0")
    except OSError:
        continue
    if any(arg.endswith(b"manager.py") for arg in argv):
        user = pwd.getpwuid(os.stat(f"/proc/{pid}").st_uid).pw_name
        print(f"manager.py runs as {user}")
        sys.exit(0 if user == "minecraft" else 1)
sys.exit("manager.py is not running")
' || fail "manager.py is not running as minecraft"

for file in eula.txt server.properties; do
  owner="$(docker exec "$NAME" stat -c %U "/server/data/$file")" || fail "/server/data/$file is missing"
  [ "$owner" = "minecraft" ] || fail "/server/data/$file is owned by $owner"
done
# start.sh writes "eula=true"; the installer later adds a comment line linking the EULA.
docker exec "$NAME" grep -qx 'eula=true' /server/data/eula.txt || fail "eula.txt is wrong"

for major in 8 21 25; do
  docker exec "$NAME" "/opt/java/$major/bin/java" -version >/dev/null 2>&1 || fail "Java $major does not run"
done

echo "Stopping (SIGTERM, 60 s grace like Railway's drainingSeconds)..."
start="$(date +%s)"
docker stop -t 60 "$NAME" >/dev/null
code="$(docker inspect -f '{{.State.ExitCode}}' "$NAME")"
echo "Stopped in $(( $(date +%s) - start ))s, exit code $code."

echo "--- last log lines ---"
docker logs --tail 30 "$NAME" 2>&1
# 137 means SIGKILL: SIGTERM did not reach the manager or it did not exit.
[ "$code" != "137" ] || fail "the container ignored SIGTERM and was killed"
echo "Smoke test passed."
