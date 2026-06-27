# Fabric Minecraft Server on Railway

A current Minecraft Java server running on Fabric, hosted on Railway with the existing admin panel, sleep proxy, and idle shutdown behavior.

## Current Versions

- Minecraft Java: `26.2`
- Fabric Loader: `0.19.3`
- Fabric Installer: `1.1.1`
- Fabric API: `0.153.0+26.2`
- Java runtime: `25`

These are pinned in the `Dockerfile` for reproducible Railway builds.

## Features

- **Modern Fabric server** for the latest stable Minecraft Java release
- **Auto-wake sleep proxy** on port `25565`
- **Auto-shutdown** after 10 minutes with no players, configurable through `IDLE_TIMEOUT`
- **Admin web panel** on Railway's HTTP port for status, logs, start/stop, and console commands
- **Persistent server data** under `/server/data`
- **One-time beta archive migration** for old world/plugin data on the Railway volume
- **Managed server mods** copied into `/server/data/mods` on each deploy

## Beta Archive

The retired Beta 1.7.3 / Project Poseidon files are preserved under:

```text
archive/beta-1.7.3/
```

On the first Fabric boot, `start.sh` also checks the Railway persistent volume for beta markers such as `plugins/`, `poseidon.yml`, `ops.txt`, or `whitelist.txt`. If found, it moves the old runtime data into:

```text
/server/data/archive/beta-1.7.3/<timestamp>/
```

Then it writes `/server/data/.fabric-migration-complete` so later restarts do not archive the new Fabric world.

## Deploy to Railway

### 1. Configure networking

The service exposes the admin panel through Railway HTTP and Minecraft through a TCP proxy.

1. In Railway, open the service settings.
2. HTTP should point at `$PORT` automatically.
3. Add a TCP proxy with internal port `25565`.
4. Share only the assigned TCP proxy host and port with players.

### 2. Add a persistent volume

Mount the volume at:

```text
/server/data
```

This stores the Fabric world, server properties, `mods/`, EULA file, and the beta archive created during migration.

### 3. Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `MC_MAX_MEMORY` | `1G` | Max Java heap size |
| `MC_MIN_MEMORY` | `512M` | Min Java heap size |
| `IDLE_TIMEOUT` | `600` | Seconds of no players before auto-stop |
| `AUTO_START` | `false` | If `true`, starts the server on container boot instead of the sleep proxy |
| `MC_PUBLIC_ADDRESS` | unset | Public Railway TCP proxy host and port shown in the admin panel |
| `ADMIN_KEY` | random | Admin panel password; set this explicitly for a stable login |

## Administration

- Admin panel: `https://your-service.up.railway.app`
- Console commands: use `POST /api/command` or the admin panel command box
- To op a player after migration, start the server and run `op <playername>` from the admin command box
- Fabric API and Vanilla Minions are managed by the Docker image and copied into `/server/data/mods`
- Additional Fabric mods can be uploaded into `/server/data/mods` and loaded on the next server start

## Project Structure

```text
BetaServer/
|-- Dockerfile          # Java 25 + Fabric server image
|-- manager.py          # Sleep proxy + server manager + admin panel
|-- railway.toml        # Railway deployment config
|-- server.properties   # Modern Minecraft server defaults
|-- server-mods/         # Managed local mod jars copied into the server image
|-- start.sh            # Container entrypoint and beta volume migration
|-- templates/          # Admin web panel UI
`-- archive/            # Retired beta server assets
```
