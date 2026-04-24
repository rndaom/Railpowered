package com.betaserver.nolagg;

/**
 * Base class for all NoLagg modules.
 */
abstract class NoLaggModule {

    protected final NoLaggPlugin plugin;

    NoLaggModule(NoLaggPlugin plugin) {
        this.plugin = plugin;
    }

    abstract void onEnable();

    abstract void onDisable();
}
