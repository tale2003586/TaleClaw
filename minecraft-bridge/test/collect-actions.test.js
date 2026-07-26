"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { findBlocks, collectBlocks, blockIdsForResource } = require("../src/actions/collect");
const minecraftData = require("minecraft-data");

function adapter() {
  const positions = [{ x: 1, y: 64, z: 0 }, { x: 2, y: 64, z: 0 }];
  const collected = [];
  return {
    collected,
    bot: {
      version: "1.21.1",
      findBlocks: () => positions,
      blockAt: (position) => ({ name: "oak_log", position }),
      collectBlock: { collect: async (block) => collected.push(block) },
    },
  };
}

test("resource mappings include normal and deepslate diamond ore", () => {
  const data = minecraftData("1.21.1");
  const ids = blockIdsForResource(data, "diamond");
  assert.ok(ids.includes(data.blocksByName.diamond_ore.id));
  assert.ok(ids.includes(data.blocksByName.deepslate_diamond_ore.id));
});

test("find and collect high-level actions work", async () => {
  const target = adapter();
  const found = await findBlocks(
    target,
    { resource: "oak_log", count: 2, max_distance: 64 },
    new AbortController().signal,
  );
  assert.equal(found.found, 2);
  const result = await collectBlocks(
    target,
    { resource: "oak_log", count: 2, max_distance: 64 },
    new AbortController().signal,
  );
  assert.equal(result.collected_blocks, 2);
  assert.equal(target.collected.length, 2);
});

test("cancelled signal prevents collection", async () => {
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(
    collectBlocks(adapter(), { resource: "oak_log", count: 1 }, controller.signal),
    (error) => error.code === "action_cancelled",
  );
});

test("diamond collection requires an iron-tier pickaxe", async () => {
  const target = adapter();
  target.bot.inventory = { items: () => [{ name: "stone_pickaxe" }] };
  await assert.rejects(
    collectBlocks(target, { resource: "diamond", count: 1 }, new AbortController().signal),
    (error) => error.code === "insufficient_tool_tier",
  );
  target.bot.inventory = { items: () => [{ name: "iron_pickaxe" }] };
  target.bot.blockAt = (position) => ({ name: "diamond_ore", position });
  await collectBlocks(
    target,
    { resource: "diamond", count: 1 },
    new AbortController().signal,
  );
});
