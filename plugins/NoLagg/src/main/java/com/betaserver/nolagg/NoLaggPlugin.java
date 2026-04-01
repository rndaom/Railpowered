package com.betaserver.nolagg;

import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;
import org.bukkit.plugin.java.JavaPlugin;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.logging.Logger;

public class NoLaggPlugin extends JavaPlugin {

    private static final Logger log = Logger.getLogger("Minecraft.NoLagg");
    private final List<NoLaggModule> modules = new ArrayList<NoLaggModule>();

    private TpsMonitorModule tpsMonitor;
    private EntityClearModule entityClear;
    private TntBufferModule tntBuffer;

    @Override
    public void onEnable() {
        // Create modules — TpsMonitor and EntityClear first (LagCommand depends on them)
        tpsMonitor = new TpsMonitorModule(this);
        entityClear = new EntityClearModule(this);
        modules.add(tpsMonitor);
        modules.add(entityClear);
        modules.add(new ItemStackerModule(this));
        modules.add(new ItemBufferModule(this));
        tntBuffer = new TntBufferModule(this);
        modules.add(tntBuffer);
        modules.add(new SpawnLimiterModule(this));
        modules.add(new ThreadWatchdogModule(this));
        modules.add(new LagCommandModule(this, tpsMonitor, entityClear));

        for (NoLaggModule module : modules) {
            module.onEnable();
        }

        log.info("[NoLagg] Enabled - 8 modules active");
    }

    @Override
    public void onDisable() {
        // Disable in reverse order
        for (int i = modules.size() - 1; i >= 0; i--) {
            modules.get(i).onDisable();
        }
        modules.clear();
        log.info("[NoLagg] Disabled");
    }

    public TpsMonitorModule getTpsMonitor() {
        return tpsMonitor;
    }

    public EntityClearModule getEntityClear() {
        return entityClear;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!command.getName().equalsIgnoreCase("lag")) return false;

        if (!sender.isOp()) {
            sender.sendMessage("\u00A7cOnly operators can use this command.");
            return true;
        }

        if (args.length == 0) {
            tpsMonitor.showOverview(sender);
            return true;
        }

        String sub = args[0].toLowerCase();
        String[] subArgs = args.length > 1 ? Arrays.copyOfRange(args, 1, args.length) : new String[0];

        if (sub.equals("mem") || sub.equals("memory")) {
            tpsMonitor.showMemory(sender);
        } else if (sub.equals("stats")) {
            tpsMonitor.showStats(sender);
        } else if (sub.equals("gc")) {
            tpsMonitor.runGc(sender);
        } else if (sub.equals("clear")) {
            if (!(sender instanceof Player)) {
                sender.sendMessage("\u00A7cUse /lag clearall from console.");
                return true;
            }
            entityClear.handleClear(sender, subArgs, false);
        } else if (sub.equals("clearall")) {
            entityClear.handleClear(sender, subArgs, true);
        } else if (sub.equals("tnt")) {
            tntBuffer.handleCommand(sender, subArgs);
        } else {
            sender.sendMessage("\u00A7e--- NoLagg Commands ---");
            sender.sendMessage("\u00A7f/lag \u00A77- TPS, memory, entity overview");
            sender.sendMessage("\u00A7f/lag mem \u00A77- Detailed memory info");
            sender.sendMessage("\u00A7f/lag stats \u00A77- Entity breakdown by world");
            sender.sendMessage("\u00A7f/lag gc \u00A77- Force garbage collection");
            sender.sendMessage("\u00A7f/lag clear [type] \u00A77- Clear entities (your world)");
            sender.sendMessage("\u00A7f/lag clearall [type] \u00A77- Clear entities (all worlds)");
            sender.sendMessage("\u00A7f/lag tnt \u00A77- TNT buffer status");
            sender.sendMessage("\u00A7f/lag tnt clear \u00A77- Clear TNT buffer");
            sender.sendMessage("\u00A77Types: items, mobs, animals, monsters, arrows, minecarts, all");
        }

        return true;
    }
}
