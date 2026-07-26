"use strict";

async function branchMine(adapter, args, signal) {
  const length = bounded(args.length || 16, 1, 64, "invalid_tunnel_length");
  const blockBudget = bounded(args.block_budget || length * 2, 1, 256, "invalid_block_budget");
  const targets = new Set(Array.isArray(args.targets) ? args.targets : [args.resource]);
  let mined = 0;
  for (let step = 0; step < length && mined < blockBudget; step += 1) {
    if (signal?.aborted) throw coded("action_cancelled");
    const observation = adapter.observe();
    const unsafe = safetyReason(observation);
    if (unsafe) return { stopped: unsafe, mined_blocks: mined, found: [] };
    const outcome = typeof adapter.branchMineStep === "function"
      ? await adapter.branchMineStep({ step, targets, remaining: blockBudget - mined, signal })
      : await defaultStep(adapter, targets, signal);
    mined += Number(outcome?.mined_blocks || 0);
    const found = (outcome?.found || []).filter((name) => targets.has(name));
    if (found.length) return { stopped: "target_found", mined_blocks: mined, found };
  }
  return { stopped: mined >= blockBudget ? "block_budget_exhausted" : "length_exhausted", mined_blocks: mined, found: [] };
}

async function defaultStep(adapter, targets, signal) {
  if (signal?.aborted) throw coded("action_cancelled");
  const bot = adapter.bot;
  if (!bot || typeof bot.blockAt !== "function" || typeof bot.dig !== "function") {
    throw coded("branch_mining_unavailable");
  }
  const position = bot.entity?.position;
  if (!position || typeof position.offset !== "function") throw coded("position_unavailable");
  const blocks = [bot.blockAt(position.offset(1, 0, 0)), bot.blockAt(position.offset(1, 1, 0))]
    .filter((block) => block && block.name !== "air");
  const found = blocks.filter((block) => targets.has(block.name)).map((block) => block.name);
  if (found.length) return { mined_blocks: 0, found };
  for (const block of blocks) {
    if (signal?.aborted) throw coded("action_cancelled");
    if (["lava", "water", "bedrock"].includes(block.name)) throw coded("unsafe_block");
    await bot.dig(block);
  }
  const next = position.offset(1, 0, 0);
  if (typeof adapter.gotoPosition === "function") {
    await adapter.gotoPosition(next, signal);
  } else if (typeof bot.pathfinder?.goto === "function") {
    const { goals } = require("mineflayer-pathfinder");
    await bot.pathfinder.goto(new goals.GoalBlock(
      Math.floor(next.x),
      Math.floor(next.y),
      Math.floor(next.z),
    ));
  } else {
    throw coded("pathfinder_unavailable");
  }
  return { mined_blocks: blocks.length, found: [] };
}

function safetyReason(observation) {
  if (!observation.connected) return "disconnected";
  if ((observation.health ?? 20) <= 6) return "low_health";
  if ((observation.food ?? 20) <= 4) return "low_food";
  if ((observation.oxygen ?? 300) <= 40) return "low_oxygen";
  if (observation.hazards?.lava) return "lava_hazard";
  if (observation.hazards?.fall) return "fall_hazard";
  return "";
}

function bounded(value, min, max, code) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < min || parsed > max) throw coded(code);
  return parsed;
}

function coded(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

module.exports = { branchMine, safetyReason };
