package com.betaserver.nolagg;

import java.util.Map;
import java.util.logging.Logger;

class ThreadWatchdogModule extends NoLaggModule {

    private static final Logger log = Logger.getLogger("Minecraft.NoLagg");
    private static final long FREEZE_THRESHOLD_MS = 10000;
    private static final long CHECK_INTERVAL_MS = 5000;

    private volatile long lastTickTime;
    private volatile boolean running;
    private Thread watchdogThread;
    private int taskId = -1;

    ThreadWatchdogModule(NoLaggPlugin plugin) {
        super(plugin);
    }

    @Override
    void onEnable() {
        lastTickTime = System.currentTimeMillis();
        running = true;

        // Update tick timestamp every tick
        taskId = plugin.getServer().getScheduler().scheduleSyncRepeatingTask(plugin, new Runnable() {
            @Override
            public void run() {
                lastTickTime = System.currentTimeMillis();
            }
        }, 1L, 1L);

        // Background watchdog thread
        watchdogThread = new Thread(new Runnable() {
            @Override
            public void run() {
                boolean alerted = false;
                while (running) {
                    try {
                        Thread.sleep(CHECK_INTERVAL_MS);
                    } catch (InterruptedException e) {
                        break;
                    }

                    if (!running) break;

                    long elapsed = System.currentTimeMillis() - lastTickTime;
                    if (elapsed > FREEZE_THRESHOLD_MS) {
                        if (!alerted) {
                            alerted = true;
                            long seconds = elapsed / 1000;
                            log.severe("[NoLagg] Server thread has not responded for " + seconds + " seconds!");
                            logMainThreadStackTrace();
                        }
                    } else {
                        alerted = false;
                    }
                }
            }
        }, "NoLagg Watchdog");
        watchdogThread.setDaemon(true);
        watchdogThread.start();
    }

    @Override
    void onDisable() {
        running = false;
        if (taskId != -1) {
            plugin.getServer().getScheduler().cancelTask(taskId);
            taskId = -1;
        }
        if (watchdogThread != null) {
            watchdogThread.interrupt();
            watchdogThread = null;
        }
    }

    private void logMainThreadStackTrace() {
        for (Map.Entry<Thread, StackTraceElement[]> entry : Thread.getAllStackTraces().entrySet()) {
            Thread thread = entry.getKey();
            if (thread.getName().equals("Server thread") || thread.getName().equals("main")) {
                log.severe("[NoLagg] Main thread stack trace:");
                for (StackTraceElement frame : entry.getValue()) {
                    log.severe("[NoLagg]   at " + frame.toString());
                }
                return;
            }
        }
        log.severe("[NoLagg] Could not find main server thread for stack trace.");
    }
}
