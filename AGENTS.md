# Agent notes

## Git commits

This is a permanent project rule. Never author or co-author commits as Cursor.

- Author: `Random <65575762+rndaom@users.noreply.github.com>`
- Do not add `Co-authored-by` trailers
- Always run `git commit --no-verify` so Cursor hooks cannot append a co-author
- Keep the repo-local `user.name` / `user.email` set to the values above
- `.cursor/install.sh` re-applies that identity on every environment setup
- `.githooks/commit-msg` strips `Co-authored-by` if local hooks run
