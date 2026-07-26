"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { craft, equip, eat, returnSafe } = require("../src/actions/basic");

function fixture() {
  const items = [{ name: "oak_log", type: 1, metadata: 0, count: 4 }];
  const calls = [];
  return {
    calls,
    safePosition: { x: 1, y: 64, z: 2 },
    gotoPosition: async (position) => calls.push(["goto", position]),
    bot: {
      version: "1.21.1",
      inventory: { items: () => items },
      recipesFor: () => [{}],
      craft: async (_recipe, count) => calls.push(["craft", count]),
      equip: async (item, destination) => calls.push(["equip", item.name, destination]),
      consume: async () => calls.push(["eat"]),
    },
  };
}

test("craft equip eat and return-safe are bounded high-level actions", async () => {
  const adapter = fixture();
  const signal = new AbortController().signal;
  await craft(adapter, { item: "oak_planks", count: 1 }, signal);
  await equip(adapter, { item: "oak_log", destination: "hand" }, signal);
  await eat(adapter, {}, signal);
  await returnSafe(adapter, {}, signal);
  assert.deepEqual(adapter.calls.map((entry) => entry[0]), ["craft", "equip", "eat", "goto"]);
});

test("basic actions honor cancellation", async () => {
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(craft(fixture(), { item: "oak_planks" }, controller.signal), {
    code: "action_cancelled",
  });
});
