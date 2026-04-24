package com.betaserver.nolagg;

import org.bukkit.Location;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.entity.ItemSpawnEvent;

import java.util.HashMap;
import java.util.Map;

class ItemBufferModule extends NoLaggModule implements Listener {

    private static final int MAX_ITEMS_PER_CHUNK = 30;
    private static final int WINDOW_TICKS = 100; // 5 seconds

    private final Map<String, Integer> spawnCounts = new HashMap<String, Integer>();
    private int taskId = -1;

    ItemBufferModule(NoLaggPlugin plugin) {
        super(plugin);
    }

    @Override
    void onEnable() {
        plugin.getServer().getPluginManager().registerEvents(this, plugin);

        // Reset counters every window
        taskId = plugin.getServer().getScheduler().scheduleSyncRepeatingTask(plugin, new Runnable() {
            @Override
            public void run() {
                spawnCounts.clear();
            }
        }, WINDOW_TICKS, WINDOW_TICKS);
    }

    @Override
    void onDisable() {
        if (taskId != -1) {
            plugin.getServer().getScheduler().cancelTask(taskId);
            taskId = -1;
        }
        spawnCounts.clear();
    }

    @EventHandler
    public void onItemSpawn(ItemSpawnEvent event) {
        if (event.isCancelled()) return;

        Location loc = event.getLocation();
        String chunkKey = (loc.getBlockX() >> 4) + ":" + (loc.getBlockZ() >> 4);

        Integer count = spawnCounts.get(chunkKey);
        int current = (count == null) ? 0 : count;

        if (current >= MAX_ITEMS_PER_CHUNK) {
            event.setCancelled(true);
            return;
        }

        spawnCounts.put(chunkKey, current + 1);
    }
}
