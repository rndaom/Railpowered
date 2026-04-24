package com.betaserver.afk;

import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerChatEvent;
import org.bukkit.event.player.PlayerInteractEvent;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerMoveEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.plugin.java.JavaPlugin;

import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.logging.Logger;

/**
 * Kicks players who are completely idle (no mouse or keyboard input)
 * for 10 minutes. Ops can toggle bypass with /bypassafk.
 */
public class AFKPlugin extends JavaPlugin implements Listener {

    private static final Logger log = Logger.getLogger("Minecraft.AFK");
    private static final long AFK_TIMEOUT_MS = 10 * 60 * 1000; // 10 minutes

    private final Map<String, Long> lastActivity = new HashMap<String, Long>();
    private final Set<String> bypassed = new HashSet<String>();

    @Override
    public void onEnable() {
        getServer().getPluginManager().registerEvents(this, this);

        // Check for AFK players every 30 seconds
        getServer().getScheduler().scheduleSyncRepeatingTask(this, new Runnable() {
            @Override
            public void run() {
                checkAFK();
            }
        }, 20L * 30, 20L * 30);

        // Initialize activity for anyone already online (plugin reload)
        for (Player p : getServer().getOnlinePlayers()) {
            lastActivity.put(p.getName(), System.currentTimeMillis());
        }

        log.info("[AFK] Enabled — 10 minute idle kick");
    }

    @Override
    public void onDisable() {
        lastActivity.clear();
        bypassed.clear();
        log.info("[AFK] Disabled");
    }

    // --- Activity tracking ---

    private void recordActivity(String name) {
        lastActivity.put(name, System.currentTimeMillis());
    }

    @EventHandler
    public void onJoin(PlayerJoinEvent event) {
        recordActivity(event.getPlayer().getName());
    }

    @EventHandler
    public void onQuit(PlayerQuitEvent event) {
        String name = event.getPlayer().getName();
        lastActivity.remove(name);
        bypassed.remove(name);
    }

    @EventHandler
    public void onMove(PlayerMoveEvent event) {
        // Only count actual position or look changes
        if (event.getFrom().getX() != event.getTo().getX()
                || event.getFrom().getY() != event.getTo().getY()
                || event.getFrom().getZ() != event.getTo().getZ()
                || event.getFrom().getYaw() != event.getTo().getYaw()
                || event.getFrom().getPitch() != event.getTo().getPitch()) {
            recordActivity(event.getPlayer().getName());
        }
    }

    @EventHandler
    public void onChat(PlayerChatEvent event) {
        recordActivity(event.getPlayer().getName());
    }

    @EventHandler
    public void onInteract(PlayerInteractEvent event) {
        recordActivity(event.getPlayer().getName());
    }

    // --- AFK check ---

    private void checkAFK() {
        long now = System.currentTimeMillis();
        for (Player player : getServer().getOnlinePlayers()) {
            String name = player.getName();
            if (bypassed.contains(name)) continue;

            Long last = lastActivity.get(name);
            if (last == null) {
                recordActivity(name);
                continue;
            }

            if (now - last >= AFK_TIMEOUT_MS) {
                player.kickPlayer("Kicked for being AFK (10 minutes idle)");
                log.info("[AFK] Kicked " + name + " for being idle");
            }
        }
    }

    // --- /bypassafk command ---

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!command.getName().equalsIgnoreCase("bypassafk")) return false;

        if (!(sender instanceof Player)) {
            sender.sendMessage("Only players can use this command.");
            return true;
        }

        Player player = (Player) sender;
        if (!player.isOp()) {
            player.sendMessage("\u00A7cOnly operators can use this command.");
            return true;
        }

        String name = player.getName();
        if (bypassed.contains(name)) {
            bypassed.remove(name);
            recordActivity(name);
            player.sendMessage("\u00A7eAFK bypass disabled.");
        } else {
            bypassed.add(name);
            player.sendMessage("\u00A7aAFK bypass enabled. You won't be kicked for idling.");
        }

        return true;
    }
}
