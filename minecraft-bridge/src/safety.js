"use strict";

const RESTRICTED_ACTIONS = new Set([
  "observe",
  "find_blocks",
  "collect_blocks",
  "craft",
  "smelt",
  "equip",
  "eat",
  "return_safe",
  "branch_mine",
  "cancel_action",
]);

const FORBIDDEN_ARGUMENTS = new Set([
  "attack",
  "container",
  "command",
  "explosive",
  "lava",
  "raw_packet",
  "code",
  "shell",
]);

class SafetyController {
  constructor({ mode = "restricted_resource_mode", catalog = null } = {}) {
    if (mode !== "restricted_resource_mode") {
      throw new Error(`unsupported safety mode: ${mode}`);
    }
    this.mode = mode;
    this.catalog = catalog;
  }

  assertAction(action, observation = {}) {
    if (!RESTRICTED_ACTIONS.has(action.type)) deny("action_not_allowed");
    for (const key of Object.keys(action.arguments || {})) {
      if (FORBIDDEN_ARGUMENTS.has(key.toLowerCase())) deny("unsafe_argument");
    }
    if ((observation.health ?? 20) <= 6) deny("low_health");
    if ((observation.food ?? 20) <= 4) deny("low_food");
    if ((observation.oxygen ?? 300) <= 40) deny("low_oxygen");
    if (observation.hazards?.lava) deny("lava_hazard");
    if (observation.hazards?.fall) deny("fall_hazard");
    const count = Number(action.arguments?.count || 0);
    if (count < 0 || count > 256) deny("action_count_exceeded");
    const distance = Number(action.arguments?.max_distance || 0);
    if (distance < 0 || distance > 256) deny("action_distance_exceeded");
    return true;
  }
}

function deny(code) {
  const error = new Error(code);
  error.code = code;
  throw error;
}

module.exports = { SafetyController, RESTRICTED_ACTIONS };
