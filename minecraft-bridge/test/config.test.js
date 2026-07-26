"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { loadConfig } = require("../src/config");

test("safe defaults use loopback, offline auth, and automatic version", () => {
  const config = loadConfig({ MINECRAFT_BRIDGE_TOKEN: "test-token" });
  assert.equal(config.host, "127.0.0.1");
  assert.equal(config.auth, "offline");
  assert.equal(config.version, undefined);
});

test("remote bind, empty token, and online auth are rejected", () => {
  assert.throws(() => loadConfig({}), /TOKEN/);
  assert.throws(
    () => loadConfig({ MINECRAFT_BRIDGE_TOKEN: "x", MINECRAFT_BRIDGE_HOST: "0.0.0.0" }),
    /TRUST_REMOTE/,
  );
  assert.throws(
    () => loadConfig({
      MINECRAFT_BRIDGE_TOKEN: "x",
      MINECRAFT_BRIDGE_HOST: "0.0.0.0",
      MINECRAFT_BRIDGE_TRUST_REMOTE: "1",
    }),
    /TRUSTED_CLIENTS/,
  );
  assert.deepEqual(
    loadConfig({
      MINECRAFT_BRIDGE_TOKEN: "x",
      MINECRAFT_BRIDGE_HOST: "0.0.0.0",
      MINECRAFT_BRIDGE_TRUST_REMOTE: "1",
      MINECRAFT_BRIDGE_TRUSTED_CLIENTS: "10.0.0.2",
    }).trustedClients,
    ["10.0.0.2"],
  );
  assert.throws(
    () => loadConfig({ MINECRAFT_BRIDGE_TOKEN: "x", MINECRAFT_AUTH_MODE: "microsoft" }),
    /offline/,
  );
});
