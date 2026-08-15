# Vanilla Minecraft Server on Railway

A current vanilla Minecraft Java server hosted on Railway with the existing admin panel, sleep proxy, and idle shutdown behavior.

## Current Version

- Minecraft Java: `26.2`
- Java runtime: `25`
- Server jar: Mojang vanilla server jar, SHA-1 `823e2250d24b3ddac457a60c92a6a941943fcd6a`

Minecraft `26.2` is the latest release resolved from Mojang's version manifest on 2026-07-05. The server jar is pinned in the `Dockerfile` for reproducible Railway builds.

## Features

- **Official vanilla Java server** with no Fabric, Forge, plugins, or bundled mods
- **Auto-wake sleep proxy** on port `25565`
- **Auto-shutdown** after 10 minutes with no players, configurable through `IDLE_TIMEOUT`
- **Admin web panel** on Railway's HTTP port for status, logs, start/stop, and console commands
- **Persistent server data** under `/server/data`
- **One-time vanilla reset** that archives old world, mod, plugin, and loader data before the fresh world is created

## Fresh Vanilla Reset

On first boot after this reset, `start.sh` archives previous runtime data from the Railway volume into:

```text
/server/data/archive/reset-to-vanilla/<timestamp>/
```

It moves existing worlds, `mods/`, `plugins/`, Fabric files, old jars, logs, and server access lists out of the active data directory. Then it writes:

```text
/server/data/.vanilla-reset-complete
```

That marker prevents later restarts from wiping the new vanilla world.

The retired Beta 1.7.3 / Project Poseidon source archive remains under:

```text
archive/beta-1.7.3/
```

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

This stores the vanilla world, server properties, EULA file, and the archive created during the reset.

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
- To op a player after reset, start the server and run `op <playername>` from the admin command box

## Project Structure

```text
BetaServer/
|-- Dockerfile          # Java 25 + vanilla server image
|-- manager.py          # Sleep proxy + server manager + admin panel
|-- railway.toml        # Railway deployment config
|-- server.properties   # Modern Minecraft server defaults
|-- start.sh            # Container entrypoint and one-time vanilla reset
|-- templates/          # Admin web panel UI
`-- archive/            # Retired beta server assets
```
