package com.betaserver.webmap;

import org.bukkit.Chunk;
import org.bukkit.Location;
import org.bukkit.World;
import org.bukkit.entity.Entity;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.block.BlockBreakEvent;
import org.bukkit.event.block.BlockPlaceEvent;
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
import java.io.InputStream;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.logging.Logger;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class WebMapPlugin extends JavaPlugin implements Listener {

    private static final Logger log = Logger.getLogger("Minecraft.WebMap");
    private static final int MAGIC = 0x574D4150; // "WMAP"
    private static final int FORMAT_VERSION = 6;
    private static final int UPDATE_MAGIC = 0x574D5550; // "WMUP"
    private static final int UPDATE_FORMAT_VERSION = 1;
    private static final Pattern TEXTURE_VALUE_PATTERN =
        Pattern.compile("\"value\"\\s*:\\s*\"([^\"]+)\"");
    private static final Pattern SKIN_URL_PATTERN =
        Pattern.compile("\"url\"\\s*:\\s*\"([^\"]+)\"");

    // Chunk key -> data array:
    //   [0..255]     = surface heights
    //   [256..511]   = surface block IDs
    //   [512..767]   = surface base heights
    //   [768..1023]  = middle heights
    //   [1024..1279] = middle block IDs
    //   [1280..1535] = middle base heights
    //   [1536..1791] = ground heights
    //   [1792..2047] = ground block IDs
    //   [2048..2303] = ground base heights
    private final Map<Long, int[]> chunkData = new ConcurrentHashMap<Long, int[]>();
    private final Map<Long, int[]> liveChunkUpdates = new ConcurrentHashMap<Long, int[]>();
    private final ConcurrentLinkedQueue<QueuedChunk> processQueue = new ConcurrentLinkedQueue<QueuedChunk>();
    private final Map<String, String> skinUrlCache = new ConcurrentHashMap<String, String>();
    private final Map<String, Boolean> pendingSkinLookups = new ConcurrentHashMap<String, Boolean>();
    private final Map<String, Long> nextSkinLookupAttempt = new ConcurrentHashMap<String, Long>();

    private File webmapDir;
    private volatile boolean dirty = false;
    private volatile boolean saving = false;
    private volatile boolean liveUpdatesDirty = false;
    private volatile int liveUpdateRevision = 0;
    private int processTaskId = -1;
    private int saveTaskId = -1;
    private int playersTaskId = -1;
    private int worldTaskId = -1;
    private int updatesTaskId = -1;

    // NMS reflection for fast block access (avoids creating Block objects)
    private Method nmsGetHandle;
    private Field nmsBlocksField;
    private boolean nmsFailed = false;

    // Blocks to skip when scanning for the surface (decorations/transparent)
    private static final boolean[] SKIP_BLOCK = new boolean[256];
    static {
        for (int id : new int[]{
            0, 6, 27, 28, 31, 32, 34, 36, 37, 38, 39, 40, 50, 51, 55, 59,
            63, 65, 66, 68, 69, 70, 72, 75, 76, 77, 83, 90, 93, 94
        }) {
            SKIP_BLOCK[id] = true;
        }
    }

    private static final class QueuedChunk {
        private final int cx;
        private final int cz;
        private final boolean publishLive;

        private QueuedChunk(int cx, int cz, boolean publishLive) {
            this.cx = cx;
            this.cz = cz;
            this.publishLive = publishLive;
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

        if (!getServer().getWorlds().isEmpty()) {
            scanWorldChunks(getServer().getWorlds().get(0));
        }

        processTaskId = getServer().getScheduler().scheduleSyncRepeatingTask(this,
            new Runnable() { public void run() { processChunks(); } }, 40L, 2L);

        saveTaskId = getServer().getScheduler().scheduleSyncRepeatingTask(this,
            new Runnable() { public void run() { saveMapData(); } }, 200L, 200L);

        playersTaskId = getServer().getScheduler().scheduleSyncRepeatingTask(this,
            new Runnable() { public void run() { savePlayerData(); } }, 5L, 5L);

        worldTaskId = getServer().getScheduler().scheduleSyncRepeatingTask(this,
            new Runnable() { public void run() { saveWorldState(); } }, 5L, 5L);

        updatesTaskId = getServer().getScheduler().scheduleSyncRepeatingTask(this,
            new Runnable() { public void run() { saveChunkUpdates(); } }, 5L, 5L);

        savePlayerData();
        saveWorldState();
        saveChunkUpdates();
        log.info("[WebMap] Enabled - " + chunkData.size() + " chunks loaded, mapping active");
    }

    @Override
    public void onDisable() {
        if (processTaskId != -1) getServer().getScheduler().cancelTask(processTaskId);
        if (saveTaskId != -1) getServer().getScheduler().cancelTask(saveTaskId);
        if (playersTaskId != -1) getServer().getScheduler().cancelTask(playersTaskId);
        if (worldTaskId != -1) getServer().getScheduler().cancelTask(worldTaskId);
        if (updatesTaskId != -1) getServer().getScheduler().cancelTask(updatesTaskId);

        saveMapDataSync();
        savePlayerData();
        saveWorldState();
        saveChunkUpdatesSync();
        log.info("[WebMap] Disabled - saved " + chunkData.size() + " chunks");
    }

    @EventHandler
    public void onChunkLoad(ChunkLoadEvent event) {
        if (getServer().getWorlds().isEmpty()) return;
        if (!event.getWorld().equals(getServer().getWorlds().get(0))) return;
        queueChunk(event.getChunk().getX(), event.getChunk().getZ(), true);
    }

    @EventHandler
    public void onBlockPlace(BlockPlaceEvent event) {
        if (getServer().getWorlds().isEmpty()) return;
        if (!event.getBlock().getWorld().equals(getServer().getWorlds().get(0))) return;
        queueChunk(event.getBlock().getChunk().getX(), event.getBlock().getChunk().getZ(), true);
    }

    @EventHandler
    public void onBlockBreak(BlockBreakEvent event) {
        if (getServer().getWorlds().isEmpty()) return;
        if (!event.getBlock().getWorld().equals(getServer().getWorlds().get(0))) return;
        queueChunk(event.getBlock().getChunk().getX(), event.getBlock().getChunk().getZ(), true);
    }

    private void queueChunk(int cx, int cz, boolean publishLive) {
        processQueue.add(new QueuedChunk(cx, cz, publishLive));
    }

    private void processChunks() {
        if (getServer().getWorlds().isEmpty()) return;
        World world = getServer().getWorlds().get(0);

        int limit = nmsFailed ? 1 : 4;
        for (int i = 0; i < limit; i++) {
            QueuedChunk queued = processQueue.poll();
            if (queued == null) break;

            boolean wasLoaded = world.isChunkLoaded(queued.cx, queued.cz);
            if (!wasLoaded && !world.loadChunk(queued.cx, queued.cz, false)) {
                continue;
            }

            processChunk(world.getChunkAt(queued.cx, queued.cz), queued.publishLive);

            if (!wasLoaded) {
                world.unloadChunk(queued.cx, queued.cz, false);
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
                log.warning("[WebMap] NMS block field not found - using slow method");
                return null;
            }

            return (byte[]) nmsBlocksField.get(nmsChunk);
        } catch (Exception e) {
            nmsFailed = true;
            log.warning("[WebMap] NMS access failed: " + e.getMessage());
            return null;
        }
    }

    private void processChunk(Chunk chunk, boolean publishLive) {
        int[] data = new int[2304];
        byte[] blocks = getNmsBlocks(chunk);

        for (int x = 0; x < 16; x++) {
            for (int z = 0; z < 16; z++) {
                int idx = x * 16 + z;
                int surfaceY = -1;
                int nmsBase = (blocks != null) ? x * 2048 + z * 128 : 0;

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

                int surfaceType = data[256 + idx];
                int base = surfaceY;
                for (int y = surfaceY - 1; y >= 0; y--) {
                    int typeId = (blocks != null)
                        ? (blocks[nmsBase + y] & 0xFF)
                        : chunk.getBlock(x, y, z).getTypeId();
                    if (typeId > 0 && typeId < 256 && !SKIP_BLOCK[typeId]) {
                        if (isVegetation(surfaceType) && typeId != surfaceType) break;
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
                                    if (gTypeId > 0 && gTypeId < 256 && !SKIP_BLOCK[gTypeId]) {
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

        long key = chunkKey(chunk.getX(), chunk.getZ());
        chunkData.put(key, data);
        if (publishLive) {
            liveChunkUpdates.put(key, data);
            liveUpdatesDirty = true;
        }
        dirty = true;
    }

    // --- Persistence ---

    private void saveMapData() {
        if (!dirty || saving || getServer().getWorlds().isEmpty()) return;
        dirty = false;
        saving = true;

        final HashMap<Long, int[]> snapshot = new HashMap<Long, int[]>(chunkData);
        final Location spawn = getServer().getWorlds().get(0).getSpawnLocation();
        final int sx = spawn.getBlockX();
        final int sy = spawn.getBlockY();
        final int sz = spawn.getBlockZ();

        new Thread(new Runnable() {
            public void run() {
                try {
                    writeMapFile(new File(webmapDir, "mapdata.bin.tmp"),
                        new File(webmapDir, "mapdata.bin"), MAGIC, FORMAT_VERSION, snapshot, sx, sy, sz, 0);
                } finally {
                    saving = false;
                }
            }
        }, "WebMap-Save").start();
    }

    private void saveMapDataSync() {
        if (chunkData.isEmpty() || getServer().getWorlds().isEmpty()) return;
        Location spawn = getServer().getWorlds().get(0).getSpawnLocation();
        writeMapFile(new File(webmapDir, "mapdata.bin.tmp"), new File(webmapDir, "mapdata.bin"),
            MAGIC, FORMAT_VERSION, new HashMap<Long, int[]>(chunkData),
            spawn.getBlockX(), spawn.getBlockY(), spawn.getBlockZ(), 0);
    }

    private void saveChunkUpdates() {
        if (!liveUpdatesDirty) return;
        liveUpdatesDirty = false;
        saveChunkUpdatesSync();
    }

    private void saveChunkUpdatesSync() {
        HashMap<Long, int[]> snapshot = new HashMap<Long, int[]>(liveChunkUpdates);
        if (snapshot.isEmpty()) return;
        liveUpdateRevision++;
        writeMapFile(new File(webmapDir, "chunk-updates.bin.tmp"),
            new File(webmapDir, "chunk-updates.bin"), UPDATE_MAGIC, UPDATE_FORMAT_VERSION,
            snapshot, 0, 0, 0, liveUpdateRevision);
    }

    private void writeMapFile(File tmpFile, File target, int magic, int version,
                              Map<Long, int[]> data, int sx, int sy, int sz, int revision) {
        try {
            DataOutputStream dos = new DataOutputStream(
                new BufferedOutputStream(new FileOutputStream(tmpFile), 65536));

            dos.writeInt(magic);
            dos.writeInt(version);
            if (magic == MAGIC) {
                dos.writeInt(sx);
                dos.writeInt(sy);
                dos.writeInt(sz);
            } else {
                dos.writeInt(revision);
            }
            dos.writeInt(data.size());

            for (Map.Entry<Long, int[]> entry : data.entrySet()) {
                long key = entry.getKey();
                int[] d = entry.getValue();

                dos.writeInt((int) (key >> 32));
                dos.writeInt((int) key);
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
            log.warning("[WebMap] Failed to write data file " + target.getName() + ": " + e.getMessage());
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
                log.warning("[WebMap] Invalid map data file - will regenerate");
                return;
            }

            int version = dis.readInt();
            if (version < 6) {
                dis.close();
                file.delete();
                log.warning("[WebMap] Map data version too old - deleted, will regenerate");
                return;
            }

            dis.readInt();
            dis.readInt();
            dis.readInt();
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
            String uuid = player.getUniqueId() != null ? player.getUniqueId().toString() : "";
            String skinUrl = resolveSkinUrl(player, uuid);
            Entity vehicle = player.getVehicle();
            String vehicleType = vehicle != null ? vehicle.getClass().getSimpleName() : "";
            boolean mounted = vehicle != null;
            boolean sitting = mounted && ("Arrow".equals(vehicleType)
                || "Minecart".equals(vehicleType) || "Boat".equals(vehicleType));

            sb.append("{\"name\":\"").append(escapeJson(player.getName())).append("\"")
              .append(",\"uuid\":\"").append(escapeJson(uuid)).append("\"")
              .append(",\"x\":").append(formatDecimal(loc.getX()))
              .append(",\"y\":").append(formatDecimal(loc.getY()))
              .append(",\"z\":").append(formatDecimal(loc.getZ()))
              .append(",\"yaw\":").append(formatAngle(loc.getYaw()))
              .append(",\"sneaking\":").append(player.isSneaking())
              .append(",\"sleeping\":").append(player.isSleeping())
              .append(",\"mounted\":").append(mounted)
              .append(",\"sitting\":").append(sitting);

            if (mounted) {
                sb.append(",\"vehicleType\":\"").append(escapeJson(vehicleType)).append("\"");
            }

            if (skinUrl != null && skinUrl.length() > 0) {
                sb.append(",\"skinUrl\":\"").append(escapeJson(skinUrl)).append("\"");
            }

            sb.append("}");
        }
        sb.append("]");
        writeJsonFile("players.json", sb.toString());
    }

    private void saveWorldState() {
        if (getServer().getWorlds().isEmpty()) return;
        World world = getServer().getWorlds().get(0);
        long time = world.getTime() % 24000L;
        boolean isDay = time >= 0L && time < 12300L;

        StringBuilder sb = new StringBuilder();
        sb.append("{\"time\":").append(time)
          .append(",\"dayProgress\":").append(String.format(Locale.US, "%.6f", time / 24000.0))
          .append(",\"isDay\":").append(isDay)
          .append(",\"storm\":").append(world.hasStorm())
          .append("}");

        writeJsonFile("world.json", sb.toString());
    }

    private void writeJsonFile(String name, String contents) {
        try {
            File tmpFile = new File(webmapDir, name + ".tmp");
            File target = new File(webmapDir, name);
            FileWriter fw = new FileWriter(tmpFile);
            fw.write(contents);
            fw.close();
            if (target.exists()) target.delete();
            tmpFile.renameTo(target);
        } catch (IOException e) {
            // Ephemeral view state is best effort.
        }
    }

    private String resolveSkinUrl(Player player, String uuid) {
        if (uuid == null || uuid.length() == 0) return null;

        String compactUuid = uuid.replace("-", "");
        String cached = skinUrlCache.get(compactUuid);
        if (cached != null) {
            return cached.length() == 0 ? null : cached;
        }

        Long retryAt = nextSkinLookupAttempt.get(compactUuid);
        if (retryAt != null && retryAt.longValue() > System.currentTimeMillis()) {
            return null;
        }

        if (pendingSkinLookups.putIfAbsent(compactUuid, Boolean.TRUE) == null) {
            final String lookupUuid = compactUuid;
            final String playerName = player.getName();
            new Thread(new Runnable() {
                public void run() {
                    try {
                        String skinUrl = fetchSkinUrl(lookupUuid);
                        if (skinUrl != null) {
                            skinUrlCache.put(lookupUuid, skinUrl);
                            nextSkinLookupAttempt.remove(lookupUuid);
                        } else {
                            skinUrlCache.put(lookupUuid, "");
                            nextSkinLookupAttempt.put(lookupUuid, System.currentTimeMillis() + 300000L);
                            log.info("[WebMap] No skin URL found for " + playerName);
                        }
                    } finally {
                        pendingSkinLookups.remove(lookupUuid);
                    }
                }
            }, "WebMap-Skin-" + playerName).start();
        }

        return null;
    }

    private String fetchSkinUrl(String uuid) {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(
                "https://sessionserver.mojang.com/session/minecraft/profile/" + uuid).openConnection();
            connection.setConnectTimeout(3000);
            connection.setReadTimeout(3000);
            connection.setUseCaches(false);
            connection.setRequestProperty("User-Agent", "BetaServer-WebMap");

            if (connection.getResponseCode() != 200) {
                return null;
            }

            String json = readUtf8(connection.getInputStream());
            Matcher valueMatcher = TEXTURE_VALUE_PATTERN.matcher(json);
            if (!valueMatcher.find()) {
                return null;
            }

            String decoded = new String(Base64.getDecoder().decode(valueMatcher.group(1)),
                StandardCharsets.UTF_8);
            Matcher urlMatcher = SKIN_URL_PATTERN.matcher(decoded);
            if (!urlMatcher.find()) {
                return null;
            }

            return urlMatcher.group(1).replace("\\/", "/");
        } catch (Exception e) {
            return null;
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    private static String readUtf8(InputStream input) throws IOException {
        StringBuilder sb = new StringBuilder();
        byte[] buffer = new byte[4096];
        int read;
        while ((read = input.read(buffer)) != -1) {
            sb.append(new String(buffer, 0, read, StandardCharsets.UTF_8));
        }
        return sb.toString();
    }

    // --- World scanning ---

    /**
     * Read McRegion file headers to find all existing chunk coordinates
     * in the world, and queue any that are not already in chunkData.
     */
    private void scanWorldChunks(World world) {
        for (Chunk chunk : world.getLoadedChunks()) {
            queueChunk(chunk.getX(), chunk.getZ(), false);
        }

        File worldDir = new File(world.getName());
        File regionDir = new File(worldDir, "region");
        if (!regionDir.isDirectory()) {
            log.info("[WebMap] No region directory found - only mapping loaded chunks");
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

            int regionX;
            int regionZ;
            try {
                regionX = Integer.parseInt(parts[1]);
                regionZ = Integer.parseInt(parts[2]);
            } catch (NumberFormatException e) {
                continue;
            }

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
                        if (sectorOffset == 0) continue;

                        int cx = regionX * 32 + lx;
                        int cz = regionZ * 32 + lz;
                        if (!chunkData.containsKey(chunkKey(cx, cz))) {
                            queueChunk(cx, cz, false);
                            queued++;
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

    private static String formatDecimal(double value) {
        return String.format(Locale.US, "%.1f", value);
    }

    private static String formatAngle(float value) {
        return String.format(Locale.US, "%.2f", value);
    }
}
