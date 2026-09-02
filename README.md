<p align="center">
  <img src="docs/assets/logo.svg" width="72" alt="Campfire">
</p>

<h1 align="center">Campfire</h1>

<p align="center">
  A Minecraft server that starts when your friends join.<br>
  One click on Railway. Latest vanilla. Sleeps when idle.
</p>

<p align="center">
  <a href="https://railway.com/new?template=https://github.com/rndaom/campfire">
    <img src="https://railway.com/button.svg" alt="Deploy on Railway">
  </a>
</p>

## After you deploy

Do these five things once in Railway, then open the web URL. That URL is this dashboard.

<p align="center">
  <img src="docs/assets/login.jpg" alt="Campfire dashboard login" width="920">
</p>

<p align="center">
  <img src="docs/assets/after-deploy.jpg" alt="After deploy steps in the Campfire dashboard" width="920">
</p>

1. Add a volume at `/server/data`
2. Add a TCP proxy on port `25565` and copy that host:port
3. Set `ADMIN_KEY` to a password you will remember
4. Set `MC_PUBLIC_ADDRESS` to the TCP address
5. Open the Railway web URL and log in with `ADMIN_KEY`

## The dashboard

Latest vanilla is already selected. Press Start when someone wants to play. The server sleeps after ten minutes empty.

<p align="center">
  <img src="docs/assets/dashboard.jpg" alt="Campfire dashboard" width="920">
</p>

From here you can change version, add Fabric or Forge, keep worlds, and make backups.

<p align="center">
  <img src="docs/assets/version.jpg" alt="Version and worlds on the Campfire dashboard" width="920">
</p>

Friends join with the official Minecraft launcher and the same version shown at the top of the dashboard.

## Something wrong?

[Open an issue](https://github.com/rndaom/campfire/issues/new/choose).

## License

MIT. Minecraft belongs to Mojang. This project downloads the official server when it starts.
