"use strict";

const Ajv = require("ajv");

const ACTION_TYPES = Object.freeze([
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

const actionSchema = {
  type: "object",
  additionalProperties: false,
  required: ["type", "arguments", "idempotency_key"],
  properties: {
    type: { enum: ACTION_TYPES },
    arguments: {
      type: "object",
      maxProperties: 16,
      additionalProperties: true,
      properties: {
        code: false,
        javascript: false,
        python: false,
        shell: false,
        raw_packet: false,
        packet: false,
      },
    },
    idempotency_key: { type: "string", minLength: 8, maxLength: 160 },
    timeout_seconds: { type: "number", exclusiveMinimum: 0, maximum: 300 },
  },
};

const ajv = new Ajv({ allErrors: true, strict: true });
const validateAction = ajv.compile(actionSchema);

function assertAction(value) {
  if (!validateAction(value)) {
    const error = new Error(`invalid action: ${ajv.errorsText(validateAction.errors)}`);
    error.code = "invalid_action";
    throw error;
  }
  return value;
}

module.exports = { ACTION_TYPES, actionSchema, assertAction };
