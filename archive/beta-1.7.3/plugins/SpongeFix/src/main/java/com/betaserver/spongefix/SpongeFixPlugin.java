package com.betaserver.spongefix;

import org.bukkit.Material;
import org.bukkit.World;
import org.bukkit.block.Block;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.block.BlockFromToEvent;
import org.bukkit.event.block.BlockPlaceEvent;
import org.bukkit.plugin.java.JavaPlugin;

import java.util.logging.Logger;

/**
 * Makes sponge blocks absorb water like in Minecraft Classic.
 * Absorbs water in a 5x5x5 area (radius 2) when placed,
 * and prevents water from flowing into that area.
 */
public class SpongeFixPlugin extends JavaPlugin implements Listener {

    private static final Logger log = Logger.getLogger("Minecraft.SpongeFix");
    private static final int RADIUS = 2;

    @Override
    public void onEnable() {
        getServer().getPluginManager().registerEvents(this, this);
        log.info("[SpongeFix] Enabled! Sponge blocks now absorb water.");
    }

    @Override
    public void onDisable() {
        log.info("[SpongeFix] Disabled");
    }

    @EventHandler
    public void onBlockPlace(BlockPlaceEvent event) {
        if (event.isCancelled()) return;
        if (event.getBlock().getType() != Material.SPONGE) return;
        absorbWater(event.getBlock());
    }

    @EventHandler
    public void onWaterFlow(BlockFromToEvent event) {
        if (event.isCancelled()) return;

        Material type = event.getBlock().getType();
        if (type != Material.WATER && type != Material.STATIONARY_WATER) return;

        if (hasSpongeNearby(event.getToBlock())) {
            event.setCancelled(true);
        }
    }

    private void absorbWater(Block sponge) {
        World world = sponge.getWorld();
        int sx = sponge.getX();
        int sy = sponge.getY();
        int sz = sponge.getZ();

        for (int x = -RADIUS; x <= RADIUS; x++) {
            for (int y = -RADIUS; y <= RADIUS; y++) {
                for (int z = -RADIUS; z <= RADIUS; z++) {
                    Block b = world.getBlockAt(sx + x, sy + y, sz + z);
                    Material mat = b.getType();
                    if (mat == Material.WATER || mat == Material.STATIONARY_WATER) {
                        b.setType(Material.AIR);
                    }
                }
            }
        }
    }

    private boolean hasSpongeNearby(Block block) {
        World world = block.getWorld();
        int bx = block.getX();
        int by = block.getY();
        int bz = block.getZ();

        for (int x = -RADIUS; x <= RADIUS; x++) {
            for (int y = -RADIUS; y <= RADIUS; y++) {
                for (int z = -RADIUS; z <= RADIUS; z++) {
                    if (world.getBlockAt(bx + x, by + y, bz + z).getType() == Material.SPONGE) {
                        return true;
                    }
                }
            }
        }
        return false;
    }
}
