# Railway

This file describes the Campfire project: one Minecraft service and a volume at `/server/data`.

If you already have a Railway project linked:

```bash
railway link
railway config plan
railway config apply
```

Then add a TCP proxy on port `25565` and set `ADMIN_KEY` plus `MC_PUBLIC_ADDRESS` in the service variables.
