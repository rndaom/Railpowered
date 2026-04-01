package com.betaserver.webmap;

import org.bukkit.Chunk;
import org.bukkit.Location;
import org.bukkit.World;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.world.ChunkLoadEvent;
import org.bukkit.plugin.java.JavaPlugin;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.FileWriter;
import java.io.IOException;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.logging.Logger;

public class WebMapPlugin extends JavaPlugin implements Listener {

    private static final Logger log = Logger.getLogger("Minecraft.WebMap");
    private static final int MAGIC = 0x574D4150; // "WMAP"
    private static final int FORMAT_VERSION = 6;

    // Chunk key -> data array:
    //   [0..255]     = surface heights
    //   [256..511]   = surface block IDs
    //   [512..767]   = surface base heights
    //   [768..1023]  = middle heights  (e.g. trunk below leaves)
    //   [1024..1279] = middle block IDs
    //   [1280..1535] = middle base heights
    //   [1536..1791] = ground heights  (terrain below canopy/trunk, 0 if none)
    //   [1792..2047] = ground block IDs
    //   [2048..2303] = ground base heights
    private final Map<Long, int[]> chunkData = new ConcurrentHashMap<Long, int[]>();
    private final ConcurrentLinkedQueue<long[]> processQueue = new ConcurrentLinkedQueue<long[]>();

    private File webmapDir;
    private volatile boolean dirty = false;
    private volatile boolean saving = false;
    private int processTaskId = -1;
    private int saveTaskId = -1;
    private int playersTaskId = -1;

    // NMS reflection for fast block access (avoids creating Block objects)
    private Method nmsGetHandle;
    private Field nmsBlocksField;
    private boolean nmsFailed = false;

    // Blocks to skip when scanning for the surface (decorations/transparent)
    private static final boolean[] SKIP_BLOCK = new boolean[256];
    static {
        for (int id : new int[]{
            0,   // Air
            6,   // Sapling
            27,  // Powered Rail
            28,  // Detector Rail
            31,  // Tall Grass
            32,  // Dead Shrub
            34,  // Piston Extension
            36,  // Block moved by piston
            37,  // Dandelion
            38,  // Rose
            39,  // Brown Mushroom
            40,  // Red Mushroom
            50,  // Torch
            51,  // Fire
            55,  // Redstone Wire
            59,  // Wheat
            63,  // Standing Sign
            65,  // Ladder
            66,  // Rail
            68,  // Wall Sign
            69,  // Lever
            70,  // Stone Pressure Plate
            72,  // Wooden Pressure Plate
            75,  // Redstone Torch (off)
            76,  // Redstone Torch (on)
            77,  // Stone Button
            83,  // Sugar Cane
            90,  // Portal
            93,  // Repeater (off)
            94   // Repeater (on)
        }) {
            SKIP_BLOCK[id] = true;
        }
    }

    private static boolean isVegetation(int typeId) {
        return typeId == 17 || typeId == 18;
    }

    @Override
    public void onEnable() {
        webmapDir = new File(getDataFolder().getParentFile().getParentFile(), "webmap");
        if (!webmapDir.exists()) {
            webmapDir.mkdirs();
        }

        loadMapData();

        getServer().getPluginManager().registerEvents(this, this);

        // Queue all world chunks by scanning region files, so unloaded
        // chunks (previously explored areas) are also mapped
        if (!getServer().getWorlds().isEmpty()) {
            World world = getServer().getWorlds().get(0);
            scanWorldChunks(world);
        }

        // Process queued chunks (fast NMS path: 4/tick, slow fallback: 1/tick)
        processTaskId = getServer().getScheduler().scheduleSyncRepeatingTask(this,
            new Runnable() { public void run() { processChunks(); } }, 40L, 2L);

        // Save map data every 10 seconds
        saveTaskId = getServer().getScheduler().scheduleSyncRepeatingTask(this,
            new Runnable() { public void run() { saveMapData(); } }, 200L, 200L);

        // Save player positions every 2 seconds
        playersTaskId = getServer().getScheduler().scheduleSyncRepeatingTask(this,
            new Runnable() { public void run() { savePlayerData(); } }, 40L, 40L);

        log.info("[WebMap] Enabled — " + chunkData.size() + " chunks loaded, mapping active");
    }

