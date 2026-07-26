"use strict";

const net = require("node:net");

function loadConfig(env = process.env) {
  const host = String(env.MINECRAFT_BRIDGE_HOST || "127.0.0.1").trim();
  const port = integer(env.MINECRAFT_BRIDGE_PORT || "8765", "MINECRAFT_BRIDGE_PORT", 1, 65535);
  const token = String(env.MINECRAFT_BRIDGE_TOKEN || "").trim();
  const serverHost = String(env.MINECRAFT_SERVER_HOST || "127.0.0.1").trim();
  const serverPort = integer(env.MINECRAFT_SERVER_PORT || "25565", "MINECRAFT_SERVER_PORT", 1, 65535);
  const username = String(env.MINECRAFT_BOT_USERNAME || "TaleClawBot").trim();
  const version = String(env.MINECRAFT_SERVER_VERSION || "").trim() || undefined;
  const auth = String(env.MINECRAFT_AUTH_MODE || "offline").trim().toLowerCase();
  const trustRemote = boolean(env.MINECRAFT_BRIDGE_TRUST_REMOTE);
  const trustedClients = String(env.MINECRAFT_BRIDGE_TRUSTED_CLIENTS || "")
    .split(",").map((value) => value.trim()).filter(Boolean);
  const requestsPerMinute = integer(
    env.MINECRAFT_BRIDGE_REQUESTS_PER_MINUTE || "240",
    "MINECRAFT_BRIDGE_REQUESTS_PER_MINUTE",
    10,
    10000,
  );
  const maxBodyBytes = integer(
    env.MINECRAFT_BRIDGE_MAX_BODY_BYTES || "16384",
    "MINECRAFT_BRIDGE_MAX_BODY_BYTES",
    1024,
    1048576,
  );

  if (!token) throw new Error("MINECRAFT_BRIDGE_TOKEN is required");
  if (!username) throw new Error("MINECRAFT_BOT_USERNAME is required");
  if (auth !== "offline") throw new Error("only offline authentication is supported");
  if (!isLoopback(host) && !trustRemote) {
    throw new Error("remote Bridge binding requires MINECRAFT_BRIDGE_TRUST_REMOTE=1");
  }
  if (!isLoopback(host) && !trustedClients.length) {
    throw new Error("remote Bridge binding requires MINECRAFT_BRIDGE_TRUSTED_CLIENTS");
  }
  return Object.freeze({
    host,
    port,
    token,
    serverHost,
    serverPort,
    username,
    version,
    auth,
    maxBodyBytes,
    trustedClients,
    requestsPerMinute,
  });
}

function integer(value, name, min, max) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < min || parsed > max) {
    throw new Error(`${name} must be an integer between ${min} and ${max}`);
  }
  return parsed;
}

function boolean(value) {
  return ["1", "true", "yes", "on"].includes(String(value || "").trim().toLowerCase());
}

function isLoopback(host) {
  const normalized = String(host).toLowerCase();
  return normalized === "localhost" || normalized === "127.0.0.1" || normalized === "::1";
}

module.exports = { loadConfig, isLoopback };
