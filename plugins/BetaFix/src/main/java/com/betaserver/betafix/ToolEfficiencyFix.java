package com.betaserver.betafix;

import net.minecraft.server.Block;
import net.minecraft.server.Item;
import net.minecraft.server.ItemAxe;
import net.minecraft.server.ItemTool;

import java.lang.reflect.Field;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;
import java.util.logging.Logger;

/**
 * Fixes axe efficiency on wooden blocks that are missing from the effective blocks list.
 * Uses NMS reflection to patch the axe's effective blocks array at startup.
 *
 * Poseidon already includes WOOD, BOOKSHELF, LOG, CHEST in the axe list.
 * This fix adds the remaining wooden blocks that are still missing.
 *
 * Note: Wooden slabs (block ID 44, data 2) share their ID with stone/cobble/sandstone
 * slabs, so they cannot be added to the axe list without affecting all slab types.
 */
public class ToolEfficiencyFix {

    private final Logger logger;

    // Block IDs for wooden blocks missing from the axe effective list.
    // Poseidon already has: WOOD(5), LOG(17), BOOKSHELF(47), CHEST(54)
    private static final int[] WOODEN_BLOCK_IDS = {
        25,  // Note Block
        53,  // Wooden Stairs
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

    public ToolEfficiencyFix(Logger logger) {
        this.logger = logger;
    }

    /**
     * Apply NMS reflection fix to add wooden blocks to axe effective list.
     * Returns true if the fix was applied successfully.
     */
    public boolean apply() {
        try {
            return doApply();
        } catch (Throwable t) {
            logger.warning("[BetaFix] Failed to apply tool efficiency fix: " + t.getClass().getName() + ": " + t.getMessage());
            t.printStackTrace();
            return false;
        }
    }

    private boolean doApply() throws Exception {
        // Find the Block[] field in ItemTool (parent of ItemAxe)
        Field effectiveBlocksField = null;
        Class<?> current = ItemTool.class;
        while (current != null && current != Object.class) {
            for (Field field : current.getDeclaredFields()) {
                if (field.getType() == Block[].class) {
                    effectiveBlocksField = field;
                    logger.info("[BetaFix] Found Block[] field '" + field.getName()
                        + "' in " + current.getSimpleName());
                    break;
                }
            }
            if (effectiveBlocksField != null) break;
            current = current.getSuperclass();
        }

        if (effectiveBlocksField == null) {
            logger.warning("[BetaFix] Could not find Block[] field in ItemTool hierarchy");
            // Log all fields for debugging
            for (Field f : ItemTool.class.getDeclaredFields()) {
                logger.info("[BetaFix]   ItemTool field: " + f.getName() + " type=" + f.getType().getName());
            }
            return false;
        }

        effectiveBlocksField.setAccessible(true);

        int axeCount = 0;

        // Iterate through all items to find axes
        for (int i = 0; i < Item.byId.length; i++) {
            Item item = Item.byId[i];
            if (item instanceof ItemAxe) {
                Block[] currentBlocks = (Block[]) effectiveBlocksField.get(item);
                logger.info("[BetaFix] Axe item ID " + i + " has " + currentBlocks.length + " effective blocks");

                Block[] newBlocks = expandEffectiveBlocks(currentBlocks);
                effectiveBlocksField.set(item, newBlocks);

                logger.info("[BetaFix] Expanded to " + newBlocks.length + " effective blocks");
                axeCount++;
            }
        }

        if (axeCount > 0) {
            logger.info("[BetaFix] Patched " + axeCount + " axe type(s) with "
                + WOODEN_BLOCK_IDS.length + " additional wooden blocks");
            return true;
        } else {
            logger.warning("[BetaFix] No axe items found in Item.byId registry");
            return false;
        }
    }

    private Block[] expandEffectiveBlocks(Block[] original) {
        Set<Integer> existingIds = new HashSet<Integer>();
        for (Block b : original) {
            if (b != null) {
                existingIds.add(b.id);
            }
        }

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
}