    @Override
    public void onDisable() {
        if (processTaskId != -1) getServer().getScheduler().cancelTask(processTaskId);
        if (saveTaskId != -1) getServer().getScheduler().cancelTask(saveTaskId);
        if (playersTaskId != -1) getServer().getScheduler().cancelTask(playersTaskId);

        saveMapDataSync();
        savePlayerData();
        log.info("[WebMap] Disabled — saved " + chunkData.size() + " chunks");
    }

    @EventHandler
    public void onChunkLoad(ChunkLoadEvent event) {
        if (getServer().getWorlds().isEmpty()) return;
        if (!event.getWorld().equals(getServer().getWorlds().get(0))) return;
        processQueue.add(new long[]{event.getChunk().getX(), event.getChunk().getZ()});
    }

    private void processChunks() {
        if (getServer().getWorlds().isEmpty()) return;
        World world = getServer().getWorlds().get(0);

        // Fast NMS path can handle more chunks; slow Bukkit API path is rate-limited
        int limit = nmsFailed ? 1 : 4;
        for (int i = 0; i < limit; i++) {
            long[] coords = processQueue.poll();
            if (coords == null) break;
            int cx = (int) coords[0];
            int cz = (int) coords[1];
            boolean wasLoaded = world.isChunkLoaded(cx, cz);
            if (!wasLoaded) {
                // Temporarily load from disk (false = don't generate new chunks)
                if (!world.loadChunk(cx, cz, false)) continue;
            }
            processChunk(world.getChunkAt(cx, cz));
            if (!wasLoaded) {
                world.unloadChunk(cx, cz, false);
            }
        }
    }

    /**
     * Get the raw block byte array from the NMS chunk via reflection.
     * This avoids creating thousands of CraftBlock objects per chunk.
     * Returns null if NMS access is unavailable (falls back to slow path).
     */
    private byte[] getNmsBlocks(Chunk chunk) {
        if (nmsFailed) return null;
        try {
            if (nmsGetHandle == null) {
                nmsGetHandle = chunk.getClass().getMethod("getHandle");
            }
            Object nmsChunk = nmsGetHandle.invoke(chunk);

            if (nmsBlocksField == null) {
                for (Field f : nmsChunk.getClass().getDeclaredFields()) {
                    if (f.getType() == byte[].class) {
                        f.setAccessible(true);
                        byte[] arr = (byte[]) f.get(nmsChunk);
                        if (arr != null && arr.length == 32768) {
                            nmsBlocksField = f;
                            log.info("[WebMap] Fast NMS block access enabled");
                            return arr;
                        }
                    }
                }
                nmsFailed = true;
                log.warning("[WebMap] NMS block field not found — using slow method");
                return null;
            }

            return (byte[]) nmsBlocksField.get(nmsChunk);
        } catch (Exception e) {
            nmsFailed = true;
            log.warning("[WebMap] NMS access failed: " + e.getMessage());
            return null;
        }
    }

