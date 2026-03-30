package com.betaserver.betafix;

import org.bukkit.plugin.java.JavaPlugin;

import java.util.logging.Logger;

/**
 * BetaFix - Fixes Beta 1.7.3 gameplay inconsistencies.
 */
public class BetaFixPlugin extends JavaPlugin {

    private static final Logger log = Logger.getLogger("Minecraft.BetaFix");

    @Override
    public void onEnable() {
        log.info("[BetaFix] Loading...");

        // Fix 1: Tool efficiency on wooden blocks (NMS reflection)
        try {
            ToolEfficiencyFix toolFix = new ToolEfficiencyFix(log);
            if (toolFix.apply()) {
                log.info("[BetaFix] Axe efficiency fix applied");
            } else {
                log.warning("[BetaFix] Axe efficiency fix failed — see above for details");
            }
        } catch (Throwable t) {
            log.warning("[BetaFix] Axe efficiency fix crashed: " + t.getClass().getName() + ": " + t.getMessage());
        }

        // Fix 2 & 3: Block drops and flint & steel durability
        BlockDropFix dropFix = new BlockDropFix(log);
        getServer().getPluginManager().registerEvents(dropFix, this);
        log.info("[BetaFix] Stair drop fix + flint & steel fix enabled");

        log.info("[BetaFix] Enabled!");
    }

    @Override
    public void onDisable() {
        log.info("[BetaFix] Disabled");
    }
}
