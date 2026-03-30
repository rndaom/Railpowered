package com.betaserver.mapart;

import org.bukkit.Bukkit;
import org.bukkit.Material;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;
import org.bukkit.map.MapCanvas;
import org.bukkit.map.MapRenderer;
import org.bukkit.map.MapView;
import org.bukkit.plugin.java.JavaPlugin;

import java.awt.Graphics2D;
import java.awt.image.BufferedImage;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.logging.Logger;
import javax.imageio.ImageIO;

/**
 * Custom map art from image URLs.
 * Players hold a map and run /mapart <url> to paint it.
 * 128x128 pixels, ~52 color palette.
 */
public class MapArtPlugin extends JavaPlugin {

    private static final Logger log = Logger.getLogger("Minecraft.MapArt");

    // Beta 1.7.3 map color palette: 13 base colors x 4 shades = 52 usable colors
    // Base colors from NMS MapColor (RGB packed ints)
    private static final int[] BASE_COLORS = {
        0x000000,  //  0: NONE (transparent, skip)
        0x7FB238,  //  1: GRASS
        0xF7E9A3,  //  2: SAND
        0xC7C7C7,  //  3: WOOL
        0xFF0000,  //  4: FIRE
        0xA0A0FF,  //  5: ICE
        0xA7A7A7,  //  6: IRON
        0x007C00,  //  7: FOLIAGE
        0xFFFFFF,  //  8: SNOW
        0xA4A8B8,  //  9: CLAY
        0x976D4D,  // 10: DIRT
        0x707070,  // 11: STONE
        0x4040FF,  // 12: WATER
        0x8F7748,  // 13: WOOD
    };

    // Shade multipliers: dark, medium, full, darkest
    private static final int[] SHADE_MUL = {180, 220, 255, 135};

    // Precomputed palette: [colorId] = {R, G, B}
    private static final int[][] PALETTE = new int[56][3];
    static {
        for (int base = 1; base <= 13; base++) {
            int r = (BASE_COLORS[base] >> 16) & 0xFF;
            int g = (BASE_COLORS[base] >> 8) & 0xFF;
            int b = BASE_COLORS[base] & 0xFF;
            for (int shade = 0; shade < 4; shade++) {
                int id = base * 4 + shade;
                PALETTE[id][0] = r * SHADE_MUL[shade] / 255;
                PALETTE[id][1] = g * SHADE_MUL[shade] / 255;
                PALETTE[id][2] = b * SHADE_MUL[shade] / 255;
            }
        }
    }

    @Override
    public void onEnable() {
        loadSavedMaps();
        log.info("[MapArt] Enabled!");
    }

    @Override
    public void onDisable() {
        log.info("[MapArt] Disabled");
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!command.getName().equalsIgnoreCase("mapart")) return false;

        if (!(sender instanceof Player)) {
            sender.sendMessage("Only players can use this command.");
            return true;
        }

        final Player player = (Player) sender;

        if (args.length < 1) {
            player.sendMessage("\u00A7eUsage: /mapart <image-url>");
            player.sendMessage("\u00A77Hold a map and provide an image URL.");
            player.sendMessage("\u00A77Images are converted to 128x128 with ~52 colors.");
            return true;
        }

        if (player.getItemInHand() == null || player.getItemInHand().getType() != Material.MAP) {
            player.sendMessage("\u00A7cYou must be holding a map!");
            return true;
        }