    private void processChunk(Chunk chunk) {
        int[] data = new int[2304];
        byte[] blocks = getNmsBlocks(chunk);

        for (int x = 0; x < 16; x++) {
            for (int z = 0; z < 16; z++) {
                int idx = x * 16 + z;
                int surfaceY = -1;
                // NMS block index: x*2048 + z*128 + y
                int nmsBase = (blocks != null) ? x * 2048 + z * 128 : 0;

                // Surface scan (top-down)
                for (int y = 127; y >= 0; y--) {
                    int typeId = (blocks != null)
                        ? (blocks[nmsBase + y] & 0xFF)
                        : chunk.getBlock(x, y, z).getTypeId();
                    if (typeId > 0 && typeId < 256 && !SKIP_BLOCK[typeId]) {
                        surfaceY = y;
                        data[idx] = y;
                        data[256 + idx] = typeId;
                        break;
                    }
                }

                if (surfaceY < 0) continue;

                // Base scan — find where the continuous solid section starts
                int surfaceType = data[256 + idx];
                int base = surfaceY;
                for (int y = surfaceY - 1; y >= 0; y--) {
                    int typeId = (blocks != null)
                        ? (blocks[nmsBase + y] & 0xFF)
                        : chunk.getBlock(x, y, z).getTypeId();
                    if (typeId > 0 && typeId < 256 && !SKIP_BLOCK[typeId]) {
                        // Stop at vegetation type transitions so tree canopies
                        // and trunks separate from terrain below
                        if ((surfaceType == 17 || surfaceType == 18)
                                && typeId != surfaceType) break;
                        base = y;
                    } else {
                        break;
                    }
                }
                data[512 + idx] = base;

                if (base > 0) {
                    int middleY = -1;
                    int middleType = 0;
                    for (int y = base - 1; y >= 0; y--) {
                        int typeId = (blocks != null)
                            ? (blocks[nmsBase + y] & 0xFF)
                            : chunk.getBlock(x, y, z).getTypeId();
                        if (typeId > 0 && typeId < 256 && !SKIP_BLOCK[typeId]) {
                            middleY = y;
                            middleType = typeId;
                            data[768 + idx] = y;
                            data[1024 + idx] = typeId;
                            break;
                        }
                    }

                    int middleBase = -1;
                    if (middleY >= 0) {
                        middleBase = middleY;
                        for (int y = middleY - 1; y >= 0; y--) {
                            int typeId = (blocks != null)
                                ? (blocks[nmsBase + y] & 0xFF)
                                : chunk.getBlock(x, y, z).getTypeId();
                            if (typeId > 0 && typeId < 256 && !SKIP_BLOCK[typeId]) {
                                if (isVegetation(middleType) && typeId != middleType) break;
                                middleBase = y;
                            } else {
                                break;
                            }
                        }
                        data[1280 + idx] = middleBase;
                    }

                    if (middleBase > 0) {
                        boolean skipSameVegetation = isVegetation(middleType);
                        for (int y = middleBase - 1; y >= 0; y--) {
                            int typeId = (blocks != null)
                                ? (blocks[nmsBase + y] & 0xFF)
                                : chunk.getBlock(x, y, z).getTypeId();
                            if (skipSameVegetation && isVegetation(typeId)) {
                                continue;
                            }
                            if (typeId > 0 && typeId < 256 && !SKIP_BLOCK[typeId]) {
                                data[1536 + idx] = y;
                                data[1792 + idx] = typeId;

                                int groundType = typeId;
                                int gBase = y;
                                for (int gy = y - 1; gy >= 0; gy--) {
                                    int gTypeId = (blocks != null)
                                        ? (blocks[nmsBase + gy] & 0xFF)
                                        : chunk.getBlock(x, gy, z).getTypeId();
                                    if (gTypeId > 0 && gTypeId < 256
                                            && !SKIP_BLOCK[gTypeId]) {
                                        if (isVegetation(groundType) && gTypeId != groundType) break;
                                        gBase = gy;
                                    } else {
                                        break;
                                    }
                                }
                                data[2048 + idx] = gBase;
                                break;
                            }
                        }
                    }
                }
            }
        }

        chunkData.put(chunkKey(chunk.getX(), chunk.getZ()), data);
        dirty = true;
    }

    // --- Persistence ---

    private void saveMapData() {
        if (!dirty || saving) return;
        dirty = false;
        saving = true;

        final HashMap<Long, int[]> snapshot = new HashMap<Long, int[]>(chunkData);
        final Location spawn = getServer().getWorlds().get(0).getSpawnLocation();
        final int sx = spawn.getBlockX(), sy = spawn.getBlockY(), sz = spawn.getBlockZ();

        new Thread(new Runnable() {
            public void run() {
                try {
                    writeMapFile(snapshot, sx, sy, sz);
                } finally {
                    saving = false;
                }
            }
        }, "WebMap-Save").start();
    }

