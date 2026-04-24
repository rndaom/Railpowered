package com.betaserver.nolagg;

import org.bukkit.World;
import org.bukkit.entity.Animals;
import org.bukkit.entity.Entity;
import org.bukkit.entity.LivingEntity;
import org.bukkit.entity.Monster;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.entity.CreatureSpawnEvent;

import java.util.HashMap;
import java.util.Map;

class SpawnLimiterModule extends NoLaggModule implements Listener {

    private static final int HOSTILE_PER_CHUNK = 8;
    private static final int PASSIVE_PER_CHUNK = 6;
    private static final int HOSTILE_PER_WORLD = 200;
    private static final int PASSIVE_PER_WORLD = 100;
    private static final int CACHE_INTERVAL = 100; // 5 seconds

    // Chunk key -> [hostile, passive]
    private final Map<String, int[]> chunkCounts = new HashMap<String, int[]>();
    // World name -> [hostile, passive]
    private final Map<String, int[]> worldCounts = new HashMap<String, int[]>();

    private int taskId = -1;

    SpawnLimiterModule(NoLaggPlugin plugin) {
        super(plugin);
    }

    @Override
    void onEnable() {
        plugin.getServer().getPluginManager().registerEvents(this, plugin);

        // Refresh entity count cache periodically
        taskId = plugin.getServer().getScheduler().scheduleSyncRepeatingTask(plugin, new Runnable() {
            @Override
            public void run() {
                refreshCounts();
            }
        }, 20L, CACHE_INTERVAL);
    }

    @Override
    void onDisable() {
        if (taskId != -1) {
            plugin.getServer().getScheduler().cancelTask(taskId);
            taskId = -1;
        }
        chunkCounts.clear();
        worldCounts.clear();
    }

    @EventHandler
    public void onCreatureSpawn(CreatureSpawnEvent event) {
        if (event.isCancelled()) return;

        // Never block plugin-spawned or spawner entities
        CreatureSpawnEvent.SpawnReason reason = event.getSpawnReason();
        if (reason == CreatureSpawnEvent.SpawnReason.CUSTOM) return;

        Entity entity = event.getEntity();
        boolean hostile = entity instanceof Monster;
        boolean passive = entity instanceof Animals;
        if (!hostile && !passive) return;

        // Check world limits
        int[] wCounts = worldCounts.get(entity.getWorld().getName());
        if (wCounts != null) {
            if (hostile && wCounts[0] >= HOSTILE_PER_WORLD) {
                event.setCancelled(true);
                return;
            }
            if (passive && wCounts[1] >= PASSIVE_PER_WORLD) {
                event.setCancelled(true);
                return;
            }
        }

        // Check chunk limits
        int cx = event.getLocation().getBlockX() >> 4;
        int cz = event.getLocation().getBlockZ() >> 4;
        String chunkKey = entity.getWorld().getName() + ":" + cx + ":" + cz;

        int[] cCounts = chunkCounts.get(chunkKey);
        if (cCounts != null) {
            if (hostile && cCounts[0] >= HOSTILE_PER_CHUNK) {
                event.setCancelled(true);
                return;
            }
            if (passive && cCounts[1] >= PASSIVE_PER_CHUNK) {
                event.setCancelled(true);
                return;
            }
        }
    }

    private void refreshCounts() {
        chunkCounts.clear();
        worldCounts.clear();

        for (World world : plugin.getServer().getWorlds()) {
            int worldHostile = 0;
            int worldPassive = 0;

            for (LivingEntity entity : world.getLivingEntities()) {
                boolean hostile = entity instanceof Monster;
                boolean passive = entity instanceof Animals;
                if (!hostile && !passive) continue;

                if (hostile) worldHostile++;
                else worldPassive++;

                int cx = entity.getLocation().getBlockX() >> 4;
                int cz = entity.getLocation().getBlockZ() >> 4;
                String chunkKey = world.getName() + ":" + cx + ":" + cz;

                int[] counts = chunkCounts.get(chunkKey);
                if (counts == null) {
                    counts = new int[]{0, 0};
                    chunkCounts.put(chunkKey, counts);
                }
                if (hostile) counts[0]++;
                else counts[1]++;
            }

            worldCounts.put(world.getName(), new int[]{worldHostile, worldPassive});
        }
    }
}
