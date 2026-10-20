## What this changes

<!-- One or two sentences, and the issue it closes if there is one (Closes #123). -->

## Why

<!-- The problem a server owner or player hits today. -->

## Type of change

- [ ] Bug fix
- [ ] New feature or behavior
- [ ] Dashboard or copy change
- [ ] Docs, CI or tooling

## How I tested it

<!-- What you ran and what you saw. For dashboard changes, add before and after screenshots. -->

- [ ] `python -m unittest discover -s tests` passes
- [ ] `shellcheck start.sh` passes (if `start.sh` changed)
- [ ] `docker build .` succeeds (if the `Dockerfile`, `start.sh` or the file layout changed)
- [ ] Tried it on a real or local server: <!-- version and server type -->

## Checklist

- [ ] Existing worlds, backups and settings on a deployed volume keep working after this change.
- [ ] No new third-party Python packages (Railpowered uses the standard library only).
- [ ] Any new file the image needs is in the `Dockerfile`, `.dockerignore` and `railway.toml` watch paths.
- [ ] Docs updated where behavior changed (README, CONTRIBUTING, `.railway/README.md`).
- [ ] No keys, worlds, jars or personal data in the diff.
