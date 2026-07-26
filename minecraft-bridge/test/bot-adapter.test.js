"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { BotAdapter } = require("../src/bot-adapter");

function config(overrides = {}) {
  return {
    serverHost: "127.0.0.1",
    serverPort: 25565,
    username: "TestBot",
    version: undefined,
    ...overrides,
  };
}

function fakeBot() {
  const bot = new EventEmitter();
  bot.username = "TestBot";
  bot.version = "1.21.1";
  bot.health = 20;
  bot.food = 20;
  bot.oxygenLevel = 300;
  bot.game = { dimension: "overworld" };
  bot.entity = { position: { x: 1, y: 64, z: 2, distanceTo: () => 3 } };
  bot.inventory = { items: () => [{ name: "oak_log", count: 2 }, { name: "oak_log", count: 2 }] };
  bot.loadPlugin = () => {};
  bot.findBlocks = () => Array.from({ length: 200 }, (_, index) => ({ x: index, y: 64, z: 0 }));
  bot.blockAt = () => ({ name: "oak_log" });
  bot.quit = () => {};
  return bot;
}

test("connection omits version for auto negotiation and returns bounded observation", async () => {
  let received;
  const bot = fakeBot();
  const adapter = new BotAdapter(config(), {
    factory: (options) => {
      received = options;
      queueMicrotask(() => bot.emit("spawn"));
      return bot;
    },
  });
  const observation = await adapter.connect();
  assert.equal(received.version, undefined);
  assert.equal(observation.version, "1.21.1");
  assert.deepEqual(observation.inventory, [{ item: "oak_log", count: 4 }]);
  const bounded = adapter.observe({ targetBlocks: [1] });
  assert.equal(bounded.nearby_blocks.length, 128);
  await adapter.disconnect();
});

test("connection error is normalized", async () => {
  const bot = fakeBot();
  const adapter = new BotAdapter(config(), {
    factory: () => {
      queueMicrotask(() => {
        const error = new Error("refused");
        error.code = "ECONNREFUSED";
        bot.emit("error", error);
      });
      return bot;
    },
  });
  await assert.rejects(adapter.connect(), (error) => error.code === "network_error");
});
