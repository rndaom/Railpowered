package com.betaserver.betafix;

import org.bukkit.Location;
import org.bukkit.Material;
import org.bukkit.block.Block;
import org.bukkit.block.BlockFace;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.block.Action;
import org.bukkit.event.block.BlockBreakEvent;
import org.bukkit.event.player.PlayerInteractEvent;
import org.bukkit.inventory.ItemStack;

import java.util.logging.Logger;

/**
 * Fixes block drop inconsistencies and tool durability bugs in Beta 1.7.3.
 *
 * - Wooden stairs drop stair blocks instead of planks
 * - Flint and steel doesn't waste durability on non-flammable surfaces
 */
public class BlockDropFix implements Listener {

    private final Logger logger;

    private static final int WOOD_STAIRS_ID = 53;

    public BlockDropFix(Logger logger) {
        this.logger = logger;
    }

    /**
     * Fix: Wooden stairs should drop the stair block, not planks.
     */
    @EventHandler(priority = EventPriority.HIGHEST)
    public void onBlockBreak(BlockBreakEvent event) {
        if (event.isCancelled()) return;

        Block block = event.getBlock();
        if (block.getTypeId() != WOOD_STAIRS_ID) return;

        Player player = event.getPlayer();
        if (player.isOp() && player.getGameMode() != null
            && player.getGameMode().getValue() == 1) {
            return; // creative mode, no drops
        }

        Location dropLoc = block.getLocation().add(0.5, 0.5, 0.5);
        block.setTypeId(0);
        event.setCancelled(true);

        block.getWorld().dropItemNaturally(dropLoc, new ItemStack(WOOD_STAIRS_ID, 1));
    }

    /**
     * Fix: Flint and steel should not lose durability when right-clicking
     * a surface where fire cannot be placed.
     */
    @EventHandler(priority = EventPriority.HIGH)
    public void onPlayerInteract(PlayerInteractEvent event) {
        if (event.getAction() != Action.RIGHT_CLICK_BLOCK) return;

        Player player = event.getPlayer();
        ItemStack held = player.getItemInHand();
        if (held == null || held.getType() != Material.FLINT_AND_STEEL) return;

        Block clicked = event.getClickedBlock();
        if (clicked == null) return;

        BlockFace face = event.getBlockFace();
        Block target = clicked.getRelative(face);

        if (target.getType() != Material.AIR) {
            event.setCancelled(true);
        }
    }
}
