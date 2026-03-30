package com.betaserver.betafix;

import net.minecraft.server.Block;
import net.minecraft.server.Item;
import net.minecraft.server.ItemAxe;
import net.minecraft.server.ItemTool;
import org.bukkit.Material;
import org.bukkit.craftbukkit.entity.CraftPlayer;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.block.BlockBreakEvent;
import org.bukkit.event.block.BlockDamageEvent;
import org.bukkit.inventory.ItemStack;
import org.bukkit.potion.PotionEffect;
import org.bukkit.potion.PotionEffectType;

import java.lang.reflect.Field;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;
import java.util.logging.Logger;

/**
 * Fixes axe efficiency on wooden blocks that are missing from the effective blocks list.
 * Also handles wooden slabs via event-based approach (shared block ID with stone slabs).
 */
public class ToolEfficiencyFix implements Listener {

    private final Logger logger;
    private boolean slabFixEnabled = true;

    // Block IDs for wooden blocks missing from the axe effective list
    private static final int[] WOODEN_BLOCK_IDS = {
        25,  // Note Block
        47,  // Bookshelf
        53,  // Wooden Stairs
        54,  // Chest
        58,  // Crafting Table
        63,  // Sign Post
        64,  // Wooden Door
        65,  // Ladder
        68,  // Wall Sign
        72,  // Wooden Pressure Plate
        84,  // Jukebox
        85,  // Fence
        96,  // Trapdoor
    };

    // Slab block ID 44 with data value 2 = wooden slab
    private static final int SLAB_BLOCK_ID = 44;
    private static final int WOODEN_SLAB_DATA = 2;

    // Track players currently mining wooden slabs (to remove haste when done)
    private final Set<String> playersMiningWoodSlab = new HashSet<String>();

    public ToolEfficiencyFix(Logger logger) {
        this.logger = logger;
    }

    /**
     * Apply NMS reflection fix to add wooden blocks to axe effective list.
     * Returns true if the fix was applied successfully.
     */
    public boolean applyNmsFix() {
        try {
            // Find the Block[] field in ItemTool (parent of ItemAxe)
            Field effectiveBlocksField = findBlockArrayField(ItemTool.class);
            if (effectiveBlocksField == null) {
                logger.warning("[BetaFix] Could not find blocksEffectiveAgainst field in ItemTool");
                return false;
            }

            effectiveBlocksField.setAccessible(true);

            int axeCount = 0;

            // Iterate through all items to find axes
            for (int i = 0; i < Item.byId.length; i++) {
                Item item = Item.byId[i];
                if (item instanceof ItemAxe) {
                    Block[] currentBlocks = (Block[]) effectiveBlocksField.get(item);
                    Block[] newBlocks = expandEffectiveBlocks(currentBlocks);
                    effectiveBlocksField.set(item, newBlocks);
                    axeCount++;
                }
            }

            if (axeCount > 0) {
                logger.info("[BetaFix] Tool efficiency fix applied to " + axeCount + " axe type(s) — "
                    + WOODEN_BLOCK_IDS.length + " wooden blocks now mine faster with axes");
                return true;
            } else {
                logger.warning("[BetaFix] No axe items found in registry");
                return false;
            }
        } catch (Exception e) {
            logger.warning("[BetaFix] Failed to apply tool efficiency NMS fix: " + e.getMessage());
            return false;
        }
    }

    /**
     * Find the Block[] typed field in the given class or its superclasses.
     */
    private Field findBlockArrayField(Class<?> clazz) {
        Class<?> current = clazz;
        while (current != null && current != Object.class) {
            for (Field field : current.getDeclaredFields()) {
                if (field.getType() == Block[].class) {
                    return field;
                }
            }
            current = current.getSuperclass();
        }
        return null;
    }

    /**
     * Create a new Block[] with the original blocks plus missing wooden blocks.
     */
    private Block[] expandEffectiveBlocks(Block[] original) {
        Set<Integer> existingIds = new HashSet<Integer>();
        for (Block b : original) {
            if (b != null) {
                existingIds.add(b.id);
            }
        }

        // Count how many we need to add
        int addCount = 0;
        for (int id : WOODEN_BLOCK_IDS) {
            if (!existingIds.contains(id) && id < Block.byId.length && Block.byId[id] != null) {
                addCount++;
            }
        }

        Block[] expanded = Arrays.copyOf(original, original.length + addCount);
        int idx = original.length;
        for (int id : WOODEN_BLOCK_IDS) {
            if (!existingIds.contains(id) && id < Block.byId.length && Block.byId[id] != null) {
                expanded[idx++] = Block.byId[id];
            }
        }

        return expanded;
    }

    // --- Event-based fix for wooden slabs ---

    @EventHandler
    public void onBlockDamage(BlockDamageEvent event) {
        if (!slabFixEnabled) return;

        org.bukkit.block.Block block = event.getBlock();
        if (block.getTypeId() != SLAB_BLOCK_ID) return;
        if (block.getData() != WOODEN_SLAB_DATA) return;

        Player player = event.getPlayer();
        ItemStack held = player.getItemInHand();
        if (held == null) return;

        // Check if player is holding an axe
        Material type = held.getType();
        if (type == Material.WOOD_AXE || type == Material.STONE_AXE
            || type == Material.IRON_AXE || type == Material.GOLD_AXE
            || type == Material.DIAMOND_AXE) {

            // Apply haste to speed up mining (level 1 = roughly matches proper tool speed)
            player.addPotionEffect(new PotionEffect(PotionEffectType.FAST_DIGGING, 200, 0), true);
            playersMiningWoodSlab.add(player.getName());
        }
    }

    @EventHandler
    public void onBlockBreak(BlockBreakEvent event) {
        if (!slabFixEnabled) return;

        Player player = event.getPlayer();
        if (playersMiningWoodSlab.remove(player.getName())) {
            player.removePotionEffect(PotionEffectType.FAST_DIGGING);
        }
    }

    public void setSlabFixEnabled(boolean enabled) {
        this.slabFixEnabled = enabled;
    }
}
