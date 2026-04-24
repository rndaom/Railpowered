package com.betaserver.nolagg;

import com.legacyminecraft.poseidon.Poseidon;
import org.bukkit.World;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Animals;
import org.bukkit.entity.Arrow;
import org.bukkit.entity.Entity;
import org.bukkit.entity.Item;
import org.bukkit.entity.Minecart;
import org.bukkit.entity.Monster;
import org.bukkit.entity.Player;

import java.util.LinkedList;
import java.util.List;

class TpsMonitorModule extends NoLaggModule {

    TpsMonitorModule(NoLaggPlugin plugin) {
        super(plugin);
    }

    @Override
    void onEnable() {
        // No scheduler or events needed
    }

    @Override
    void onDisable() {
        // Nothing to clean up
    }

    double getTps() {
        try {
            LinkedList<Double> records = Poseidon.getTpsRecords();
            if (records != null && !records.isEmpty()) {
                return records.getFirst();
            }
        } catch (Exception e) {
            // Fall through to default
        }
        return 20.0;
    }

    void showOverview(CommandSender sender) {
        double tps = getTps();
        Runtime rt = Runtime.getRuntime();
        long usedMb = (rt.totalMemory() - rt.freeMemory()) / 1048576;
        long maxMb = rt.maxMemory() / 1048576;
        int percent = (int) (usedMb * 100 / maxMb);

        int totalEntities = 0;
        int totalChunks = 0;
        StringBuilder worldInfo = new StringBuilder();
        List<World> worlds = plugin.getServer().getWorlds();
        for (int i = 0; i < worlds.size(); i++) {
            World w = worlds.get(i);
            int entities = w.getEntities().size();
            totalEntities += entities;
            totalChunks += w.getLoadedChunks().length;
            if (i > 0) worldInfo.append(", ");
            worldInfo.append(w.getName()).append(": ").append(entities);
        }

        String tpsColor = tps >= 18 ? "\u00A7a" : (tps >= 15 ? "\u00A7e" : "\u00A7c");

        sender.sendMessage("\u00A7e--- Server Performance ---");
        sender.sendMessage("\u00A77TPS: " + tpsColor + String.format("%.1f", tps) + " \u00A77/ 20.0");
        sender.sendMessage("\u00A77Memory: \u00A7f" + usedMb + " MB \u00A77/ " + maxMb + " MB (" + percent + "%)");
        sender.sendMessage("\u00A77Entities: \u00A7f" + totalEntities + " \u00A77(" + worldInfo + ")");
        sender.sendMessage("\u00A77Chunks: \u00A7f" + totalChunks + " loaded");
    }

    void showMemory(CommandSender sender) {
        Runtime rt = Runtime.getRuntime();
        long total = rt.totalMemory();
        long free = rt.freeMemory();
        long max = rt.maxMemory();
        long used = total - free;

        sender.sendMessage("\u00A7e--- Memory ---");
        sender.sendMessage("\u00A77Used:      \u00A7f" + (used / 1048576) + " MB");
        sender.sendMessage("\u00A77Allocated: \u00A7f" + (total / 1048576) + " MB");
        sender.sendMessage("\u00A77Max:       \u00A7f" + (max / 1048576) + " MB");
        sender.sendMessage("\u00A77Free:      \u00A7f" + (free / 1048576) + " MB \u00A77(in allocated)");
        sender.sendMessage("\u00A77Usage:     \u00A7f" + (used * 100 / max) + "%");
    }

    void showStats(CommandSender sender) {
        sender.sendMessage("\u00A7e--- Entity Breakdown ---");
        for (World world : plugin.getServer().getWorlds()) {
            int items = 0, monsters = 0, animals = 0;
            int minecarts = 0, arrows = 0, other = 0;

            for (Entity entity : world.getEntities()) {
                if (entity instanceof Player) continue;
                if (entity instanceof Item) items++;
                else if (entity instanceof Monster) monsters++;
                else if (entity instanceof Animals) animals++;
                else if (entity instanceof Minecart) minecarts++;
                else if (entity instanceof Arrow) arrows++;
                else other++;
            }

            int total = items + monsters + animals + minecarts + arrows + other;
            sender.sendMessage("\u00A7f" + world.getName() + " \u00A77(" + total + " total):");
            sender.sendMessage("  \u00A77Items: \u00A7f" + items
                + "  \u00A77Monsters: \u00A7f" + monsters
                + "  \u00A77Animals: \u00A7f" + animals);
            sender.sendMessage("  \u00A77Minecarts: \u00A7f" + minecarts
                + "  \u00A77Arrows: \u00A7f" + arrows
                + "  \u00A77Other: \u00A7f" + other);
        }
    }

    void runGc(CommandSender sender) {
        Runtime rt = Runtime.getRuntime();
        long beforeUsed = (rt.totalMemory() - rt.freeMemory()) / 1048576;

        System.gc();

        long afterUsed = (rt.totalMemory() - rt.freeMemory()) / 1048576;
        long freed = beforeUsed - afterUsed;

        sender.sendMessage("\u00A7eGarbage collection complete.");
        sender.sendMessage("\u00A77Before: \u00A7f" + beforeUsed + " MB \u00A77-> After: \u00A7f" + afterUsed + " MB \u00A77(freed " + freed + " MB)");
    }
}