    private void saveMapDataSync() {
        if (chunkData.isEmpty() || getServer().getWorlds().isEmpty()) return;
        Location spawn = getServer().getWorlds().get(0).getSpawnLocation();
        writeMapFile(new HashMap<Long, int[]>(chunkData),
            spawn.getBlockX(), spawn.getBlockY(), spawn.getBlockZ());
    }

    private void writeMapFile(Map<Long, int[]> data, int sx, int sy, int sz) {
        File tmpFile = new File(webmapDir, "mapdata.bin.tmp");
        File target = new File(webmapDir, "mapdata.bin");

        try {
            DataOutputStream dos = new DataOutputStream(
                new BufferedOutputStream(new FileOutputStream(tmpFile), 65536));

            dos.writeInt(MAGIC);
            dos.writeInt(FORMAT_VERSION);
            dos.writeInt(sx);
            dos.writeInt(sy);
            dos.writeInt(sz);
            dos.writeInt(data.size());

            for (Map.Entry<Long, int[]> entry : data.entrySet()) {
                long key = entry.getKey();
                int[] d = entry.getValue();

                dos.writeInt((int)(key >> 32));  // chunk X
                dos.writeInt((int) key);          // chunk Z

                for (int i = 0; i < 256; i++) dos.writeByte(d[i]);
                for (int i = 0; i < 256; i++) dos.writeByte(d[256 + i]);
                for (int i = 0; i < 256; i++) dos.writeByte(d[512 + i]);
                for (int i = 0; i < 256; i++) dos.writeByte(d[768 + i]);
                for (int i = 0; i < 256; i++) dos.writeByte(d[1024 + i]);
                for (int i = 0; i < 256; i++) dos.writeByte(d[1280 + i]);
                for (int i = 0; i < 256; i++) dos.writeByte(d[1536 + i]);
                for (int i = 0; i < 256; i++) dos.writeByte(d[1792 + i]);
                for (int i = 0; i < 256; i++) dos.writeByte(d[2048 + i]);
            }

            dos.close();
            if (target.exists()) target.delete();
            tmpFile.renameTo(target);

        } catch (IOException e) {
            log.warning("[WebMap] Failed to write map data: " + e.getMessage());
        }
    }

    private void loadMapData() {
        File file = new File(webmapDir, "mapdata.bin");
        if (!file.exists()) return;

        try {
            DataInputStream dis = new DataInputStream(
                new BufferedInputStream(new FileInputStream(file)));

            int magic = dis.readInt();
            if (magic != MAGIC) {
                dis.close();
                log.warning("[WebMap] Invalid map data file — will regenerate");
                return;
            }

            int version = dis.readInt();
            if (version < 6) {
                dis.close();
                file.delete();
                log.warning("[WebMap] Map data version too old — deleted, will regenerate");
                return;
            }

            dis.readInt(); // spawn X (read fresh from world)
            dis.readInt(); // spawn Y
            dis.readInt(); // spawn Z
            int chunkCount = dis.readInt();

            for (int c = 0; c < chunkCount; c++) {
                int cx = dis.readInt();
                int cz = dis.readInt();
                int[] data = new int[2304];

                for (int i = 0; i < 256; i++) data[i] = dis.readByte() & 0xFF;
                for (int i = 0; i < 256; i++) data[256 + i] = dis.readByte() & 0xFF;
                for (int i = 0; i < 256; i++) data[512 + i] = dis.readByte() & 0xFF;
                for (int i = 0; i < 256; i++) data[768 + i] = dis.readByte() & 0xFF;
                for (int i = 0; i < 256; i++) data[1024 + i] = dis.readByte() & 0xFF;
                for (int i = 0; i < 256; i++) data[1280 + i] = dis.readByte() & 0xFF;
                for (int i = 0; i < 256; i++) data[1536 + i] = dis.readByte() & 0xFF;
                for (int i = 0; i < 256; i++) data[1792 + i] = dis.readByte() & 0xFF;
                for (int i = 0; i < 256; i++) data[2048 + i] = dis.readByte() & 0xFF;

                chunkData.put(chunkKey(cx, cz), data);
            }

            dis.close();
            log.info("[WebMap] Loaded " + chunkCount + " chunks from disk");

        } catch (IOException e) {
            log.warning("[WebMap] Failed to load map data: " + e.getMessage());
        }
    }

