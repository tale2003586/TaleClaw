"use strict";

const { randomUUID } = require("node:crypto");

const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);
const TRANSITIONS = {
  pending: new Set(["running", "cancelled", "failed"]),
  running: new Set(["succeeded", "failed", "cancelled"]),
};

class ActionStore {
  constructor() {
    this.actions = new Map();
    this.byKey = new Map();
    this.activeByBot = new Map();
  }

  create(botId, request) {
    const existingId = this.byKey.get(request.idempotency_key);
    if (existingId) return this.get(existingId);
    const active = this.activeByBot.get(botId);
    if (active) {
      const error = new Error(`bot already has active action ${active}`);
      error.code = "action_conflict";
      throw error;
    }
    const action = {
      action_id: `action-${randomUUID()}`,
      bot_id: botId,
      request: structuredClone(request),
      status: "pending",
      progress: 0,
      error_code: null,
      message: "",
      controller: new AbortController(),
    };
    this.actions.set(action.action_id, action);
    this.byKey.set(request.idempotency_key, action.action_id);
    this.activeByBot.set(botId, action.action_id);
    return publicAction(action);
  }

  get(actionId) {
    const action = this.actions.get(actionId);
    if (!action) {
      const error = new Error("action not found");
      error.code = "action_not_found";
      throw error;
    }
    return publicAction(action);
  }

  internal(actionId) {
    const action = this.actions.get(actionId);
    if (!action) {
      const error = new Error("action not found");
      error.code = "action_not_found";
      throw error;
    }
    return action;
  }

  transition(actionId, status, patch = {}) {
    const action = this.internal(actionId);
    if (action.status === status) return publicAction(action);
    if (TERMINAL.has(action.status) || !TRANSITIONS[action.status]?.has(status)) {
      const error = new Error(`illegal action transition ${action.status} -> ${status}`);
      error.code = "illegal_action_transition";
      throw error;
    }
    Object.assign(action, patch, { status });
    if (TERMINAL.has(status)) this.activeByBot.delete(action.bot_id);
    return publicAction(action);
  }

  cancel(actionId) {
    const action = this.internal(actionId);
    if (TERMINAL.has(action.status)) return publicAction(action);
    action.controller.abort();
    return this.transition(actionId, "cancelled");
  }
}

function publicAction(action) {
  return {
    action_id: action.action_id,
    status: action.status,
    progress: action.progress,
    error_code: action.error_code,
    message: action.message,
  };
}

module.exports = { ActionStore };
