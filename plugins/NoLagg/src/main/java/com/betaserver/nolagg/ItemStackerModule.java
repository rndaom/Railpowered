package com.betaserver.nolagg;

import org.bukkit.World;
import org.bukkit.entity.Entity;
import org.bukkit.entity.Item;
import org.bukkit.inventory.ItemStack;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

class ItemStackerModule extends NoLaggModule {

    private static final double MERGE_RADIUS_SQ = 9.0; // 3 blocks squared
    private static final int CHECK_INTERVAL = 100;      // 5 seconds
    private static final int MAX_PER_CYCLE = 200;

    private int taskId = -1;

    ItemStackerModule(NoLaggPlugin plugin) {
        super(plugin);
    }

    @Override
    void onEnable() {
        taskId = plugin.getServer().getScheduler().scheduleSyncRepeatingTask(plugin, new Runnable() {
            @Override
            public void run() {
                mergeItems();
            }
        }, CHECK_INTERVAL, CHECK_INTERVAL);
    }

    @Override
    void onDisable() {
        if (taskId != -1) {
            plugin.getServer().getScheduler().cancelTask(taskId);
            taskId = -1;
        }
    }

    private void mergeItems() {
        for (World world : plugin.getServer().getWorlds()) {
            mergeItemsInWorld(world);
        }
    }

    private void mergeItemsInWorld(World world) {
        // Collect all item entities, up to MAX_PER_CYCLE
        List<Item> items = new ArrayList<Item>();
        for (Entity entity : world.getEntities()) {
            if (entity instanceof Item && !entity.isDead()) {
                items.add((Item) entity);
                if (items.size() >= MAX_PER_CYCLE) break;
            }
        }

        if (items.size() < 2) return;

        // Group by chunk for efficient comparison
        Map<String, List<Item>> byChunk = new HashMap<String, List<Item>>();
        for (Item item : items) {
            String key = (item.getLocation().getBlockX() >> 4) + ":" + (item.getLocation().getBlockZ() >> 4);
            List<Item> list = byChunk.get(key);
            if (list == null) {
                list = new ArrayList<Item>();
                byChunk.put(key, list);
            }
            list.add(item);
        }

        // Merge within each chunk group
        for (List<Item> group : byChunk.values()) {
            mergeGroup(group);
        }
    }

    private void mergeGroup(List<Item> items) {
        for (int i = 0; i < items.size(); i++) {
            Item a = items.get(i);
            if (a.isDead()) continue;

            ItemStack stackA = a.getItemStack();
            int maxStack = stackA.getType().getMaxStackSize();
            if (stackA.getAmount() >= maxStack) continue;

            for (int j = i + 1; j < items.size(); j++) {
                Item b = items.get(j);
                if (b.isDead()) continue;

                ItemStack stackB = b.getItemStack();
                if (stackA.getTypeId() != stackB.getTypeId()) continue;
                if (stackA.getDurability() != stackB.getDurability()) continue;

                if (a.getLocation().distanceSquared(b.getLocation()) > MERGE_RADIUS_SQ) continue;

                // Merge B into A
                int combined = stackA.getAmount() + stackB.getAmount();
                if (combined <= maxStack) {
                    stackA.setAmount(combined);
                    a.setItemStack(stackA);
                    b.remove();
                } else {
                    stackA.setAmount(maxStack);
                    a.setItemStack(stackA);
                    stackB.setAmount(combined - maxStack);
                    b.setItemStack(stackB);
                }

                if (stackA.getAmount() >= maxStack) break;
            }
        }
    }
}
