"use strict";

const express = require("express");
const { assertAction } = require("./schemas");
const { findBlocks, collectBlocks } = require("./actions/collect");
const { craft, smelt, equip, eat, returnSafe } = require("./actions/basic");
const { branchMine } = require("./actions/branch-mine");

function createServer({ config, adapter, actionStore, safety }) {
  const app = express();
  const requestWindows = new Map();
  app.disable("x-powered-by");
  app.use(express.json({ limit: config.maxBodyBytes }));
  app.use((request, response, next) => {
    if (request.path === "/health") return next();
    const client = normalizeAddress(request.ip || request.socket?.remoteAddress || "");
    if (
      Array.isArray(config.trustedClients)
      && config.trustedClients.length
      && !config.trustedClients.some((entry) => addressMatches(client, entry))
    ) {
      return response.status(403).json({ error: "client_not_allowed" });
    }
    const now = Date.now();
    const window = requestWindows.get(client) || { started: now, count: 0 };
    if (now - window.started >= 60000) {
      window.started = now;
      window.count = 0;
    }
    window.count += 1;
    requestWindows.set(client, window);
    if (window.count > Number(config.requestsPerMinute || 240)) {
      return response.status(429).json({ error: "rate_limited" });
    }
    if (request.get("authorization") !== `Bearer ${config.token}`) {
      return response.status(401).json({ error: "unauthorized" });
    }
    return next();
  });

  app.get("/health", (_request, response) => {
    response.json({ ok: true, status: adapter.status });
  });
  app.post("/v1/bot/connect", async (_request, response) => {
    await route(response, async () => adapter.connect());
  });
  app.post("/v1/bot/disconnect", async (_request, response) => {
    await route(response, async () => {
      await adapter.disconnect();
      return { disconnected: true };
    });
  });
  app.get("/v1/bot/state", async (_request, response) => {
    await route(response, async () => adapter.observe());
  });
  app.post("/v1/actions", async (request, response) => {
    await route(response, async () => {
      const action = assertAction(request.body);
      const observation = adapter.observe();
      safety.assertAction(action, observation);
      const created = actionStore.create(observation.bot_id, action);
      execute(created.action_id, action, { adapter, actionStore, safety });
      return created;
    }, 202);
  });
  app.get("/v1/actions/:actionId", async (request, response) => {
    await route(response, async () => actionStore.get(request.params.actionId));
  });
  app.post("/v1/actions/:actionId/cancel", async (request, response) => {
    await route(response, async () => actionStore.cancel(request.params.actionId));
  });

  app.use((error, _request, response, _next) => {
    const code = error.type === "entity.too.large" ? "payload_too_large" : "invalid_json";
    response.status(error.type === "entity.too.large" ? 413 : 400).json({ error: code });
  });
  return app;
}

function normalizeAddress(value) {
  return String(value).replace(/^::ffff:/, "");
}

function addressMatches(address, rule) {
  if (address === rule) return true;
  const [network, prefixText] = String(rule).split("/");
  if (!prefixText || !address.includes(".") || !network.includes(".")) return false;
  const prefix = Number(prefixText);
  if (!Number.isInteger(prefix) || prefix < 0 || prefix > 32) return false;
  const addressValue = ipv4Value(address);
  const networkValue = ipv4Value(network);
  if (addressValue === null || networkValue === null) return false;
  const mask = prefix === 0 ? 0 : (0xffffffff << (32 - prefix)) >>> 0;
  return (addressValue & mask) === (networkValue & mask);
}

function ipv4Value(value) {
  const octets = String(value).split(".").map(Number);
  if (
    octets.length !== 4
    || octets.some((part) => !Number.isInteger(part) || part < 0 || part > 255)
  ) return null;
  return (
    ((octets[0] << 24) >>> 0)
    + (octets[1] << 16)
    + (octets[2] << 8)
    + octets[3]
  ) >>> 0;
}

async function execute(actionId, request, { adapter, actionStore, safety }) {
  const internal = actionStore.internal(actionId);
  try {
    actionStore.transition(actionId, "running");
    safety.assertAction(request, adapter.observe());
    if (request.type === "find_blocks") {
      await findBlocks(adapter, request.arguments, internal.controller.signal);
    } else if (request.type === "collect_blocks") {
      await collectBlocks(adapter, request.arguments, internal.controller.signal);
    } else if (request.type === "craft") {
      await craft(adapter, request.arguments, internal.controller.signal);
    } else if (request.type === "smelt") {
      await smelt(adapter, request.arguments, internal.controller.signal);
    } else if (request.type === "equip") {
      await equip(adapter, request.arguments, internal.controller.signal);
    } else if (request.type === "eat") {
      await eat(adapter, request.arguments, internal.controller.signal);
    } else if (request.type === "return_safe") {
      await returnSafe(adapter, request.arguments, internal.controller.signal);
    } else if (request.type === "branch_mine") {
      await branchMine(adapter, request.arguments, internal.controller.signal);
    } else if (request.type !== "observe") {
      throw coded("action_not_implemented");
    }
    if (!internal.controller.signal.aborted) actionStore.transition(actionId, "succeeded", { progress: 1 });
  } catch (error) {
    if (internal.controller.signal.aborted || error.code === "action_cancelled") {
      if (!["cancelled", "succeeded", "failed"].includes(internal.status)) {
        actionStore.transition(actionId, "cancelled");
      }
      return;
    }
    if (!["cancelled", "succeeded", "failed"].includes(internal.status)) {
      actionStore.transition(actionId, "failed", {
        error_code: String(error.code || "action_failed").slice(0, 96),
        message: String(error.message || error).slice(0, 500),
      });
    }
  }
}

async function route(response, operation, successStatus = 200) {
  try {
    response.status(successStatus).json(await operation());
  } catch (error) {
    const status = error.code === "action_not_found" ? 404
      : error.code === "action_conflict" ? 409
        : 400;
    response.status(status).json({
      error: String(error.code || "bridge_error").slice(0, 96),
      message: String(error.message || error).slice(0, 500),
    });
  }
}

function coded(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

module.exports = { createServer, execute, normalizeAddress, addressMatches };
