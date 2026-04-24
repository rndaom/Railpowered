package com.betaserver.nolagg;

class LagCommandModule extends NoLaggModule {

    private static final double WARNING_TPS = 15.0;
    private static final double CRITICAL_TPS = 10.0;
    private static final long CHECK_INTERVAL = 100; // 5 seconds
    private static final long COOLDOWN_MS = 60000;  // 60 seconds

    private final TpsMonitorModule tpsMonitor;
    private final EntityClearModule entityClear;

    private static final long STARTUP_GRACE_MS = 120000; // 2 minutes

    private long lastWarningTime = 0;
    private long lastClearTime = 0;
    private long enabledTime = 0;
    private int taskId = -1;

    LagCommandModule(NoLaggPlugin plugin, TpsMonitorModule tpsMonitor, EntityClearModule entityClear) {
        super(plugin);
        this.tpsMonitor = tpsMonitor;
        this.entityClear = entityClear;
    }

    @Override
    void onEnable() {
        enabledTime = System.currentTimeMillis();
        taskId = plugin.getServer().getScheduler().scheduleSyncRepeatingTask(plugin, new Runnable() {
            @Override
            public void run() {
                checkTps();
            }
        }, CHECK_INTERVAL, CHECK_INTERVAL);
    }

    @Override
    void onDisable() {
        if (taskId != -1) {
            plugin.getServer().getScheduler().cancelTask(taskId);
            taskId = -1;
        }
    }

    private void checkTps() {
        long now = System.currentTimeMillis();

        // Don't trigger during startup — TPS is naturally low while chunks generate
        if (now - enabledTime < STARTUP_GRACE_MS) return;

        double tps = tpsMonitor.getTps();

        if (tps < CRITICAL_TPS && (now - lastClearTime) > COOLDOWN_MS) {
            lastClearTime = now;
            lastWarningTime = now;
            int cleared = entityClear.clearItemsAllWorlds();
            plugin.getServer().broadcastMessage(
                "\u00A7c[NoLagg] \u00A7fLow TPS (\u00A7c" + String.format("%.1f", tps)
                + "\u00A7f) - auto-cleared \u00A7e" + cleared + " \u00A7fground items."
            );
        } else if (tps < WARNING_TPS && (now - lastWarningTime) > COOLDOWN_MS) {
            lastWarningTime = now;
            plugin.getServer().broadcastMessage(
                "\u00A7e[NoLagg] \u00A7fServer is lagging (TPS: \u00A7e"
                + String.format("%.1f", tps) + "\u00A7f). Please be patient."
            );
        }
    }
}
