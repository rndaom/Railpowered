# Railway one-click

A GitHub URL is not a one-click deploy. `railway.com/new?template=https://github.com/...`
opens the New project chooser. The working button is a **published Railway template**:

`https://railway.com/new/template/<code>`

That template is what attaches the volume, TCP 25565, HTTP URL, and a generated
`ADMIN_KEY`. End users then click Deploy once.

## Publish the template once

You need a Railway account that can see this GitHub repo (public, or private with
Railway’s GitHub access).

1. In Railway, create a project from this repo (or `railway link` and
   `railway config apply`).
2. Confirm the Minecraft service has a volume on `/server/data`, a TCP proxy on
   `25565`, and a public HTTP domain. Generate the domain if it is missing.
3. Project settings → **Generate Template from Project** → Create Template.
4. Copy the template URL and put it on the Deploy button in `README.md`.

Do not point the button at a GitHub URL. Railway will not read this folder from
that link.
