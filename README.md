# Minecraft Beta 1.7.3 Server on Railway

A Minecraft Beta 1.7.3 server hosted on Railway with automatic sleep/wake and idle shutdown to save costs.

## Features

- **Beta 1.7.3 vanilla server** - the classic Minecraft experience
- **Auto-wake sleep proxy** - when the server is off, a lightweight proxy listens on the game port. When a player connects, they see "Server is waking up! Reconnect in ~30 seconds" and the server auto-starts. No web panel needed for your friends.
- **Auto-shutdown** - server stops after 10 minutes of no players (configurable), saving Railway credits
- **Admin web panel** - private dashboard for you to monitor status, logs, and control the server
- **Persistent world** - your world data survives redeploys (requires Railway volume)
- **Discord chat bridge** - Minecraft chat can relay to Discord, and an existing Discord bot can relay channel messages back into the world

## Deploy to Railway

### 1. Create the project

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) and create a new project
3. Select **Deploy from GitHub repo** and pick your repo

### 2. Configure networking

In the Railway service settings:

1. **HTTP port**: Railway auto-detects the admin panel on `$PORT` (no config needed)
2. **TCP proxy** (for Minecraft connections):
   - Go to **Settings -> Networking**
   - Click **Add TCP Proxy**
   - Set internal port to **25565**
   - Railway assigns a public host + port (for example, `roundhouse.proxy.rlwy.net:12345`)
   - **This is the only address you share with friends**

### 3. Add a persistent volume

To keep your world across deploys:

1. Go to your service -> **Settings -> Volumes**
2. Click **Add Volume**
3. Set mount path to `/server/data`
4. This stores the world, ops list, whitelist, and plugin data persistently

### 4. Set environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MC_MAX_MEMORY` | `512M` | Max Java heap size |
| `MC_MIN_MEMORY` | `256M` | Min Java heap size |
| `IDLE_TIMEOUT` | `600` | Seconds of no players before auto-stop |
| `AUTO_START` | `false` | If `true`, starts the server on container boot instead of the sleep proxy |
| `MC_PUBLIC_ADDRESS` | unset | Public Railway TCP proxy host and port shown in the admin panel, for example `roundhouse.proxy.rlwy.net:12345` |
| `DISCORD_WEBHOOK_URL` | unset | Discord incoming webhook URL for Minecraft -> Discord chat |
| `DISCORD_BRIDGE_SECRET` | unset | Shared secret your existing Discord bot must send to `/api/discord-chat` |
| `DISCORD_CHANNEL_ID` | unset | Optional Discord channel ID to enforce for inbound bot relays |
| `ADMIN_KEY` | random | Admin panel password; set this explicitly for a stable login |

### 5. Share with friends

Give your friends **only the TCP proxy address** (for example, `roundhouse.proxy.rlwy.net:12345`).

- If the server is running, they join normally
- If the server is sleeping, they see a waking message and reconnect after about 30 seconds

The admin web panel (`https://your-service.up.railway.app`) is for you only.

## How the Cost Saving Works

Railway charges per minute of CPU/RAM usage. This setup has two modes:

| Mode | What's Running | Resource Usage | Cost |
|------|----------------|----------------|------|
| **Active** | MC server + sleep proxy off | ~512 MB RAM + CPU | Normal |
| **Sleeping** | Sleep proxy + admin panel only | ~30 MB RAM, minimal CPU | Near-zero |

Lifecycle: player connects -> sleep proxy wakes server -> players play -> everyone leaves -> idle timeout expires -> server auto-stops -> sleep proxy resumes.

## Client Setup

### Recommended: BetaCraft Launcher

[BetaCraft](https://betacraft.uk) is the best way to play Beta 1.7.3:

- **Working skins** - patches the old skin URLs to work with modern Mojang APIs
- **Proper sounds** - downloads all Beta sound resources correctly
- **Classic textures** - the authentic Beta 1.7.3 look

### Alternative: Official Minecraft Launcher

1. Go to **Installations -> New Installation**
2. Select version `old_beta b1.7.3`
3. Note: skins may not display correctly with the official launcher

### Connecting

1. Launch Beta 1.7.3
2. Click **Multiplayer**
3. Enter the TCP proxy address
4. If the server is sleeping, wait about 30 seconds and reconnect after the wake-up message

## Server Administration

- **Admin panel**: `https://your-service.up.railway.app` - keep this URL private
- **Ops**: edit `ops.txt` to add operator usernames, one per line
- **Whitelist**: set `white-list=true` in `server.properties` and add usernames to `whitelist.txt`
- **Console commands**: use the admin panel API at `POST /api/command` with `{"command":"say hello"}`

## Discord Bridge

The bridge has two parts:

- **Minecraft -> Discord**: the `DiscordBridge` plugin captures player chat and the Python manager relays it to `DISCORD_WEBHOOK_URL`
- **Discord -> Minecraft**: your existing Discord bot forwards messages from one chosen channel to `POST /api/discord-chat`

### Inbound Bot Contract

Send requests to:

`https://your-service.up.railway.app/api/discord-chat`

Required header:

- `X-Discord-Bridge-Secret: <DISCORD_BRIDGE_SECRET>`

Example body:

```json
{
  "channel_id": "123456789012345678",
  "author": "discord_display_name",
  "content": "hello from discord"
}
```

Behavior:

- If `DISCORD_CHANNEL_ID` is set, the request must include the same `channel_id`
- The endpoint returns `409` if the Minecraft server is not currently running
- The endpoint does not auto-wake the server from Discord chat
- Names and messages are sanitized and length-limited before being shown in-game

## Project Structure

```text
BetaServer/
|-- Dockerfile          # Java 8 + Python 3 container
|-- manager.py          # Sleep proxy + server manager + admin panel + Discord bridge relay
|-- plugins/            # Poseidon plugins, including DiscordBridge
|-- railway.toml        # Railway deployment config
|-- server.properties   # Minecraft server config
|-- start.sh            # Container entrypoint
|-- templates/          # Admin web panel UI
|-- ops.txt             # Server operators
`-- whitelist.txt       # Whitelisted players
```