        final String url = args[0];
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            player.sendMessage("\u00A7cURL must start with http:// or https://");
            return true;
        }

        player.sendMessage("\u00A7eDownloading image...");

        getServer().getScheduler().scheduleAsyncDelayedTask(this, new Runnable() {
            @Override
            public void run() {
                try {
                    final BufferedImage image = downloadAndResize(url);
                    getServer().getScheduler().scheduleSyncDelayedTask(MapArtPlugin.this, new Runnable() {
                        @Override
                        public void run() {
                            applyMapArt(player, image);
                        }
                    });
                } catch (final Exception e) {
                    getServer().getScheduler().scheduleSyncDelayedTask(MapArtPlugin.this, new Runnable() {
                        @Override
                        public void run() {
                            player.sendMessage("\u00A7cFailed: " + e.getMessage());
                        }
                    });
                }
            }
        });

        return true;
    }

    private BufferedImage downloadAndResize(String urlStr) throws Exception {
        URL url = new URL(urlStr);
        HttpURLConnection conn = (HttpURLConnection) url.openConnection();
        conn.setInstanceFollowRedirects(true);
        conn.setConnectTimeout(10000);
        conn.setReadTimeout(10000);
        conn.setRequestProperty("User-Agent", "MapArt/1.0");

        int status = conn.getResponseCode();
        if (status != 200) {
            throw new IOException("HTTP " + status);
        }
        if (conn.getContentLength() > 5 * 1024 * 1024) {
            throw new IOException("Image too large (max 5MB)");
        }

        InputStream in = conn.getInputStream();
        BufferedImage original = ImageIO.read(in);
        in.close();
        if (original == null) {
            throw new IOException("Could not decode image");
        }

        BufferedImage resized = new BufferedImage(128, 128, BufferedImage.TYPE_INT_ARGB);
        Graphics2D g = resized.createGraphics();
        g.drawImage(original, 0, 0, 128, 128, null);
        g.dispose();
        return resized;
    }

    private void applyMapArt(Player player, BufferedImage image) {
        if (player.getItemInHand() == null || player.getItemInHand().getType() != Material.MAP) {
            player.sendMessage("\u00A7cYou're no longer holding a map!");
            return;
        }

        short mapId = player.getItemInHand().getDurability();
        MapView view = Bukkit.getMap(mapId);
        if (view == null) {
            player.sendMessage("\u00A7cInvalid map. Right-click it once first to initialize.");
            return;
        }

        final byte[] colors = imageToMapColors(image);

        // Replace renderers with our custom image
        for (MapRenderer r : view.getRenderers()) {
            view.removeRenderer(r);
        }
        view.addRenderer(new StaticImageRenderer(colors));

        saveMapImage(mapId, image);
        player.sendMessage("\u00A7aMap art applied! (Map #" + mapId + ")");
    }

    /**
     * Convert a 128x128 image to map color bytes using nearest-color matching.
     */
    private byte[] imageToMapColors(BufferedImage image) {
        byte[] colors = new byte[128 * 128];
        for (int y = 0; y < 128; y++) {
            for (int x = 0; x < 128; x++) {
                int rgb = image.getRGB(x, y);
                int a = (rgb >> 24) & 0xFF;
                if (a < 128) {
                    colors[y * 128 + x] = 0; // transparent
                } else {
                    int r = (rgb >> 16) & 0xFF;
                    int g = (rgb >> 8) & 0xFF;
                    int b = rgb & 0xFF;
                    colors[y * 128 + x] = nearestColor(r, g, b);
                }
            }
        }
        return colors;
    }

    /**
     * Find the nearest map palette color for an RGB value.
     * Uses simple Euclidean distance in RGB space.
     */
    private byte nearestColor(int r, int g, int b) {
        int bestId = 4; // default to first real color
        int bestDist = Integer.MAX_VALUE;
        // Color IDs 4-55 are the usable colors (base 1-13, shade 0-3)
        for (int id = 4; id < 56; id++) {
            int dr = r - PALETTE[id][0];
            int dg = g - PALETTE[id][1];
            int db = b - PALETTE[id][2];
            int dist = dr * dr + dg * dg + db * db;
            if (dist < bestDist) {
                bestDist = dist;
                bestId = id;
            }
        }
        return (byte) bestId;
    }

    // --- Renderer ---

    private static class StaticImageRenderer extends MapRenderer {
        private final byte[] colors;
        private boolean drawn = false;

        StaticImageRenderer(byte[] colors) {
            this.colors = colors;
        }

        @Override
        public void render(MapView map, MapCanvas canvas, Player player) {
            if (!drawn) {
                for (int x = 0; x < 128; x++) {
                    for (int y = 0; y < 128; y++) {
                        canvas.setPixel(x, y, colors[y * 128 + x]);
                    }
                }
                drawn = true;
            }
        }
    }

    // --- Persistence ---

    private void saveMapImage(short mapId, BufferedImage image) {
        try {
            File dir = new File(getDataFolder(), "maps");
            dir.mkdirs();
            ImageIO.write(image, "PNG", new File(dir, mapId + ".png"));
        } catch (IOException e) {
            log.warning("[MapArt] Failed to save map " + mapId + ": " + e.getMessage());
        }
    }

    private void loadSavedMaps() {
        File dir = new File(getDataFolder(), "maps");
        if (!dir.exists()) return;

        File[] files = dir.listFiles();
        if (files == null) return;

        int count = 0;
        for (File file : files) {
            if (!file.getName().endsWith(".png")) continue;
            try {
                String name = file.getName().replace(".png", "");
                short mapId = Short.parseShort(name);
                BufferedImage image = ImageIO.read(file);
                if (image == null) continue;

                MapView view = Bukkit.getMap(mapId);
                if (view == null) continue;

                byte[] colors = imageToMapColors(image);
                for (MapRenderer r : view.getRenderers()) {
                    view.removeRenderer(r);
                }
                view.addRenderer(new StaticImageRenderer(colors));
                count++;
            } catch (Exception e) {
                log.warning("[MapArt] Failed to load " + file.getName() + ": " + e.getMessage());
            }
        }

        if (count > 0) {
            log.info("[MapArt] Loaded " + count + " saved map(s)");
        }
    }
}
