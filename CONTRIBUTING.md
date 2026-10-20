# Contributing to Railpowered

Thanks for your interest in Railpowered. Bug reports, fixes, docs and ideas
are all welcome.

Railpowered is a one-click Railway template for a Minecraft server that sleeps
when nobody is online. Please keep that path simple: one deploy, no extra
services, and a dashboard for everything else. If you plan a large change,
open an issue first so we can agree on the approach before you write it.

## Contents

- [What you need](#what-you-need)
- [Project structure](#project-structure)
- [Run it locally](#run-it-locally)
- [Work on the dashboard without Minecraft](#work-on-the-dashboard-without-minecraft)
- [Run it in Docker](#run-it-in-docker)
- [Tests and checks](#tests-and-checks)
- [Conventions](#conventions)
- [Maintainer notes](#maintainer-notes)

## What you need

- **Python 3.12** (the version in the Docker image; 3.13 is also tested). The
  code uses only the standard library, so there is nothing to `pip install`.
- **Java**, only if you want to run a real Minecraft server outside Docker:
  Java 21 or 25 for modern versions, Java 8 for 1.16.5 and older.
- **Docker**, optional, to build and run the same image Railway runs.
- **bash** and [ShellCheck](https://www.shellcheck.net/), optional, to check
  `start.sh`.

## Project structure

```
manager.py          Entry point: python3 manager.py
installer.py        Downloads and prepares Minecraft (vanilla, Fabric, Forge,
                    modpacks), and manages setups, worlds, backups, players
                    and server.properties on the data volume
mc_host/
  main.py           Startup and shutdown: signals, background prepare,
                    sleep proxy and HTTP server
  config.py         Environment variables, ports, memory and the admin key
  auth.py           Signed session cookies, logout and sign-out-everywhere,
                    login rate limits
  web.py            Static files, page templates and security headers (CSP)
  panel.py          The dashboard's HTTP server and JSON API
  tasks.py          Background tasks, one at a time (installs, switches,
                    backups, restores)
  installer_api.py  Adapter between the panel/process layer and installer.py
  process.py        Starts and stops Minecraft, the console, idle sleep, wake-up
  proxy.py          Sleep proxy on 25565: answers server-list pings and wakes
                    the server on a login (modern and 1.2.5-1.6 clients)
  state.py          Shared server state and the log buffer
web/                The dashboard: HTML, CSS and JavaScript, no build step
tools/
  mock_server.py    Serves web/ with a fake API for dashboard work
tests/              unittest suite (standard library only)
server.properties   Defaults copied to a new volume on first boot
start.sh            Container entrypoint: seeds the volume, drops root,
                    starts manager.py
Dockerfile          Ubuntu 24.04, Python 3.12, Temurin JREs 8, 17, 21 and 25
railway.toml        Railway build and deploy settings (config as code)
.railway/           Template notes and a reference IaC definition
.github/            CI, issue templates and maintenance scripts
```

Everything the server writes lives in the data directory: `/server/data` in
the container (a Railway volume), or wherever `MC_DIR` points locally. It
holds `manager.json` (setups and settings), `server.properties`, `worlds/`,
`backups/`, `instances/` (Fabric, Forge and modpack installs), `jars/`, the
player lists and the dashboard's key and session secret.

## Run it locally

From the repository root, point `MC_DIR` at a scratch folder (`data/` is
already in `.gitignore`) and choose a dashboard key:

```bash
# macOS / Linux / WSL
MC_DIR="$PWD/data" ADMIN_KEY="dev-only-passphrase" PORT=8080 python3 manager.py
```

```powershell
# Windows PowerShell
$env:MC_DIR = "$PWD\data"; $env:ADMIN_KEY = "dev-only-passphrase"; $env:PORT = "8080"
python manager.py
```

Open <http://localhost:8080> and sign in with the key. The sleep proxy listens
on port 25565, so a Minecraft client on the same machine can join
`localhost`. The first start downloads the server jar into `data/`.

Useful variables while developing:

| Variable | Default | What it does |
|---|---|---|
| `MC_DIR` | `/server/data` | Data directory. Always set it locally. |
| `ADMIN_KEY` | generated | Dashboard key. If unset, one is generated, saved in the data directory and printed in the log. |
| `PORT` | `8080` | Dashboard port. |
| `AUTO_START` | `false` | Start Minecraft at boot instead of on the first join. |
| `MINECRAFT_VERSION` | `latest` | First boot only: the version of the first setup. |
| `SERVER_TYPE` | `vanilla` | First boot only: `vanilla`, `fabric` or `forge` (Forge needs a pinned `MINECRAFT_VERSION`: 1.16.5, 1.12.2 or 1.7.10). |
| `IDLE_TIMEOUT` | `600` | Seconds with nobody online before Minecraft sleeps (`0` = never, at most `86400`: the dashboard allows up to one day). |
| `START_TIMEOUT` | `900` | Seconds a start may take before Minecraft is stopped and reported as crashed (`0` = no limit; at least `120`, at most `21600`). |
| `MC_MAX_MEMORY` / `MC_MIN_MEMORY` | `auto` / `256M` | Java heap limits. |
| `STOP_TIMEOUT` | `30` | Seconds Minecraft gets to save and exit before it is killed (5 to 45, so a shutdown fits in Railway's 60-second draining window). |
| `JAVA_8_HOME`, `JAVA_17_HOME`, `JAVA_21_HOME`, `JAVA_25_HOME` | `/opt/java/<n>` | Where each Java lives. Set only the ones you have: Railpowered picks the smallest installed Java the version needs (a Java 25 also runs 1.20.5+), then falls back to `java` on `PATH`. |
| `WEB_DIR` | `/server/web`, then `./web` | Where the dashboard files are served from. |
| `WEB_PORT` | unset | Dashboard port; overrides `PORT` (either is ignored if it equals the Minecraft port). |
| `MC_PUBLIC_ADDRESS` | unset | Join address shown in the dashboard, when Railway's TCP proxy variables (`RAILWAY_TCP_PROXY_DOMAIN`, `RAILWAY_TCP_PROXY_PORT`) don't give it. |
| `BACKUP_KEEP` | `0` | Automatic backups (before a restore, a downgrade or an upgrade) kept per world; older ones are deleted. `0` keeps all. Manual and uploaded backups are never deleted. |
| `MAX_UPLOAD_MB` | `1024` | Largest modpack or backup upload, and largest modpack download. |
| `MAX_EXTRACT_MB` | `8192` | Largest unpacked modpack (also limited by free space). |
| `FABRIC_LOADER_VERSION`, `FABRIC_INSTALLER_VERSION` | unset | Pin Fabric's loader and installer instead of using the newest stable ones. |

Fabric, Forge and modpack setups link their world folders with symlinks. On
Windows that needs Developer Mode (or WSL), so for those setups run it in
Docker or WSL.

## Work on the dashboard without Minecraft

For changes to `web/`, you don't need Java or a real server:

```bash
python3 tools/mock_server.py
```

It serves `web/` with canned API responses that follow the real API, so you
can click through every screen (online, sleeping, a task in progress, errors)
and see changes on reload. The options are described at the top of the file.

## Run it in Docker

This builds the same image Railway builds and keeps the data in a named
volume:

```bash
docker build -t railpowered .
docker run --rm -it \
  -p 8080:8080 -p 25565:25565 \
  -e ADMIN_KEY="dev-only-passphrase" \
  -v railpowered-data:/server/data \
  railpowered
```

The container starts as root only long enough to give the volume to the
`minecraft` user (uid 1000); Python and Minecraft run as that user. Press
Ctrl+C to stop: Minecraft saves the world before the container exits.

## Tests and checks

Run the same checks as CI before you open a pull request:

```bash
python -m unittest discover -s tests      # all tests, no network needed
shellcheck start.sh .github/scripts/*.sh tests/e2e/*.sh  # if you changed a shell script
docker build -t railpowered .             # if you changed the Dockerfile,
                                          # start.sh or the file layout
.github/scripts/smoke_test.sh railpowered # boots the image and checks it
```

On Windows, tests that need symlinks or bash skip themselves when those are not
available; CI runs the full suite on Linux.

CI (`.github/workflows/ci.yml`) runs the tests on Python 3.12 and 3.13 on
Linux, and on 3.12 on Windows, compiles every Python file, runs ShellCheck,
actionlint, hadolint and `node --check` on the dashboard scripts, and builds
the image and smoke-tests it. On pull requests, `.github/workflows/e2e.yml`
also builds the image and plays it through with `tests/e2e/run_docker.sh`
(start, sleep and wake, a live backup, a Fabric setup, the idle timer, a stop
with Minecraft online and a redeploy). You can run that locally with Docker:
`tests/e2e/run_docker.sh railpowered`.

## Conventions

- **Standard library only.** No third-party Python packages, in the app or
  the tests.
- **Never lose a world.** Code that touches the data volume must not move,
  overwrite or delete user data as a side effect. Deletions go through
  explicit user actions and checked paths. `start.sh` only creates what is
  missing.
- **Existing volumes keep working.** A new version has to boot on a volume
  written by an older one. If you change the format of `manager.json` or the
  folder layout, read the old format too.
- **One API contract.** Every `/api/` route returns JSON, and errors look like
  `{"error": "plain-English message", "code": "slug"}`. Requests that change
  state send the `X-Railpowered: 1` header.
- **Plain words in the UI.** Messages should tell a server owner what happened
  and what to do next, without stack traces or jargon.
- **Tests come with fixes.** A bug fix includes a test that fails without it.
  Tests use temporary directories and must not touch the network.
- **Commits** use the imperative mood ("Fix wake-up after a crash") and keep
  one logical change each.

## Maintainer notes

- **Deploy button.** The README's Deploy button must point at the published
  template (`https://railway.com/new/template/AGX0Mu`), not at a GitHub URL.
  A GitHub URL opens Railway's generic project chooser without the volume, the
  TCP proxy or the variables. See [`.railway/README.md`](.railway/README.md).
- **New runtime files.** If the image needs a new file, add it to the
  `Dockerfile` `COPY` lines, the `.dockerignore` allowlist and the
  `watchPatterns` in `railway.toml`.
- **Java updates.** The Temurin JREs are pinned by release and SHA-256 in the
  `Dockerfile`. `python3 .github/scripts/jre_versions.py --write` updates the
  pins; a weekly workflow opens an issue when a newer release is out.
- **`main` is live.** Railway template deployments follow `main`, so keep it
  green.

## Code of conduct

Be kind and assume good intent. Harassment or personal attacks are not
tolerated in issues, pull requests or anywhere else in the project.

## Security

Please report vulnerabilities privately. See [SECURITY.md](SECURITY.md).
