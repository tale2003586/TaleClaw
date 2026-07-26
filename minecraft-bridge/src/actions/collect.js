"use strict";

const minecraftData = require("minecraft-data");

async function findBlocks(adapter, args, signal) {
  abortIfNeeded(signal);
  const bot = requireBot(adapter);
  const data = minecraftData(bot.version);
  const blockIds = blockIdsForResource(data, args.resource);
  const positions = bot.findBlocks({
    matching: blockIds,
    maxDistance: Number(args.max_distance || 64),
    count: Number(args.count || 1),
  }) || [];
  return { found: positions.length, positions: positions.slice(0, 128).map(vector) };
}

async function collectBlocks(adapter, args, signal) {
  abortIfNeeded(signal);
  const bot = requireBot(adapter);
  const data = minecraftData(bot.version);
  assertToolTier(bot, args.resource);
  const blockIds = blockIdsForResource(data, args.resource);
  const positions = bot.findBlocks({
    matching: blockIds,
    maxDistance: Number(args.max_distance || 64),
    count: Number(args.count || 1),
  }) || [];
  if (!positions.length) throw coded("resource_not_found");
  const blocks = positions
    .slice(0, Number(args.count || 1))
    .map((position) => bot.blockAt(position))
    .filter(Boolean);
  for (const block of blocks) {
    abortIfNeeded(signal);
    await bot.collectBlock.collect(block, { ignoreNoPath: false });
  }
  return { collected_blocks: blocks.length };
}

function assertToolTier(bot, resource) {
  const required = { cobblestone: 1, coal: 1, raw_iron: 2, diamond: 3 }[resource] || 0;
  if (!required) return true;
  const tiers = {
    wooden_pickaxe: 1,
    stone_pickaxe: 2,
    iron_pickaxe: 3,
    diamond_pickaxe: 4,
    netherite_pickaxe: 5,
  };
  const items = bot.inventory?.items?.() || [];
  const best = items.reduce((tier, item) => Math.max(tier, tiers[item?.name] || 0), 0);
  if (best < required) throw coded("insufficient_tool_tier");
  return true;
}

function blockIdsForResource(data, resource) {
  const names = {
    oak_log: [
      "oak_log",
      "birch_log",
      "spruce_log",
      "jungle_log",
      "acacia_log",
      "dark_oak_log",
      "mangrove_log",
      "cherry_log",
    ],
    cobblestone: ["stone"],
    coal: ["coal_ore", "deepslate_coal_ore"],
    raw_iron: ["iron_ore", "deepslate_iron_ore"],
    diamond: ["diamond_ore", "deepslate_diamond_ore"],
  }[resource];
  if (!names) throw coded("unknown_resource");
  const ids = names.map((name) => data.blocksByName[name]?.id).filter(Number.isInteger);
  if (!ids.length) throw coded("unsupported_resource_version");
  return ids;
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

function vector(position) {
  return { x: Number(position.x), y: Number(position.y), z: Number(position.z) };
}

module.exports = { findBlocks, collectBlocks, blockIdsForResource, assertToolTier };
