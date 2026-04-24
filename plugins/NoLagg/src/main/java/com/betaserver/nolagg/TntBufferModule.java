package com.betaserver.nolagg;

import org.bukkit.Location;
import org.bukkit.World;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Entity;
import org.bukkit.entity.TNTPrimed;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.entity.ExplosionPrimeEvent;

import java.util.LinkedList;

class TntBufferModule extends NoLaggModule implements Listener {

    private static final int MAX_PER_TICK = 5;

    private final LinkedList<PendingExplosion> queue = new LinkedList<PendingExplosion>();
    private boolean processingQueue = false;
    private int processedThisTick = 0;
    private int taskId = -1;

    TntBufferModule(NoLaggPlugin plugin) {
        super(plugin);
    }

    @Override
    void onEnable() {
        plugin.getServer().getPluginManager().registerEvents(this, plugin);

        // Process queue every tick
        taskId = plugin.getServer().getScheduler().scheduleSyncRepeatingTask(plugin, new Runnable() {
            @Override
            public void run() {
                processedThisTick = 0;
                processQueue();
            }
        }, 1L, 1L);
    }

    @Override
    void onDisable() {
        if (taskId != -1) {
            plugin.getServer().getScheduler().cancelTask(taskId);
            taskId = -1;
        }
        queue.clear();
    }

    @EventHandler
    public void onExplosionPrime(ExplosionPrimeEvent event) {
        if (event.isCancelled()) return;

        // Only buffer TNT explosions
        Entity entity = event.getEntity();
        if (!(entity instanceof TNTPrimed)) return;

        // During queue processing, let explosions through (already rate-limited)
        if (processingQueue) return;

        // If under the per-tick limit and queue is empty, let it through
        if (queue.isEmpty() && processedThisTick < MAX_PER_TICK) {
            processedThisTick++;
            return;
        }

        // Queue it
        event.setCancelled(true);
        entity.remove();
        queue.add(new PendingExplosion(
            entity.getLocation(),
            event.getRadius(),
            event.getFire()
        ));
    }

    private void processQueue() {
        if (queue.isEmpty()) return;

        processingQueue = true;
        int count = Math.min(MAX_PER_TICK, queue.size());
        for (int i = 0; i < count; i++) {
            PendingExplosion pending = queue.removeFirst();
            World world = pending.location.getWorld();
            if (world != null) {
                world.createExplosion(
                    pending.location.getX(),
                    pending.location.getY(),
                    pending.location.getZ(),
                    pending.radius,
                    pending.fire
                );
            }
        }
        processingQueue = false;
    }

    void handleCommand(CommandSender sender, String[] args) {
        if (args.length > 0 && args[0].equalsIgnoreCase("clear")) {
            int size = queue.size();
            queue.clear();
            sender.sendMessage("\u00A7a[NoLagg] \u00A7fCleared \u00A7e" + size + " \u00A7fpending TNT detonations.");
        } else {
            sender.sendMessage("\u00A7e--- TNT Buffer ---");
            sender.sendMessage("\u00A77Queued: \u00A7f" + queue.size() + " detonations");
            sender.sendMessage("\u00A77Rate: \u00A7f" + MAX_PER_TICK + " per tick");
            if (!queue.isEmpty()) {
                sender.sendMessage("\u00A77Use \u00A7f/lag tnt clear \u00A77to cancel all.");
            }
        }
    }

    private static class PendingExplosion {
        final Location location;
        final float radius;
        final boolean fire;

        PendingExplosion(Location location, float radius, boolean fire) {
            this.location = location;
            this.radius = radius;
            this.fire = fire;
        }
    }
}
