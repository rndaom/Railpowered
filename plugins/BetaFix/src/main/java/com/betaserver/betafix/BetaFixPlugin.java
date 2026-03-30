package com.betaserver.betafix;

import org.bukkit.plugin.java.JavaPlugin;

/**
 * BetaFix - Fixes Beta 1.7.3 gameplay inconsistencies.
 *
 * Fixes applied:
 * - Axe efficiency on 13 wooden block types (NMS patch)
 * - Wooden stairs drop stair blocks instead of planks
 * - Flint and steel durability not wasted on invalid surfaces
 */
public class BetaFixPlugin extends JavaPlugin {

    @Override
    public void onEnable() {
        getLogger().info("Loading BetaFix...");

        // Fix 1: Tool efficiency on wooden blocks (NMS reflection)
        ToolEfficiencyFix toolFix = new ToolEfficiencyFix(getLogger());
        if (toolFix.apply()) {
            getLogger().info("Axe efficiency fix applied (13 wooden block types)");
        } else {
            getLogger().warning("Axe efficiency fix failed");
        }

        // Fix 2 & 3: Block drops and flint & steel durability
        BlockDropFix dropFix = new BlockDropFix(getLogger());
        getServer().getPluginManager().registerEvents(dropFix, this);
        getLogger().info("Stair drop fix + flint & steel fix enabled");

        getLogger().info("BetaFix enabled!");
    }

    @Override
    public void onDisable() {
        getLogger().info("BetaFix disabled");
    }
}