    private void savePlayerData() {
        StringBuilder sb = new StringBuilder("[");
        boolean first = true;
        for (Player player : getServer().getOnlinePlayers()) {
            if (!first) sb.append(",");
            first = false;
            Location loc = player.getLocation();
            sb.append("{\"name\":\"").append(escapeJson(player.getName()))
              .append("\",\"x\":").append(String.format("%.1f", loc.getX()))
              .append(",\"y\":").append(String.format("%.1f", loc.getY()))
              .append(",\"z\":").append(String.format("%.1f", loc.getZ()))
              .append("}");
        }
        sb.append("]");

        try {
            File tmpFile = new File(webmapDir, "players.json.tmp");
            File target = new File(webmapDir, "players.json");
            FileWriter fw = new FileWriter(tmpFile);
            fw.write(sb.toString());
            fw.close();
            if (target.exists()) target.delete();
            tmpFile.renameTo(target);
        } catch (IOException e) {
            // Silently ignore — player data is ephemeral
        }
    }

    // --- World scanning ---

    /**
     * Read McRegion file headers to find ALL existing chunk coordinates
     * in the world, and queue any that aren't already in chunkData.
     */
    private void scanWorldChunks(World world) {
        // Also queue all currently loaded chunks (always re-scan for logic changes)
        for (Chunk chunk : world.getLoadedChunks()) {
            processQueue.add(new long[]{chunk.getX(), chunk.getZ()});
        }

        File worldDir = new File(world.getName());
        File regionDir = new File(worldDir, "region");
        if (!regionDir.isDirectory()) {
            log.info("[WebMap] No region directory found — only mapping loaded chunks");
            return;
        }

        File[] regionFiles = regionDir.listFiles();
        if (regionFiles == null) return;

        int queued = 0;
        for (File regionFile : regionFiles) {
            String name = regionFile.getName();
            if (!name.endsWith(".mcr") && !name.endsWith(".mca")) continue;
            if (!name.startsWith("r.")) continue;

            String[] parts = name.split("\\.");
            if (parts.length != 4) continue;
            int regionX, regionZ;
            try {
                regionX = Integer.parseInt(parts[1]);
                regionZ = Integer.parseInt(parts[2]);
            } catch (NumberFormatException e) {
                continue;
            }

            // Read 4KB header — each 4-byte entry's first 3 bytes are the
            // sector offset; if non-zero the chunk exists on disk
            try {
                FileInputStream fis = new FileInputStream(regionFile);
                byte[] header = new byte[4096];
                int read = fis.read(header);
                fis.close();
                if (read < 4096) continue;

                for (int lz = 0; lz < 32; lz++) {
                    for (int lx = 0; lx < 32; lx++) {
                        int off = 4 * (lx + lz * 32);
                        int sectorOffset = ((header[off] & 0xFF) << 16)
                                         | ((header[off + 1] & 0xFF) << 8)
                                         | (header[off + 2] & 0xFF);
                        if (sectorOffset != 0) {
                            int cx = regionX * 32 + lx;
                            int cz = regionZ * 32 + lz;
                            if (!chunkData.containsKey(chunkKey(cx, cz))) {
                                processQueue.add(new long[]{cx, cz});
                                queued++;
                            }
                        }
                    }
                }
            } catch (IOException e) {
                log.warning("[WebMap] Failed to read region: " + name);
            }
        }

        log.info("[WebMap] Queued " + queued + " unscanned chunks from " +
                 regionFiles.length + " region files");
    }

    // --- Utilities ---

    private static long chunkKey(int cx, int cz) {
        return ((long) cx << 32) | (cz & 0xFFFFFFFFL);
    }

    private static String escapeJson(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"");
    }
}
