"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { ActionStore } = require("../src/action-store");

function request(key = "action-0001") {
  return { type: "observe", arguments: {}, idempotency_key: key };
}

test("action creation is idempotent and enforces one active action", () => {
  const store = new ActionStore();
  const first = store.create("bot", request());
  assert.equal(store.create("bot", request()).action_id, first.action_id);
  assert.throws(() => store.create("bot", request("action-0002")), /active action/);
  store.transition(first.action_id, "running");
  store.transition(first.action_id, "succeeded");
  assert.equal(store.create("bot", request("action-0002")).status, "pending");
});

test("cancel aborts once and terminal state cannot regress", () => {
  const store = new ActionStore();
  const action = store.create("bot", request());
  assert.equal(store.cancel(action.action_id).status, "cancelled");
  assert.equal(store.cancel(action.action_id).status, "cancelled");
  assert.throws(() => store.transition(action.action_id, "running"), /illegal/);
});
