package com.betaserver.onesleep;

import org.bukkit.World;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerBedEnterEvent;
import org.bukkit.plugin.java.JavaPlugin;

import java.util.logging.Logger;

/**
 * Only one player needs to sleep to skip the night.
 */
public class OneSleepPlugin extends JavaPlugin implements Listener {

    private static final Logger log = Logger.getLogger("Minecraft.OneSleep");

    @Override
    public void onEnable() {
        getServer().getPluginManager().registerEvents(this, this);
        log.info("[OneSleep] Enabled! Only one player needs to sleep to skip the night.");
    }

    @Override
    public void onDisable() {
        log.info("[OneSleep] Disabled");
    }

    @EventHandler
    public void onBedEnter(PlayerBedEnterEvent event) {
        if (event.isCancelled()) return;

        final Player sleeper = event.getPlayer();
        final World world = sleeper.getWorld();

        // Wait 100 ticks (5 seconds) for the sleep animation, then skip night
        getServer().getScheduler().scheduleSyncDelayedTask(this, new Runnable() {
            @Override
            public void run() {
                if (!sleeper.isOnline() || !sleeper.isSleeping()) return;

                world.setTime(0);
                world.setStorm(false);
                world.setThundering(false);

                getServer().broadcastMessage("\u00A7e" + sleeper.getName() + " slept through the night.");
            }
        }, 100L);
    }
}
