# Beta 1.7.3 Plugin Development Reference

Quick reference for what's possible (and impossible) when making plugins
for a Minecraft Beta 1.7.3 server running Project Poseidon.

---

## Hard Limits: What CANNOT Be Done

These are client-side hardcoded behaviors. The vanilla Beta 1.7.3 client
has no code to handle these, so no server plugin can make them work.

### Visual / UI
- **No scoreboards** — packet doesn't exist (added 1.5)
- **No boss bars** — packet doesn't exist (added 1.9 for custom bars)
- **No titles/subtitles/action bar text** — packets don't exist (added 1.8)
- **No tab list header/footer** — packet doesn't exist (added 1.8)
- **No clickable/hoverable chat** — no JSON chat, plain text only (added 1.7.2)
- **No bold/italic/underline/strikethrough text** — only 16 color codes work (formatting added ~1.0)
- **No custom textures or resource packs** — textures are baked into the client JAR
- **No custom block models/shapes** — hardcoded in client
- **No custom particles** — no particle packet; only hardcoded effects via EffectPlay
- **No custom sounds** — no named sound packet; only hardcoded effect IDs
- **No world border visual** — doesn't exist (added 1.8)
- **No server icon in server list** — not supported

### Gameplay Systems That Don't Exist
- **No hunger/food bar** — food heals directly, instantly, and doesn't stack (except cookies)
- **No sprinting** — no sprint key, only walking speed (added Beta 1.8)
- **No experience/XP/levels** — no XP bar, no XP orbs (added Beta 1.8)
- **No enchanting** — no enchantment table, no enchanted items (added 1.0)
- **No potions/brewing** — no potion effects at all, no PotionEffect API (added 1.0)
- **No creative mode** — survival only (added Beta 1.8)
- **No villager trading** — no villagers exist (added 1.0)
- **No animal breeding** — animals can't breed (added 1.0)
- **No The End** — only Overworld and Nether dimensions

### Items & Inventory
- **No custom item names** — items can't have display names (anvils added 1.4.2)
- **No item lore/description** — no NBT data on items at all
- **No enchantment glint** — can't make items glow
- **No custom NBT tags on items** — item format is just: ID + count + damage value
- **No item attributes** — no attack damage/speed bonuses (added 1.6)
- **No spawn eggs** — don't exist (added 1.1)
- **No writable books** — Book & Quill doesn't exist (added 1.3.1)

### Mining & Tools
- **Mining speed is client-driven** — the client calculates break time from its own
  hardcoded tool/block tables and sends "done mining" when finished. The server can
  only validate timing (anti-cheat) or force-break blocks early as a workaround.
  The BetaFix plugin demonstrates this workaround with a server-side timer.
- **Tool effectiveness is hardcoded in client** — adding blocks to a tool's effective
  list server-side doesn't change client mining speed

### Entities & Mobs
- **No custom entity types** — client only renders the ~15 mobs it knows about
- **No entity custom names** — name tags don't exist (added 1.5/1.6)
- **No entity invisibility** — metadata flag doesn't exist
- **No entity glowing** — doesn't exist (added 1.9)
- **Cannot disguise entities** — no way to make a pig render as a zombie etc.
- **Only these mobs exist:** Creeper, Skeleton, Spider, Giant, Zombie, Slime,
  Ghast, Zombie Pigman, Pig, Sheep, Cow, Chicken, Squid, Wolf
- **No Endermen, Cave Spiders, Silverfish, Blaze, Iron Golem, Ocelot, Witch,
  Bat, Horse, or anything from 1.0+**

### World
- **Build height: 0-127** (128 blocks, not 256)
- **Block IDs: 0-255 only** (~97 used), 4-bit data values (0-15)
- **Only 6 tile entity types:** Sign, Chest, Furnace, Dispenser, Mob Spawner, Note Block
- **Only 4 GUI window types:** Chest, Crafting Table, Furnace, Dispenser
- **Cannot create custom GUIs** — only the 4 window types above exist

### Networking
- **No plugin channels** — no Plugin Message packet, no custom server-client data (added 1.3)
- **No encryption** — all traffic is plaintext
- **No compression** — all packets uncompressed
- **Chat input limit: ~100 characters** (server-to-client can be longer)

---

## What Poseidon Adds (Beyond Vanilla CraftBukkit CB1060)

### Custom Events (Plugin-Usable)
| Event | Description |
|---|---|
| `PlayerDeathEvent` | Death message customization, keepInventory flag |
| `PlayerReceivePacketEvent` | Intercept packets FROM player (cancellable) |
| `PlayerSendPacketEvent` | Intercept packets TO player (cancellable) |
| `PlayerConnectionInitializationEvent` | Pre-auth connection event |
| `PlayerItemDamageEvent` | Item durability damage (cancellable, adjustable) |
| `ChestOpenedEvent` | Player opens chest (cancellable) |
| `InventoryTransactionEvent` | Item added/removed from inventory (cancellable) |
| `ItemSpawnEvent` | Item entity spawns in world |
| `ItemDespawnEvent` | Item entity despawns |
| `ProjectileHitEvent` | Projectile hits something |
| `ExplosionPrimeEvent` | Explosion about to happen (cancellable, adjustable radius) |

**Note:** Packet events require `settings.packet-events.enabled: true` in poseidon.yml
(disabled by default for performance).

### Player API Additions
- `player.getUniqueId()` — UUID support (not in CB1060)
- `player.hidePlayer(other)` / `showPlayer(other)` / `canSee(other)` — vanish system
- `player.sendPacket(player, packet)` — direct NMS packet sending
- `player.getConnectionType()` — detect proxy connections (BungeeCord, Release2Beta)

