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
  modpacks that run code. Failed sign-ins are limited per address, and a
  flood of them from many addresses puts every sign-in into a short, slow
  queue. While such a flood lasts, new sign-ins (yours too) may be refused
  with "Too many attempts"; browsers that are already signed in keep working,
  because some connections are kept for them. A short or reused key is still
  the weakest point, so Railpowered refuses a key that is shorter than 12
  characters or a well-known password (and uses the key saved on the volume
  instead) unless you set `ALLOW_WEAK_ADMIN_KEY=1`.
- **Add a second factor if you like.** Set `TOTP_SECRET` to a base32 secret
  that you also add to an authenticator app, and signing in needs the app's
  6-digit code as well as the key. A malformed secret refuses every sign-in
  instead of silently turning the second factor off; fix or remove the
  variable to get back in.
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
  operator's. Setups on Minecraft 1.6.4 and older always run with online mode
  off, because Mojang's old login servers are gone; Railpowered does that for
  you and the dashboard hides the switch. Turn the whitelist on for those
  setups and keep the operator list short.
- **Only install mods and modpacks you trust.** Mods and modpacks run with the
  same permissions as the dashboard. A malicious one can read the dashboard
  key (from the manager process or the files on the volume), sign in to the
  dashboard, and change or delete worlds and backups. Railpowered runs
  Minecraft as an unprivileged user and leaves the key out of the server's
  environment, but that does not separate the two.
- **Sign out devices you no longer use.** Logging out ends that browser's
  session for good, also across restarts. "Sign out everywhere" in the
  dashboard ends every session at once; changing `ADMIN_KEY` does too.
- **Download backups you care about.** Backups live on the same Railway volume
  as the worlds, so deleting the volume or the service deletes them too.
  "Upload backup" on the Backups page brings a downloaded one back. Only upload
  backups you made or trust: a world can carry command blocks and data packs.
- **Keep Railway variables and logs private.** Don't share screenshots of the
  Variables page or the dashboard key.
