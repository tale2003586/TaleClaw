"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { addressMatches, normalizeAddress } = require("../src/server");

test("trusted client matching supports exact addresses and bounded IPv4 CIDR", () => {
  assert.equal(normalizeAddress("::ffff:172.18.0.4"), "172.18.0.4");
  assert.equal(addressMatches("127.0.0.1", "127.0.0.1"), true);
  assert.equal(addressMatches("172.18.0.4", "172.16.0.0/12"), true);
  assert.equal(addressMatches("10.0.0.4", "172.16.0.0/12"), false);
  assert.equal(addressMatches("10.0.0.4", "invalid/12"), false);
});