### Server API
- `server.getPlayer(UUID)` — lookup by UUID
- `server.getPoseidonVersion()` — version string
- `Poseidon.getTpsRecords()` — TPS history
- `ArtificialPacket53BlockChange` — fake block display packets

### UUID System
- Full Mojang UUID support with cache
- `PoseidonUUID` utility class for UUID lookups
- Offline UUID fallback for cracked players

### Built-in Fixes (Already Handled by Poseidon)
- Duplication glitches (piston exploits, sand/gravel duping)
- Crash exploits
- Drowning push-down fix
- Player knockback fix
- Skeleton shooting sound fix
- Lava flow fix
- Speed hack detection
- Modern fence bounding boxes (optional)
- Leaf block replacement protection
- Mob spawner entity limits

### Poseidon Configuration (poseidon.yml)
- Customizable join/leave/kick/ban messages
- Daily log rotation
- Tree growth block protection
- Spawn randomization
- Safe teleport on join
- Packet spam detection

---

## What CAN Be Done (Server-Side Capabilities)

### Blocks & World
- Place/remove any of the ~97 block types
- Send fake blocks to specific players (BlockSet packet)
- Modify sign text (4 lines x 15 chars, with color codes)
- Open/modify chest, furnace, dispenser, crafting table inventories
- Control mob spawner entity type and delay
- Force-break blocks server-side (set to air + drop items)
- Send explosion effects (visual + knockback)
- Trigger hardcoded sound/particle effects (EffectPlay packet)

### Players
- Teleport players
- Send colored chat messages (16 colors: 0-9, a-f)
- Heal/damage players (UpdateHealth packet)
- Track player positions (client sends position every tick)
- Modify player inventory contents
- Set player equipment (visible to others)
- Vanish players (Poseidon API)

### Entities
- Spawn/despawn the ~15 available mob types
- Set entity velocity (knockback, launch)
- Ride entities (attach player to entity)
- Modify entity metadata (on-fire, crouching, mob-specific flags)

### Items
- Give items (by numeric ID + damage value)
- Modify inventory slots
- Detect held item changes
- Track item durability (Poseidon's PlayerItemDamageEvent)

### Commands & Chat
- Register custom commands
- Intercept and modify chat messages
- Color chat text (16 colors)
- Send server messages to all/specific players

### Scheduling
- Bukkit scheduler for delayed/repeating tasks
- Async tasks for non-blocking operations

### Networking (Poseidon-Specific)
- Intercept and modify/cancel any packet (with packet events enabled)
- Send raw NMS packets to players
- Detect player connection type (direct, proxy)

### Maps
- Maps exist (added Beta 1.6) — server can send pixel data
- Could potentially render custom images on maps

---

## Available Blocks Quick Reference (IDs 0-96)

```
  0 Air             17 Log              44 Slab            72 Wood Press. Plate
  1 Stone            18 Leaves           45 Brick Block     73 Redstone Ore
  2 Grass            19 Sponge           46 TNT             74 Redstone Ore (lit)
  3 Dirt             20 Glass            47 Bookshelf       75 RS Torch (off)
  4 Cobblestone      21 Lapis Ore        48 Mossy Cobble    76 RS Torch (on)
  5 Planks           22 Lapis Block      49 Obsidian        77 Stone Button
  6 Sapling          23 Dispenser        50 Torch           78 Snow Layer
  7 Bedrock          24 Sandstone        51 Fire            79 Ice
  8 Water (flowing)  25 Note Block       52 Mob Spawner     80 Snow Block
  9 Water (still)    26 Bed              53 Wood Stairs     81 Cactus
 10 Lava (flowing)   27 Powered Rail     54 Chest           82 Clay
 11 Lava (still)     28 Detector Rail    55 Redstone Wire   83 Sugar Cane
 12 Sand             29 Sticky Piston    56 Diamond Ore     84 Jukebox
 13 Gravel           30 Cobweb           57 Diamond Block   85 Fence
 14 Gold Ore         31 Tall Grass       58 Crafting Table  86 Pumpkin
 15 Iron Ore         32 Dead Bush        59 Crops           87 Netherrack
 16 Coal Ore         33 Piston           60 Farmland        88 Soul Sand
                     34 Piston Extension 61 Furnace         89 Glowstone
                     35 Wool (16 colors) 62 Furnace (lit)   90 Portal
                     36 Piston Moving    63 Sign Post       91 Jack-o-Lantern
                     37 Dandelion        64 Wood Door       92 Cake
                     38 Rose             65 Ladder          93 Repeater (off)
                     39 Brown Mushroom   66 Rail            94 Repeater (on)
                     40 Red Mushroom     67 Cobble Stairs   95 Locked Chest
                     41 Gold Block       68 Wall Sign       96 Trapdoor
                     42 Iron Block       69 Lever
                     43 Double Slab      70 Stone Press. Plate
```

---

## Available Mobs Quick Reference

**Hostile:** Creeper (50), Skeleton (51), Spider (52), Giant (53),
Zombie (54), Slime (55), Ghast (56), Zombie Pigman (57)

**Passive:** Pig (90), Sheep (91), Cow (92), Chicken (93), Squid (94)

**Tameable:** Wolf (95)

**Objects:** Boat, Minecart, Arrow, Snowball, Egg, Primed TNT,
Falling Sand/Gravel, Fishing Bobber, Painting, Lightning Bolt
