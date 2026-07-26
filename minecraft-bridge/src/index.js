"use strict";

const { loadConfig } = require("./config");
const { BotAdapter } = require("./bot-adapter");
const { ActionStore } = require("./action-store");
const { SafetyController } = require("./safety");
const { createServer } = require("./server");

async function main() {
  const config = loadConfig();
  const adapter = new BotAdapter(config);
  const actionStore = new ActionStore();
  const safety = new SafetyController();
  const app = createServer({ config, adapter, actionStore, safety });
  const server = app.listen(config.port, config.host, () => {
    process.stdout.write(`Minecraft Bridge listening on ${config.host}:${config.port}\n`);
  });
  const shutdown = async () => {
    await adapter.disconnect();
    server.close(() => process.exit(0));
  };
  process.once("SIGINT", shutdown);
  process.once("SIGTERM", shutdown);
}

if (require.main === module) {
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}

module.exports = { main };
