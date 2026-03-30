package com.betaserver.sit;

import org.bukkit.Location;
import org.bukkit.Material;
import org.bukkit.block.Block;
import org.bukkit.block.BlockFace;
import org.bukkit.entity.Arrow;
import org.bukkit.entity.Entity;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.block.Action;
import org.bukkit.event.player.PlayerInteractEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.inventory.ItemStack;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.util.Vector;

import java.util.HashMap;
import java.util.Iterator;
import java.util.Map;
import java.util.logging.Logger;

/**
 * Right-click stairs or slabs with an empty hand to sit down.
 * Uses an arrow entity as an invisible seat — gives the minecart sitting pose.
 */
public class SitPlugin extends JavaPlugin implements Listener {

    private static final Logger log = Logger.getLogger("Minecraft.Sit");
    private final Map<String, SeatInfo> sittingPlayers = new HashMap<String, SeatInfo>();

    @Override
    public void onEnable() {
        getServer().getPluginManager().registerEvents(this, this);

        // Cleanup: detect dismounted players, remove dead seats, keep arrows in place
        getServer().getScheduler().scheduleSyncRepeatingTask(this, new Runnable() {
            @Override
            public void run() {
                Iterator<Map.Entry<String, SeatInfo>> it = sittingPlayers.entrySet().iterator();
                while (it.hasNext()) {
                    Map.Entry<String, SeatInfo> entry = it.next();
                    SeatInfo info = entry.getValue();
                    Entity seat = info.seat;

                    if (seat.isDead() || seat.getPassenger() == null) {
                        seat.remove();
                        it.remove();
                        continue;
                    }

                    // Pin the arrow to its original position (prevent gravity/physics)
                    Location current = seat.getLocation();
                    if (current.distanceSquared(info.origin) > 0.01) {
                        seat.eject();
                        seat.teleport(info.origin);
                        Player p = getServer().getPlayer(entry.getKey());
                        if (p != null && p.isOnline()) {
                            seat.setPassenger(p);
                        }
                    }
                }
            }
        }, 5L, 5L);

        log.info("[Sit] Enabled! Right-click stairs/slabs with empty hand to sit.");
    }

    @Override
    public void onDisable() {
        for (SeatInfo info : sittingPlayers.values()) {
            if (info.seat.getPassenger() != null) info.seat.eject();
            info.seat.remove();
        }
        sittingPlayers.clear();
        log.info("[Sit] Disabled");
    }

    @EventHandler
    public void onPlayerInteract(PlayerInteractEvent event) {
        if (event.getAction() != Action.RIGHT_CLICK_BLOCK) return;

        Player player = event.getPlayer();

        // If already sitting, right-click to stand up
        if (sittingPlayers.containsKey(player.getName())) {
            unsit(player);
            event.setCancelled(true);
            return;
        }

        // Must have empty hand
        ItemStack hand = player.getItemInHand();
        if (hand != null && hand.getType() != Material.AIR) return;

        Block block = event.getClickedBlock();
        if (block == null) return;
        if (!isSittable(block.getTypeId())) return;

        // Need air above to sit
        if (block.getRelative(BlockFace.UP).getType() != Material.AIR) return;

        // Check nobody else is sitting here
        Location blockCenter = block.getLocation().add(0.5, 0.5, 0.5);
        for (SeatInfo info : sittingPlayers.values()) {
            if (info.seat.getLocation().distanceSquared(blockCenter) < 1.5) {
                return;
            }
        }

        sit(player, block);
        event.setCancelled(true);
    }

    @EventHandler
    public void onPlayerQuit(PlayerQuitEvent event) {
        unsit(event.getPlayer());
    }

    private void sit(Player player, Block block) {
        // Arrow mount offset is ~0.375, player height is 1.8.
        // Total from arrow Y to player head: ~2.175 blocks.
        // With ceiling at block.Y + 2, arrow must be at Y - 0.2 or lower.
        // Using Y - 0.5 keeps player well below ceiling and hides arrow inside the block.
        Location sitLoc = new Location(block.getWorld(),
            block.getX() + 0.5, block.getY() - 0.5, block.getZ() + 0.5);

        Arrow seat = block.getWorld().spawn(sitLoc, Arrow.class);
        seat.setVelocity(new Vector(0, 0, 0));
        seat.setPassenger(player);

        sittingPlayers.put(player.getName(), new SeatInfo(seat, sitLoc.clone()));
    }

    private void unsit(Player player) {
        SeatInfo info = sittingPlayers.remove(player.getName());
        if (info != null) {
            Location standUp = info.origin.clone().add(0, 0.6, 0);
            info.seat.eject();
            info.seat.remove();
            player.teleport(standUp);
        }
    }

    private boolean isSittable(int typeId) {
        return typeId == 44  // Slab (all types: stone, sandstone, wood, cobble)
            || typeId == 53  // Wooden Stairs
            || typeId == 67; // Cobblestone Stairs
    }

    private static class SeatInfo {
        final Entity seat;
        final Location origin;

        SeatInfo(Entity seat, Location origin) {
            this.seat = seat;
            this.origin = origin;
        }
    }
}
