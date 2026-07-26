"use strict";

async function craft(adapter, args, signal) {
  abortIfNeeded(signal);
  const bot = requireBot(adapter);
  const item = String(args.item || "");
  const count = boundedCount(args.count);
  const data = require("minecraft-data")(bot.version);
  const itemType = data.itemsByName[item]?.id;
  if (!Number.isInteger(itemType)) throw coded("unknown_item");
  const recipes = bot.recipesFor?.(itemType, null, count, null) || [];
  if (!recipes.length) throw coded("missing_ingredients");
  await bot.craft(recipes[0], count, null);
  return { item, count };
}

async function equip(adapter, args, signal) {
  abortIfNeeded(signal);
  const bot = requireBot(adapter);
  const item = (bot.inventory?.items?.() || []).find(
    (entry) => entry.name === String(args.item || ""),
  );
  if (!item) throw coded("item_not_found");
  await bot.equip(item, String(args.destination || "hand"));
  return { equipped: item.name };
}

async function eat(adapter, _args, signal) {
  abortIfNeeded(signal);
  const bot = requireBot(adapter);
  if (typeof bot.consume !== "function") throw coded("food_not_available");
  await bot.consume();
  return { consumed: true };
}

async function returnSafe(adapter, _args, signal) {
  abortIfNeeded(signal);
  const bot = requireBot(adapter);
  const target = adapter.safePosition;
  if (!target) throw coded("safe_position_unknown");
  if (typeof adapter.gotoPosition === "function") {
    await adapter.gotoPosition(target, signal);
  } else if (typeof bot.pathfinder?.goto === "function") {
    const { goals } = require("mineflayer-pathfinder");
    await bot.pathfinder.goto(new goals.GoalNear(target.x, target.y, target.z, 1));
  } else {
    throw coded("pathfinder_unavailable");
  }
  return { position: { x: target.x, y: target.y, z: target.z } };
}

async function smelt(adapter, args, signal) {
  abortIfNeeded(signal);
  const bot = requireBot(adapter);
  const count = boundedCount(args.count);
  const data = require("minecraft-data")(bot.version);
  const furnaceId = data.blocksByName.furnace?.id;
  const positions = bot.findBlocks?.({ matching: [furnaceId], maxDistance: 16, count: 1 }) || [];
  if (!positions.length || typeof bot.openFurnace !== "function") {
    throw coded("furnace_not_found");
  }
  const furnace = await bot.openFurnace(bot.blockAt(positions[0]));
  try {
    const input = inventoryItem(bot, String(args.input || ""));
    const fuel = inventoryItem(bot, String(args.fuel || "coal"));
    if (!input || input.count < count || !fuel) throw coded("missing_smelting_materials");
    await furnace.putInput(input.type, input.metadata, count);
    await furnace.putFuel(fuel.type, fuel.metadata, Math.max(1, Math.ceil(count / 8)));
    if (typeof adapter.waitForSmelt === "function") {
      await adapter.waitForSmelt(furnace, count, signal);
    }
    abortIfNeeded(signal);
    const output = furnace.outputItem?.();
    if (!output || output.count < count) throw coded("smelting_incomplete");
    await furnace.takeOutput();
    return { output: output.name, count };
  } finally {
    furnace.close?.();
  }
}

function inventoryItem(bot, name) {
  return (bot.inventory?.items?.() || []).find((item) => item.name === name);
}

function boundedCount(value) {
  const count = Number(value || 1);
  if (!Number.isInteger(count) || count < 1 || count > 64) throw coded("invalid_count");
  return count;
}

function requireBot(adapter) {
  if (!adapter.bot) throw coded("not_connected");
  return adapter.bot;
}

function abortIfNeeded(signal) {
  if (signal?.aborted) throw coded("action_cancelled");
}

function coded(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

module.exports = { craft, smelt, equip, eat, returnSafe };
