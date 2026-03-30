# Minecraft Beta 1.7.3 Server on Railway

A Minecraft Beta 1.7.3 server hosted on Railway with automatic sleep/wake and idle shutdown to save costs.

## Features

- **Beta 1.7.3 vanilla server** — the classic Minecraft experience
- **Auto-wake sleep proxy** — when the server is off, a lightweight proxy listens on the game port. When a player connects, they see "Server is waking up! Reconnect in ~30 seconds" and the server auto-starts. No web panel needed for your friends.
- **Auto-shutdown** — server stops after 10 minutes of no players (configurable), saving Railway credits
- **Admin web panel** — private dashboard for you to monitor status, logs, and control the server
- **Persistent world** — your world data survives redeploys (requires Railway volume)

## Deploy to Railway

### 1. Create the project

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) and create a new project
3. Select **"Deploy from GitHub repo"** and pick your repo

### 2. Configure networking

In the Railway service settings:

1. **HTTP port**: Railway auto-detects the admin panel on `$PORT` (no config needed)
2. **TCP proxy** (for Minecraft connections):
   - Go to **Settings** → **Networking**
   - Click **"Add TCP Proxy"**
   - Set internal port to **25565**
   - Railway assigns a public host + port (e.g., `roundhouse.proxy.rlwy.net:12345`)
   - **This is the only address you share with friends**

### 3. Add a persistent volume

To keep your world across deploys:

1. Go to your service → **Settings** → **Volumes**
2. Click **"Add Volume"**
3. Set mount path to `/server/data`
4. This stores the world, ops list, and whitelist persistently

### 4. Set environment variables (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `MC_MAX_MEMORY` | `512M` | Max Java heap size |
| `MC_MIN_MEMORY` | `256M` | Min Java heap size |
| `IDLE_TIMEOUT` | `600` | Seconds of no players before auto-stop (default 10 min) |
| `AUTO_START` | `false` | If `true`, starts MC server on container boot instead of sleep proxy |

### 5. Share with friends

Give your friends **only the TCP proxy address** (e.g., `roundhouse.proxy.rlwy.net:12345`). They just connect to it in Minecraft:

- If the server is running, they join normally
- If the server is sleeping, they see a "waking up" message and reconnect after ~30 seconds

The admin web panel (`https://your-service.up.railway.app`) is for you only.

## How the Cost Saving Works

Railway charges per minute of CPU/RAM usage. This setup has two modes:

| Mode | What's Running | Resource Usage | Cost |
|------|---------------|---------------|------|
| **Active** | MC server + sleep proxy off | ~512MB RAM + CPU | Normal |
| **Sleeping** | Sleep proxy + admin panel only | ~30MB RAM, minimal CPU | Near-zero |

The lifecycle: Player connects → sleep proxy wakes server → players play → everyone leaves → 10 min idle → server auto-stops → sleep proxy resumes. The idle cost is fractions of a cent per day.

## Client Setup (for players)

### Recommended: BetaCraft Launcher

[BetaCraft](https://betacraft.uk) is the best way to play Beta 1.7.3:
- **Working skins** — patches the old skin URLs to work with modern Mojang APIs
- **Proper sounds** — downloads all Beta sound resources correctly
- **Classic textures** — the authentic Beta 1.7.3 look

### Alternative: Official Minecraft Launcher

1. Go to **Installations** → **New Installation**
2. Select version `old_beta b1.7.3`
3. Note: skins may not display correctly with the official launcher

### Connecting

1. Launch Beta 1.7.3
2. Click **Multiplayer**
3. Enter the TCP proxy address (e.g., `roundhouse.proxy.rlwy.net:12345`)
4. If the server is sleeping, you'll see *"Server is waking up!"* — wait ~30 seconds and reconnect

## Server Administration

- **Admin panel**: `https://your-service.up.railway.app` — keep this URL private
- **Ops**: Edit `ops.txt` to add operator usernames (one per line)
- **Whitelist**: Set `white-list=true` in `server.properties` and add usernames to `whitelist.txt`
- **Console commands**: Use the admin panel API: `POST /api/command` with `{"command": "say hello"}`

## Project Structure

```
BetaServer/
├── Dockerfile          # Java 8 + Python 3 container
├── railway.toml        # Railway deployment config
├── manager.py          # Sleep proxy + server manager + admin panel + auto-shutdown
├── start.sh            # Container entrypoint
├── server.properties   # Minecraft server config
├── ops.txt             # Server operators
├── whitelist.txt       # Whitelisted players
└── templates/
    └── index.html      # Admin web panel UI
```
