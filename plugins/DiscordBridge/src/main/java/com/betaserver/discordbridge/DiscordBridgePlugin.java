package com.betaserver.discordbridge;

import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerChatEvent;
import org.bukkit.plugin.java.JavaPlugin;

import java.nio.charset.Charset;
import java.util.Base64;
import java.util.logging.Logger;

/**
 * Bridges player chat to the external server manager and renders inbound
 * Discord messages inside Minecraft.
 */
public class DiscordBridgePlugin extends JavaPlugin implements Listener {

    private static final Logger log = Logger.getLogger("Minecraft.DiscordBridge");
    private static final String RELAY_PREFIX = "[DiscordBridge] CHAT";
    private static final Charset UTF8 = Charset.forName("UTF-8");
    private static final String DISCORD_PREFIX = "\u00A79[Discord] \u00A7f";

    private final Base64.Encoder encoder = Base64.getEncoder();
    private final Base64.Decoder decoder = Base64.getDecoder();
    private boolean relayOutbound;

    @Override
    public void onEnable() {
        relayOutbound = hasValue("DISCORD_WEBHOOK_URL");
        getServer().getPluginManager().registerEvents(this, this);

        if (relayOutbound) {
            log.info("[DiscordBridge] Enabled - relaying Minecraft chat to Discord");
        } else {
            log.info("[DiscordBridge] Enabled - inbound Discord chat only (DISCORD_WEBHOOK_URL not set)");
        }
    }

    @Override
    public void onDisable() {
        log.info("[DiscordBridge] Disabled");
    }

    @EventHandler(ignoreCancelled = true)
    public void onPlayerChat(PlayerChatEvent event) {
        if (!relayOutbound) {
            return;
        }

        String player = cleanOutbound(event.getPlayer().getName(), 32);
        String message = cleanOutbound(event.getMessage(), 220);
        if (message.length() == 0) {
            return;
        }

        log.info(RELAY_PREFIX + " " + encode(player) + " " + encode(message));
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!command.getName().equalsIgnoreCase("dchat")) {
            return false;
        }

        if (args.length < 2) {
            sender.sendMessage("Usage: /dchat <author-b64> <message-b64>");
            return true;
        }

        String author;
        String message;
        try {
            author = cleanInbound(decode(args[0]), 32);
            message = cleanInbound(decode(args[1]), 220);
        } catch (IllegalArgumentException e) {
            log.warning("[DiscordBridge] Invalid /dchat payload: " + e.getMessage());
            return true;
        }

        if (author.length() == 0 || message.length() == 0) {
            return true;
        }

        getServer().broadcastMessage(DISCORD_PREFIX + author + "\u00A77: \u00A7f" + message);
        return true;
    }

    private boolean hasValue(String key) {
        String value = System.getenv(key);
        return value != null && value.trim().length() > 0;
    }

    private String encode(String value) {
        return encoder.encodeToString(value.getBytes(UTF8));
    }

    private String decode(String value) {
        return new String(decoder.decode(value), UTF8);
    }

    private String cleanOutbound(String value, int maxLen) {
        if (value == null) {
            return "";
        }

        StringBuilder cleaned = new StringBuilder();
        boolean skipColorCode = false;
        for (int i = 0; i < value.length(); i++) {
            char ch = value.charAt(i);
            if (skipColorCode) {
                skipColorCode = false;
                continue;
            }
            if (ch == '\u00A7') {
                skipColorCode = true;
                continue;
            }
            if (ch == '\r' || ch == '\n' || ch < 32) {
                continue;
            }
            cleaned.append(ch);
            if (cleaned.length() >= maxLen) {
                break;
            }
        }
        return cleaned.toString().trim();
    }

    private String cleanInbound(String value, int maxLen) {
        if (value == null) {
            return "";
        }

        StringBuilder cleaned = new StringBuilder();
        boolean skipColorCode = false;
        for (int i = 0; i < value.length(); i++) {
            char ch = value.charAt(i);
            if (skipColorCode) {
                skipColorCode = false;
                continue;
            }
            if (ch == '\u00A7') {
                skipColorCode = true;
                continue;
            }
            if (ch == '\r' || ch == '\n' || ch < 32) {
                continue;
            }
            cleaned.append(ch);
            if (cleaned.length() >= maxLen) {
                break;
            }
        }
        return cleaned.toString().trim();
    }
}
