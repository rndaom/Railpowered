# Railway one-click

The Deploy button uses this published-ready template:

`https://railway.com/new/template/AGX0Mu`

A GitHub URL is not a one-click deploy. `railway.com/new?template=https://github.com/...`
opens the New project chooser.

The template attaches the volume at `/server/data`, a TCP proxy on `25565`, and a
public HTTP domain on port `8080`. `PORT` defaults to `8080` so Railway’s health
check hits the dashboard instead of Minecraft. `ADMIN_KEY` is required on the
Deploy form so the owner chooses the dashboard password. Railway does not
generate it.

The repo is still private. Deployers need Railway’s GitHub access to
`rndaom/Railpowered`. The template tracks the repository default branch.

## Recreate the template

You need a Railway account that can see this GitHub repo.

1. Create or link a seed project (`railway init` / `railway link`).
2. Confirm the Minecraft service has a volume on `/server/data`, a TCP proxy on
   `25565`, and a public HTTP domain.
3. Project settings → **Generate Template from Project**.
4. In the template editor, set `ADMIN_KEY` as required with an empty default and
   a short description. Do not use `${{secret()}}`. Set `PORT` to `8080`.
5. Put the new `/new/template/<code>` URL on the Deploy button in `README.md`.
