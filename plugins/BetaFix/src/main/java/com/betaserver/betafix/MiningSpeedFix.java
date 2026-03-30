package com.betaserver.betafix;

import org.bukkit.Location;
import org.bukkit.Material;
import org.bukkit.block.Block;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.block.BlockBreakEvent;
import org.bukkit.event.block.BlockDamageEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.inventory.ItemStack;
import org.bukkit.plugin.java.JavaPlugin;

import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.logging.Logger;

/**
 * Forces correct axe mining speed for wooden blocks.
 *
 * The vanilla Beta 1.7.3 client doesn't know about server-side axe
 * effectiveness changes — mining speed is client-driven. This fix uses
 * a server-side timer to break blocks at the correct axe-boosted speed,
 * overriding the client's slow calculation.
 */
public class MiningSpeedFix implements Listener {

    private final JavaPlugin plugin;
    private final Logger logger;

    // Active mining task per player
    private final Map<String, Integer> activeTasks = new HashMap<String, Integer>();

    // Wooden blocks NOT in the vanilla client's axe list (WOOD=5, LOG=17, BOOKSHELF=47, CHEST=54)
    private static final Set<Integer> BOOSTED_BLOCKS = new HashSet<Integer>();
    static {
        BOOSTED_BLOCKS.add(25);  // Note Block
        BOOSTED_BLOCKS.add(53);  // Wooden Stairs
        BOOSTED_BLOCKS.add(58);  // Crafting Table
        BOOSTED_BLOCKS.add(63);  // Sign Post
        BOOSTED_BLOCKS.add(65);  // Ladder
        BOOSTED_BLOCKS.add(68);  // Wall Sign
        BOOSTED_BLOCKS.add(72);  // Wooden Pressure Plate
        BOOSTED_BLOCKS.add(85);  // Fence
        BOOSTED_BLOCKS.add(96);  // Trapdoor
    }

    // Block hardness values (vanilla Beta 1.7.3)
    private static final float[] HARDNESS = new float[256];
    static {
        HARDNESS[25] = 0.8F;   // Note Block
        HARDNESS[53] = 2.0F;   // Wooden Stairs
        HARDNESS[58] = 2.5F;   // Crafting Table
        HARDNESS[63] = 1.0F;   // Sign Post
        HARDNESS[65] = 0.4F;   // Ladder
        HARDNESS[68] = 1.0F;   // Wall Sign
        HARDNESS[72] = 0.5F;   // Wooden Pressure Plate
        HARDNESS[85] = 2.0F;   // Fence
        HARDNESS[96] = 3.0F;   // Trapdoor
    }

    // Drop item ID for each block (most drop themselves)
    private static final int[] DROP_ID = new int[256];
    static {
        for (int i = 0; i < 256; i++) DROP_ID[i] = i;
        DROP_ID[63] = 323;  // Sign Post  -> Sign item
        DROP_ID[68] = 323;  // Wall Sign  -> Sign item
    }

    // For getTargetBlock ray trace — only air is transparent
    private static final HashSet<Byte> TRANSPARENT = new HashSet<Byte>();
    static {
        TRANSPARENT.add((byte) 0);
    }

    public MiningSpeedFix(JavaPlugin plugin, Logger logger) {
        this.plugin = plugin;
        this.logger = logger;
    }

    @EventHandler
    public void onBlockDamage(BlockDamageEvent event) {
        if (event.isCancelled()) return;

        final Player player = event.getPlayer();
        ItemStack hand = player.getItemInHand();
        if (hand == null) return;

        float axeSpeed = getAxeSpeed(hand.getType());
        if (axeSpeed <= 0) return;

        Block block = event.getBlock();
        final int blockId = block.getTypeId();
        if (!BOOSTED_BLOCKS.contains(blockId)) return;

        float hardness = HARDNESS[blockId];
        if (hardness <= 0) return;

        // Calculate ticks to break with axe speed boost
        float damagePerTick = axeSpeed / hardness / 30.0F;
        final int ticksToBreak = Math.max(1, (int) Math.ceil(1.0 / damagePerTick));

        final String playerName = player.getName();
        cancelTask(playerName);

        final Location blockLoc = block.getLocation();
        final int bx = block.getX();
        final int by = block.getY();
        final int bz = block.getZ();

        int taskId = plugin.getServer().getScheduler().scheduleSyncRepeatingTask(
            plugin,
            new Runnable() {
                int elapsed = 0;

                @Override
                public void run() {
                    elapsed++;

                    if (!player.isOnline()) {
                        cancelTask(playerName);
                        return;
                    }

                    // Player moved too far (mining reach is ~6 blocks)
                    if (player.getLocation().distanceSquared(blockLoc) > 36) {
                        cancelTask(playerName);
                        return;
                    }

                    // Player switched away from axe
                    ItemStack currentHand = player.getItemInHand();
                    if (currentHand == null || getAxeSpeed(currentHand.getType()) <= 0) {
                        cancelTask(playerName);
                        return;
                    }

                    // Block already broken by something else
                    Block current = blockLoc.getBlock();
                    if (current.getTypeId() != blockId) {
                        cancelTask(playerName);
                        return;
                    }

                    // Player stopped looking at this block
                    try {
                        Block target = player.getTargetBlock(TRANSPARENT, 6);
                        if (target == null || target.getX() != bx
                                || target.getY() != by || target.getZ() != bz) {
                            cancelTask(playerName);
                            return;
                        }
                    } catch (Exception e) {
                        cancelTask(playerName);
                        return;
                    }

                    if (elapsed >= ticksToBreak) {
                        // Drop the correct item
                        Location dropLoc = blockLoc.clone().add(0.5, 0.5, 0.5);
                        int dropId = DROP_ID[blockId];
                        current.setTypeId(0);
                        current.getWorld().dropItemNaturally(dropLoc, new ItemStack(dropId, 1));

                        // Damage the axe
                        short durability = currentHand.getDurability();
                        currentHand.setDurability((short) (durability + 1));
                        if (currentHand.getDurability() >= currentHand.getType().getMaxDurability()) {
                            player.setItemInHand(null);
                        }

                        cancelTask(playerName);
                    }
                }
            },
            1L, 1L  // start after 1 tick, repeat every tick
        );

        activeTasks.put(playerName, taskId);
    }

    /**
     * If the block broke naturally (client finished mining), cancel our task.
     */
    @EventHandler
    public void onBlockBreak(BlockBreakEvent event) {
        cancelTask(event.getPlayer().getName());
    }

    @EventHandler
    public void onPlayerQuit(PlayerQuitEvent event) {
        cancelTask(event.getPlayer().getName());
    }

    private void cancelTask(String playerName) {
        Integer taskId = activeTasks.remove(playerName);
        if (taskId != null) {
            plugin.getServer().getScheduler().cancelTask(taskId);
        }
    }

    private float getAxeSpeed(Material material) {
        switch (material) {
            case WOOD_AXE:    return 2.0F;
            case STONE_AXE:   return 4.0F;
            case IRON_AXE:    return 6.0F;
            case DIAMOND_AXE: return 8.0F;
            case GOLD_AXE:    return 12.0F;
            default:          return 0;
        }
    }
}
