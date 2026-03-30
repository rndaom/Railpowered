package com.betaserver.betafix;

import org.bukkit.plugin.java.JavaPlugin;

/**
 * BetaFix — Fixes Beta 1.7.3 gameplay inconsistencies.
 *
 * Fixes applied:
 * - Axe efficiency on all wooden blocks (chests, crafting tables, bookshelves, etc.)
 * - Wooden slab axe efficiency (event-based, since slabs share block ID 44)
 * - Wooden stairs drop stair blocks instead of planks
 * - Flint and steel durability not wasted on invalid surfaces
 */
public class BetaFixPlugin extends JavaPlugin {

    @Override
    public void onEnable() {
        getLogger().info("BetaFix loading — fixing Beta 1.7.3 inconsistencies...");

        // Fix 1: Tool efficiency on wooden blocks (NMS reflection)
        ToolEfficiencyFix toolFix = new ToolEfficiencyFix(getLogger());
        boolean nmsApplied = toolFix.applyNmsFix();

        if (nmsApplied) {
            getLogger().info("Axe efficiency fix applied (13 wooden block types)");
        } else {
            getLogger().warning("Axe efficiency NMS fix failed — wooden blocks may not mine faster with axes");
        }

        // Register event listener for wooden slab fix (part of tool efficiency)
        getServer().getPluginManager().registerEvents(toolFix, this);
        getLogger().info("Wooden slab axe efficiency fix enabled (event-based)");

        // Fix 2 & 3: Block drops and flint & steel durability
        BlockDropFix dropFix = new BlockDropFix(getLogger());
        getServer().getPluginManager().registerEvents(dropFix, this);
        getLogger().info("Wooden stair drop fix enabled");
        getLogger().info("Flint & steel durability fix enabled");

        getLogger().info("BetaFix enabled — all fixes active!");
    }

    @Override
    public void onDisable() {
        getLogger().info("BetaFix disabled");
    }
}
