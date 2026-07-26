"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { SafetyController } = require("../src/safety");

const safety = new SafetyController();
const safeObservation = { health: 20, food: 20, oxygen: 300, hazards: {} };

test("restricted resource action passes", () => {
  assert.equal(
    safety.assertAction(
      { type: "collect_blocks", arguments: { resource: "oak_log", count: 4 } },
      safeObservation,
    ),
    true,
  );
});

test("dangerous actions, arguments, hazards and budgets fail", () => {
  assert.throws(() => safety.assertAction({ type: "attack", arguments: {} }, safeObservation));
  assert.throws(() => safety.assertAction(
    { type: "collect_blocks", arguments: { command: "/op me" } },
    safeObservation,
  ));
  assert.throws(() => safety.assertAction(
    { type: "collect_blocks", arguments: { count: 4 } },
    { ...safeObservation, hazards: { lava: true } },
  ));
  assert.throws(() => safety.assertAction(
    { type: "collect_blocks", arguments: { count: 257 } },
    safeObservation,
  ));
});
