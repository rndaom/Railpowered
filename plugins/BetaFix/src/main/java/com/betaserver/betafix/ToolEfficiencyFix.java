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
 * Note: Wooden slabs (block ID 44, data 2) share their ID with stone/cobble/sandstone
 * slabs, so they cannot be added to the axe list without affecting all slab types.
 * A proper fix for wooden slabs would require deeper NMS patches.
 */
public class ToolEfficiencyFix {

    private final Logger logger;

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

    public ToolEfficiencyFix(Logger logger) {
        this.logger = logger;
    }

    /**
     * Apply NMS reflection fix to add wooden blocks to axe effective list.
     * Returns true if the fix was applied successfully.
     */
    public boolean apply() {
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
                logger.info("[BetaFix] Axe efficiency fix applied to " + axeCount + " axe type(s) - "
                    + WOODEN_BLOCK_IDS.length + " wooden blocks now mine faster with axes");
                return true;
            } else {
                logger.warning("[BetaFix] No axe items found in registry");
                return false;
            }
        } catch (Exception e) {
            logger.warning("[BetaFix] Failed to apply tool efficiency fix: " + e.getMessage());
            return false;
        }
    }

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
