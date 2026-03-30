package com.betaserver.greentext;

import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerChatEvent;
import org.bukkit.plugin.java.JavaPlugin;

import java.util.logging.Logger;

/**
 * 4chan-style greentext in chat.
 * Messages starting with ">" appear in green.
 */
public class GreenTextPlugin extends JavaPlugin implements Listener {

    private static final Logger log = Logger.getLogger("Minecraft.GreenText");
    private static final String GREEN = "\u00A7a";

    @Override
    public void onEnable() {
        getServer().getPluginManager().registerEvents(this, this);
        log.info("[GreenText] Enabled!");
    }

    @Override
    public void onDisable() {
        log.info("[GreenText] Disabled");
    }

    @EventHandler
    public void onPlayerChat(PlayerChatEvent event) {
        if (event.isCancelled()) return;

        String message = event.getMessage();
        if (message.startsWith(">")) {
            event.setMessage(GREEN + message);
        }
    }
}
