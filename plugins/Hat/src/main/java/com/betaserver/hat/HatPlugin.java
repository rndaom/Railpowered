package com.betaserver.hat;

import org.bukkit.Material;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;
import org.bukkit.inventory.ItemStack;
import org.bukkit.inventory.PlayerInventory;
import org.bukkit.plugin.java.JavaPlugin;

import java.util.logging.Logger;

/**
 * /hat — wear any block on your head.
 * Use /hat with an empty hand to remove your hat.
 */
public class HatPlugin extends JavaPlugin {

    private static final Logger log = Logger.getLogger("Minecraft.Hat");

    @Override
    public void onEnable() {
        log.info("[Hat] Enabled! Use /hat to wear any block.");
    }

    @Override
    public void onDisable() {
        log.info("[Hat] Disabled");
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!command.getName().equalsIgnoreCase("hat")) return false;

        if (!(sender instanceof Player)) {
            sender.sendMessage("Only players can wear hats!");
            return true;
        }

        Player player = (Player) sender;
        PlayerInventory inv = player.getInventory();
        ItemStack hand = player.getItemInHand();

        if (hand == null || hand.getType() == Material.AIR) {
            // Empty hand: take hat off
            ItemStack helmet = inv.getHelmet();
            if (helmet != null && helmet.getType() != Material.AIR) {
                inv.setHelmet(null);
                player.setItemInHand(helmet);
                player.sendMessage("\u00A7eHat removed.");
            } else {
                player.sendMessage("\u00A7cHold a block to wear it as a hat!");
            }
            return true;
        }

        // Swap hand item and helmet
        ItemStack oldHelmet = inv.getHelmet();
        inv.setHelmet(hand);

        if (oldHelmet != null && oldHelmet.getType() != Material.AIR) {
            player.setItemInHand(oldHelmet);
        } else {
            player.setItemInHand(null);
        }

        String name = hand.getType().name().toLowerCase().replace('_', ' ');
        player.sendMessage("\u00A7eYou are now wearing \u00A7f" + name + "\u00A7e as a hat!");
        return true;
    }
}
