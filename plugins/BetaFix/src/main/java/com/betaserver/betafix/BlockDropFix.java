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

    // Wooden stairs block ID
    private static final int WOOD_STAIRS_ID = 53;

    public BlockDropFix(Logger logger) {
        this.logger = logger;
    }

    /**
     * Fix: Wooden stairs should drop the stair block, not planks.
     * Stone stairs already drop correctly — this makes wooden stairs consistent.
     */
    @EventHandler(priority = EventPriority.HIGHEST)
    public void onBlockBreak(BlockBreakEvent event) {
        if (event.isCancelled()) return;

        Block block = event.getBlock();
        if (block.getTypeId() != WOOD_STAIRS_ID) return;

        // Don't drop if player is in creative mode
        Player player = event.getPlayer();
        if (player.getGameMode() == org.bukkit.GameMode.CREATIVE) return;

        // Cancel default drops by setting the block to air manually,
        // then drop the correct item
        Location dropLoc = block.getLocation().add(0.5, 0.5, 0.5);
        block.setTypeId(0); // set to air
        event.setCancelled(true);

        // Drop 1 wooden stair block
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

        // Check if fire can be placed on the clicked face
        BlockFace face = event.getBlockFace();
        Block target = clicked.getRelative(face);

        // Fire can only exist in air blocks
        if (target.getType() != Material.AIR) {
            event.setCancelled(true);
        }
    }
}
