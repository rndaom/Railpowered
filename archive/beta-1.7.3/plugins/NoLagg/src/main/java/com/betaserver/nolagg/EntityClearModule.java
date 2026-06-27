package com.betaserver.nolagg;

import org.bukkit.World;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Animals;
import org.bukkit.entity.Arrow;
import org.bukkit.entity.Entity;
import org.bukkit.entity.Item;
import org.bukkit.entity.Minecart;
import org.bukkit.entity.Monster;
import org.bukkit.entity.Player;

import java.util.List;

class EntityClearModule extends NoLaggModule {

    EntityClearModule(NoLaggPlugin plugin) {
        super(plugin);
    }

    @Override
    void onEnable() {
        // Command-only module
    }

    @Override
    void onDisable() {
        // Nothing to clean up
    }

    void handleClear(CommandSender sender, String[] args, boolean allWorlds) {
        String type = args.length > 0 ? args[0].toLowerCase() : "items";

        if (!isValidType(type)) {
            sender.sendMessage("\u00A7cUnknown type: " + type);
            sender.sendMessage("\u00A77Types: items, mobs, animals, monsters, arrows, minecarts, all");
            return;
        }

        int removed;
        if (allWorlds) {
            removed = clearInWorlds(plugin.getServer().getWorlds(), type);
            sender.sendMessage("\u00A7a[NoLagg] \u00A7fRemoved \u00A7e" + removed + " \u00A7f" + type + " from all worlds.");
        } else {
            Player player = (Player) sender;
            removed = clearInWorld(player.getWorld(), type);
            sender.sendMessage("\u00A7a[NoLagg] \u00A7fRemoved \u00A7e" + removed + " \u00A7f" + type + " from " + player.getWorld().getName() + ".");
        }
    }

    /**
     * Clears all ground items across all worlds. Used by LagCommandModule.
     */
    int clearItemsAllWorlds() {
        return clearInWorlds(plugin.getServer().getWorlds(), "items");
    }

    private int clearInWorlds(List<World> worlds, String type) {
        int total = 0;
        for (World world : worlds) {
            total += clearInWorld(world, type);
        }
        return total;
    }

    private int clearInWorld(World world, String type) {
        int count = 0;
        for (Entity entity : world.getEntities()) {
            if (entity instanceof Player) continue;
            if (matchesType(entity, type)) {
                entity.remove();
                count++;
            }
        }
        return count;
    }

    private boolean matchesType(Entity entity, String type) {
        switch (type) {
            case "items":    return entity instanceof Item;
            case "monsters": return entity instanceof Monster;
            case "animals":  return entity instanceof Animals;
            case "mobs":     return entity instanceof Monster || entity instanceof Animals;
            case "arrows":   return entity instanceof Arrow;
            case "minecarts": return entity instanceof Minecart;
            case "all":      return true;
            default:         return false;
        }
    }

    private boolean isValidType(String type) {
        return type.equals("items") || type.equals("monsters") || type.equals("animals")
            || type.equals("mobs") || type.equals("arrows") || type.equals("minecarts")
            || type.equals("all");
    }
}
