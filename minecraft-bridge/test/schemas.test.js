"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { assertAction } = require("../src/schemas");

test("valid high-level action passes", () => {
  const action = {
    type: "collect_blocks",
    arguments: { resource: "oak_log", count: 4 },
    idempotency_key: "collect-0001",
  };
  assert.equal(assertAction(action), action);
});

test("code, raw packet, unknown action and extra root fields fail", () => {
  for (const action of [
    { type: "collect_blocks", arguments: { code: "x" }, idempotency_key: "12345678" },
    { type: "collect_blocks", arguments: { raw_packet: {} }, idempotency_key: "12345678" },
    { type: "attack", arguments: {}, idempotency_key: "12345678" },
    { type: "observe", arguments: {}, idempotency_key: "12345678", extra: true },
  ]) {
    assert.throws(() => assertAction(action));
  }
});
