# Minecraft 1.2.5 Server on Railway

A friends-only Minecraft Java server. The default after deploy is **vanilla 1.2.5**. The same admin site can later switch vanilla, Fabric, curated Forge, or an uploaded modpack, plus worlds and backups.

## Default versions

- Minecraft Java: `1.2.5` (vanilla)
- Java for 1.2.5: `8`
- Extra Javas in the image for later switches: `21` and `25`

The official 1.2.5 server jar is pinned in the Docker image and checked with SHA-1 `d8321edc9470e56b8ad5c67bbd16beba25843336`.

## What you get

- Sleep proxy on port `25565` (legacy 1.2.5 clients and modern 1.7+ clients)
- Auto-stop after 10 minutes with no players (`IDLE_TIMEOUT`)
- Admin web panel on Railway HTTP: start/stop, logs, console, version/type, worlds, backups, whitelist, ops
- Persistent data on the Railway volume at `/server/data`

## First deploy reset

The first boot after this update **archives** leftover Fabric 26.2 / beta files on the volume to:

```text
/server/data/archive/fabric-26.2/<timestamp>/
```

It does not delete that archive. Then it seeds a fresh vanilla 1.2.5 config and an empty `worlds/world` folder.

## Deploy to Railway

1. HTTP should point at `$PORT` (admin panel + `/health`).
2. Add a TCP proxy with internal port `25565`. Share that host:port with players.
3. Mount a volume at `/server/data`.
4. Set `ADMIN_KEY` to a password you will remember.
5. Set `MC_PUBLIC_ADDRESS` to the public TCP proxy host:port so the panel can show it.

### Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `MC_MAX_MEMORY` | `1G` | Max Java heap |
| `MC_MIN_MEMORY` | `512M` | Min Java heap |
| `IDLE_TIMEOUT` | `600` | Seconds with no players before auto-stop |
| `AUTO_START` | `false` | Start Minecraft on container boot instead of the sleep proxy |
| `MC_PUBLIC_ADDRESS` | unset | Public TCP address shown in the panel |
| `ADMIN_KEY` | random | Admin panel password |
| `MINECRAFT_VERSION` | `1.2.5` | Used only when `manager.json` does not exist yet |
| `SERVER_TYPE` | `vanilla` | Used only when `manager.json` does not exist yet |

Changing version later is done in the **admin panel**. That writes `/server/data/manager.json` on the volume. You do not need a Railway rebuild for a version switch.

## How friends join (1.2.5)

1. Use Minecraft Java **1.2.5**, not the latest release and not Fabric.
2. Official launcher: create an installation, pick release `1.2.5`.
3. Or Prism / MultiMC with 1.2.5 and the Betacraft proxy `betacraft.ee:11707`.
4. Ask an admin to whitelist the exact player name.
5. Connect to the Railway TCP address. If the server is sleeping, wait ~30 seconds and reconnect.

1.2.5 defaults to `online-mode=false` and `white-list=true`. Offline mode means a stranger who can reach the port can spoof a name, so keep the whitelist on.

## Admin panel

- URL: `https://your-service.up.railway.app`
- Start / stop / save-all / console commands
- Switch type (`vanilla`, `fabric`, `forge`, `modpack`) and version
- Create, select, and delete worlds
- Backup, download, and restore worlds
- Edit whitelist and ops

Forge is a curated list: `1.7.10`, `1.12.2`, `1.16.5`. Fabric picks a loader from Fabric meta for that Minecraft version. Modrinth `.mrpack` files install the Fabric or Forge loader from pack dependencies. Worlds stay in `/server/data/worlds` and are reused across loaders.

Do not reuse a 1.2.5 world on a modern version (or the other way around) unless you know it is compatible. The panel asks for a world name when you switch.

## Project structure

```text
|-- Dockerfile          Java 8/21/25 image, pinned 1.2.5 jar
|-- start.sh            Volume reset/archive, then manager
|-- manager.py          Sleep proxy, process control, admin API
|-- installer.py        Jars, loaders, worlds, backups
|-- manager.json        Default runtime config
|-- server.properties   1.2.5 defaults
|-- templates/          Admin UI
`-- archive/            Retired beta 1.7.3 and Fabric 26.2 files (not used)
```
