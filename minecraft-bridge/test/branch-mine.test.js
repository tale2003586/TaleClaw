"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { branchMine } = require("../src/actions/branch-mine");

const safe = {
  connected: true,
  health: 20,
  food: 20,
  oxygen: 300,
  hazards: {},
};

test("branch mining stops immediately when target is found", async () => {
  const adapter = {
    observe: () => safe,
    branchMineStep: async () => ({ mined_blocks: 2, found: ["diamond"] }),
  };
  const result = await branchMine(
    adapter,
    { resource: "diamond", length: 16, block_budget: 32 },
    new AbortController().signal,
  );
  assert.equal(result.stopped, "target_found");
  assert.deepEqual(result.found, ["diamond"]);
});

test("branch mining stops for hazards, budget, and cancellation", async () => {
  const hazardous = {
    observe: () => ({ ...safe, hazards: { lava: true } }),
    branchMineStep: async () => ({ mined_blocks: 2 }),
  };
  assert.equal(
    (await branchMine(hazardous, { resource: "diamond" }, new AbortController().signal)).stopped,
    "lava_hazard",
  );
  const bounded = {
    observe: () => safe,
    branchMineStep: async () => ({ mined_blocks: 2 }),
  };
  assert.equal(
    (await branchMine(
      bounded,
      { resource: "diamond", length: 8, block_budget: 2 },
      new AbortController().signal,
    )).stopped,
    "block_budget_exhausted",
  );
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(
    branchMine(bounded, { resource: "diamond" }, controller.signal),
    { code: "action_cancelled" },
  );
});
