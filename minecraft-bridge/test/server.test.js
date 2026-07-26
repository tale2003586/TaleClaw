"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createServer } = require("../src/server");
const { ActionStore } = require("../src/action-store");
const { SafetyController } = require("../src/safety");

function fixture() {
  const observation = {
    connected: true,
    bot_id: "bot",
    position: { x: 0, y: 64, z: 0 },
    health: 20,
    food: 20,
    oxygen: 300,
    hazards: {},
  };
  const adapter = {
    status: "spawned",
    observe: () => observation,
    connect: async () => observation,
    disconnect: async () => {},
    bot: {
      version: "1.21.1",
      findBlocks: () => [],
      collectBlock: { collect: async () => {} },
    },
  };
  const config = { token: "secret", maxBodyBytes: 16384 };
  return {
    app: createServer({
      config,
      adapter,
      actionStore: new ActionStore(),
      safety: new SafetyController(),
    }),
    config,
  };
}

async function withServer(operation) {
  const { app, config } = fixture();
  const server = app.listen(0, "127.0.0.1");
  await new Promise((resolve) => server.once("listening", resolve));
  const port = server.address().port;
  try {
    await operation(`http://127.0.0.1:${port}`, config);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

test("health is public and bot state requires bearer auth", async () => {
  await withServer(async (base, config) => {
    assert.equal((await fetch(`${base}/health`)).status, 200);
    assert.equal((await fetch(`${base}/v1/bot/state`)).status, 401);
    const response = await fetch(`${base}/v1/bot/state`, {
      headers: { authorization: `Bearer ${config.token}` },
    });
    assert.equal(response.status, 200);
    assert.equal((await response.json()).bot_id, "bot");
  });
});

test("action endpoint validates schema and deduplicates", async () => {
  await withServer(async (base, config) => {
    const body = {
      type: "find_blocks",
      arguments: { resource: "oak_log", count: 1 },
      idempotency_key: "find-00001",
    };
    const request = () => fetch(`${base}/v1/actions`, {
      method: "POST",
      headers: {
        authorization: `Bearer ${config.token}`,
        "content-type": "application/json",
      },
      body: JSON.stringify(body),
    });
    const first = await (await request()).json();
    const second = await (await request()).json();
    assert.equal(first.action_id, second.action_id);
  });
});
