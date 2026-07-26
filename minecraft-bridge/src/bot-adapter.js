"use strict";

const mineflayer = require("mineflayer");
const { pathfinder, goals } = require("mineflayer-pathfinder");
const collectBlock = require("mineflayer-collectblock").plugin;

class BotAdapter {
  constructor(config, { factory = mineflayer.createBot } = {}) {
    this.config = config;
    this.factory = factory;
    this.bot = null;
    this.status = "disconnected";
    this.lastError = null;
  }

  async connect() {
    if (this.bot && this.status === "spawned") return this.observe();
    const options = {
      host: this.config.serverHost,
      port: this.config.serverPort,
      username: this.config.username,
      auth: "offline",
    };
    if (this.config.version) options.version = this.config.version;
    const bot = this.factory(options);
    this.bot = bot;
    if (typeof bot.loadPlugin === "function") {
      bot.loadPlugin(pathfinder);
      bot.loadPlugin(collectBlock);
    }
    this.status = "connecting";
    return new Promise((resolve, reject) => {
      const cleanup = () => {
        bot.removeListener?.("spawn", onSpawn);
        bot.removeListener?.("error", onError);
        bot.removeListener?.("kicked", onKicked);
      };
      const onSpawn = () => {
        cleanup();
        this.status = "spawned";
        this.safePosition = bot.entity?.position?.clone?.() || bot.entity?.position || null;
        this._attachLifecycle(bot);
        resolve(this.observe());
      };
      const onError = (error) => {
        cleanup();
        this.status = "error";
        this.lastError = normalizeError(error);
        reject(this.lastError);
      };
      const onKicked = (reason) => {
        cleanup();
        this.status = "kicked";
        const error = new Error(String(reason));
        error.code = "server_rejected";
        reject(error);
      };
      bot.once("spawn", onSpawn);
      bot.once("error", onError);
      bot.once("kicked", onKicked);
    });
  }

  _attachLifecycle(bot) {
    bot.on?.("end", () => {
      this.status = "disconnected";
    });
    bot.on?.("death", () => {
      this.status = "dead";
    });
    bot.on?.("kicked", (reason) => {
      this.status = "kicked";
      this.lastError = { code: "server_rejected", message: String(reason) };
    });
    bot.on?.("error", (error) => {
      this.lastError = normalizeError(error);
    });
  }

  observe({ targetBlocks = [] } = {}) {
    if (!this.bot) {
      const error = new Error("bot is not connected");
      error.code = "not_connected";
      throw error;
    }
    const bot = this.bot;
    const inventory = aggregateInventory(bot.inventory?.items?.() || []).slice(0, 128);
    const nearbyBlocks = [];
    if (targetBlocks.length && typeof bot.findBlocks === "function") {
      const positions = bot.findBlocks({
        matching: targetBlocks,
        maxDistance: 64,
        count: 128,
      }) || [];
      for (const position of positions.slice(0, 128)) {
        const block = bot.blockAt?.(position);
        nearbyBlocks.push({
          block: String(block?.name || "unknown").slice(0, 96),
          position: vector(position),
          distance: Number(bot.entity?.position?.distanceTo?.(position) || 0),
        });
      }
    }
    return {
      observed_at: new Date().toISOString(),
      connected: this.status === "spawned",
      bot_id: String(bot.username || this.config.username).slice(0, 96),
      server_id: `${this.config.serverHost}:${this.config.serverPort}`.slice(0, 256),
      world_id: String(bot.game?.dimension || "unknown").slice(0, 256),
      version: String(bot.version || this.config.version || "").slice(0, 32),
      position: vector(bot.entity?.position || { x: 0, y: 0, z: 0 }),
      dimension: String(bot.game?.dimension || "overworld").slice(0, 64),
      health: bounded(bot.health ?? 20, 0, 20),
      food: bounded(bot.food ?? 20, 0, 20),
      oxygen: bounded(bot.oxygenLevel ?? 300, 0, 300),
      inventory,
      equipment: [],
      nearby_blocks: nearbyBlocks,
      nearby_drops: [],
      hazards: { lava: false, fall: false, drowning: false, hostile_mob_count: 0 },
      current_action_id: null,
      current_action_status: null,
    };
  }

  async disconnect() {
    if (this.bot) this.bot.quit?.("TaleClaw task finished");
    this.status = "disconnected";
    this.bot = null;
  }

  async gotoPosition(position, signal) {
    if (!this.bot?.pathfinder?.goto) {
      const error = new Error("pathfinder unavailable");
      error.code = "pathfinder_unavailable";
      throw error;
    }
    if (signal?.aborted) {
      const error = new Error("action cancelled");
      error.code = "action_cancelled";
      throw error;
    }
    const onAbort = () => this.bot?.pathfinder?.stop?.();
    signal?.addEventListener?.("abort", onAbort, { once: true });
    try {
      await this.bot.pathfinder.goto(
        new goals.GoalBlock(
          Math.floor(position.x),
          Math.floor(position.y),
          Math.floor(position.z),
        ),
      );
    } finally {
      signal?.removeEventListener?.("abort", onAbort);
    }
    if (signal?.aborted) {
      const error = new Error("action cancelled");
      error.code = "action_cancelled";
      throw error;
    }
  }
}

function aggregateInventory(items) {
  const counts = new Map();
  for (const item of items) {
    const name = String(item?.name || "").slice(0, 96);
    if (!name) continue;
    counts.set(name, (counts.get(name) || 0) + Number(item.count || 0));
  }
  return [...counts.entries()].map(([item, count]) => ({ item, count }));
}

function vector(value) {
  return { x: Number(value.x || 0), y: Number(value.y || 0), z: Number(value.z || 0) };
}

function bounded(value, min, max) {
  return Math.max(min, Math.min(max, Number(value)));
}

function normalizeError(error) {
  const result = new Error(String(error?.message || error));
  result.code = error?.code === "ECONNREFUSED" ? "network_error" : "connection_error";
  return result;
}

module.exports = { BotAdapter, aggregateInventory };
