package com.betaserver.coloredsigns;

import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.block.SignChangeEvent;
import org.bukkit.plugin.java.JavaPlugin;

import java.util.logging.Logger;

/**
 * Use &0-&9, &a-&f color codes when writing signs.
 */
public class ColoredSignsPlugin extends JavaPlugin implements Listener {

    private static final Logger log = Logger.getLogger("Minecraft.ColoredSigns");

    @Override
    public void onEnable() {
        getServer().getPluginManager().registerEvents(this, this);
        log.info("[ColoredSigns] Enabled! Use & color codes on signs.");
    }

    @Override
    public void onDisable() {
        log.info("[ColoredSigns] Disabled");
    }

    @EventHandler
    public void onSignChange(SignChangeEvent event) {
        for (int i = 0; i < 4; i++) {
            String line = event.getLine(i);
            if (line != null && line.contains("&")) {
                event.setLine(i, translateColorCodes(line));
            }
        }
    }

    private String translateColorCodes(String text) {
        char[] chars = text.toCharArray();
        for (int i = 0; i < chars.length - 1; i++) {
            if (chars[i] == '&' && "0123456789abcdefklmnor".indexOf(chars[i + 1]) != -1) {
                chars[i] = '\u00A7'; // section sign (color code prefix)
            }
        }
        return new String(chars);
    }
}
