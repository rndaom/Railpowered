# Railway template notes

These notes are for maintainers of the published Railway template. To deploy
your own server, use the Deploy button in the main [README](../README.md).

## The template

The Deploy button points at the published template:

```
https://railway.com/new/template/AGX0Mu
```

Keep it that way. A GitHub URL such as
`railway.com/new?template=https://github.com/...` is not a one-click deploy: it
opens Railway's generic "New project" chooser without the volume, TCP proxy or
variables below.

The template creates one service, **Minecraft**, built from this repository's
`Dockerfile`, with:

| Setting | Value | Why |
|---|---|---|
| Volume | `/server/data`, 5 GB requested | Worlds, backups, setups and the dashboard's session secret. Railway caps volume size by plan, so a plan with a smaller limit gets that limit. |
| TCP proxy | port `25565` | The address players join. Railpowered reads `RAILWAY_TCP_PROXY_DOMAIN` and `RAILWAY_TCP_PROXY_PORT` to show it in the dashboard. |
| HTTP domain | port `8080` | The dashboard. `/health` answers here without login, for Railway's health check. |
| Health check | `/health`, 300 s timeout | The dashboard starts before Minecraft is downloaded, so this passes within seconds. |
| Draining | 60 s | Time between SIGTERM and SIGKILL on a redeploy or restart, so Minecraft saves the world before it stops. Railway's default is 0 s. |
| Overlap | 0 s | Only one container may use the world volume at a time. |
| Restart policy | on failure, 10 retries | |

The build, health check, draining and restart settings come from
[`railway.toml`](../railway.toml), which Railway applies on every deploy. The
volume, TCP proxy, HTTP domain and variables belong to the template itself.
[`railway.ts`](railway.ts) is a reference definition of the same service in
Railway's TypeScript IaC; when you change the template, update it too.

`railway.toml` also sets watch paths, so commits that only touch docs, tests or
CI do not redeploy anyone's server. If you add a file the image needs, add it
to `watchPatterns` and to the `COPY` lines in the `Dockerfile`.

## Variables

Set these in the template editor. The descriptions are written to be pasted
into the Deploy form as they are.

| Variable | Default | Description |
|---|---|---|
| `ADMIN_KEY` | `${{secret(32)}}` | Dashboard password, generated for you. Find it in the service's Variables after deploying, or replace it with your own long passphrase (20+ characters). Weak keys are refused. Anyone who has it controls your server. |
| `PORT` | `8080` | Dashboard port. Keep 8080 so Railway's health check and HTTP domain reach the dashboard. |
| `MINECRAFT_VERSION` | `latest` | First deploy only: the Minecraft version to start with (`latest`, or a version such as `1.21.1`). Change versions later from the dashboard. |
| `SERVER_TYPE` | `vanilla` | First deploy only: `vanilla`, `fabric` or `forge` (Forge needs a specific `MINECRAFT_VERSION`, 1.6.1 or newer). Add modpacks or other versions later from the dashboard. |
| `AUTO_START` | `false` | `true` starts Minecraft when the container boots. `false` waits until a player joins or you press Start. The dashboard setting overrides this. |
| `IDLE_TIMEOUT` | `600` | Seconds with nobody online before Minecraft goes to sleep (600 = 10 minutes, 0 = never, at most 86400 = one day). The dashboard setting overrides this. |
| `MC_MAX_MEMORY` | `auto` | Java heap limit: `auto` (fits the container, at most 2G) or a size such as `3G`. Keep it below the service's memory limit. |
| `MC_MIN_MEMORY` | `256M` | Java heap reserved at start, such as `256M` or `1G`. Must not exceed `MC_MAX_MEMORY`. |

About `ADMIN_KEY`:

- Give it the default `${{secret(32)}}` and keep it required. Railway fills in
  a random 32-character key at deploy time; the owner reads it in the
  service's **Variables** (or types their own on the Deploy form). This is the
  recommended default: a key people pick by hand is often short or reused.
- Railpowered refuses a weak `ADMIN_KEY` at boot (shorter than 12 characters,
  fewer than 5 different characters, or a well-known password). It then uses
  the key saved on the volume, says so loudly in the deploy log, and shows a
  banner in the dashboard until the key is fixed. `ALLOW_WEAK_ADMIN_KEY=1`
  turns the check off.
- If a service runs without `ADMIN_KEY` (for example one deployed straight from
  the GitHub repo), Railpowered generates a key, saves it on the volume and
  prints it in the deploy logs. Setting `ADMIN_KEY` later replaces it.
- Changing `ADMIN_KEY` signs everyone out of the dashboard.

## Template description

Use this (or something close) as the template's overview text:

> A Minecraft Java server that sleeps when nobody is online and wakes up when a
> friend joins, with a web dashboard for versions, worlds, backups and players.
> Your dashboard password is `ADMIN_KEY`: a random one is generated for you
> and shown in the service's Variables.
>
> By deploying, you agree to the Minecraft EULA
> (https://www.minecraft.net/eula). Railpowered accepts it for you so the
> server can start. Railpowered is not affiliated with Mojang or Microsoft.

## Recreate the template

You need a Railway account with access to this GitHub repository.

1. Create a project with one service from the repository (`railway init`, or
   **New project → Deploy from GitHub repo** in the dashboard).
2. On the service, add a volume mounted at `/server/data`, a TCP proxy on port
   `25565`, and a generated public domain on port `8080`.
3. Add the variables from the table above.
4. Deploy once and check that the dashboard opens and `/health` returns
   `{"status": "ok"}`.
5. Project settings → **Generate Template from Project**.
6. In the template editor, make `ADMIN_KEY` required with the default `${{secret(32)}}`,
   paste the variable descriptions and the template description, and publish.
7. Put the new `https://railway.com/new/template/<code>` URL on the Deploy
   button in `README.md` and at the top of this file.

## Updating a published template

Railway watches the template's source repository. When the default branch
changes, people who deployed the template get an update notification and apply
it when they choose, so a broken commit on `main` reaches real servers. Keep
`main` green (CI builds the image and runs the tests). Template settings
(variables, their descriptions, the volume and the networking) do not come
from the repository; change them in the template editor and keep `railway.ts`
and this file in step.
