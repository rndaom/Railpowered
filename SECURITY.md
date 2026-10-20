# Security

## Reporting a vulnerability

If you find a problem that could let someone into a dashboard, a server or a
world they should not reach, please report it privately through
[GitHub Security Advisories](https://github.com/rndaom/Railpowered/security/advisories/new).
Please do not open a public issue for it.

Helpful details:

- what an attacker can do, and what they need first (network access only, a
  player account, the dashboard key, ...);
- steps or a request that reproduces it;
- the Railpowered version or commit, and the server type and Minecraft
  version if they matter.

Railpowered is a small project maintained in spare time. You can expect
an acknowledgement within a week and a fix or a plan within 30 days for
confirmed issues. Once a fix is on `main`, the advisory is published with
credit to you, unless you prefer otherwise.

## Supported versions

Only the latest code on `main` is supported. Railway deployments of the
template follow `main`, so fixes reach servers when their owners apply the
update Railway offers (or redeploy).

## Scope

In scope: the dashboard and its API, login and sessions, the sleep proxy,
`start.sh` and the Docker image, and anything that lets a player or a
stranger reach data on the volume.

Out of scope: vulnerabilities in Minecraft itself, in Fabric, Forge or mods,
in Railway's platform, and attacks that need the dashboard key (the key
grants full control by design).

## Keeping your server safe

The dashboard is on a public Railway URL, and the join address is easy to
share and to scan for, so a few habits matter:

- **Use a long `ADMIN_KEY`.** Four or more random words, or 20+ random
  characters. Anyone with the key controls your server, including uploading
  modpacks that run code. Failed sign-ins are rate limited, but a short or
  reused key is still the weakest point.
- **Know where a generated key appears.** If `ADMIN_KEY` is not set,
  Railpowered generates one, saves it on the volume and prints it in the
  Railway deploy logs, which everyone in your Railway project can read. Set
  your own `ADMIN_KEY` in the service's Variables to replace it.
- **Changing `ADMIN_KEY` signs everyone out.** Do that if you think the key has
  leaked, then redeploy.
- **Turn on the whitelist for a private server.** Without it, anyone with a
  Minecraft account who finds the address can join (and wake the server).
  Add your friends and enable the whitelist from the dashboard's Players page.
- **Keep online mode on.** With `online-mode=false`, players are not checked
  against Mojang accounts and anyone can join under any name, including an
  operator's. Only turn it off for old versions that need it, and only with
  the whitelist on.
- **Only install mods and modpacks you trust.** They run inside the server
  with access to everything on the volume, including worlds and backups.
  Railpowered runs Minecraft as an unprivileged user and keeps the dashboard
  key out of the server's environment, but a malicious mod can still damage
  your worlds.
- **Download backups you care about.** Backups live on the same Railway volume
  as the worlds, so deleting the volume or the service deletes them too.
- **Keep Railway variables and logs private.** Don't share screenshots of the
  Variables page or the dashboard key.
