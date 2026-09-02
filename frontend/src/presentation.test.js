import assert from "node:assert/strict";
import test from "node:test";

import { formatTime, shortSha, statusTone } from "./presentation.js";

test("presentation helpers provide stable fallbacks", () => {
  assert.equal(formatTime(null), "not recorded");
  assert.equal(shortSha(null), "unknown");
  assert.equal(shortSha("1234567890abcdef"), "1234567890ab");
});

test("statusTone groups lifecycle states", () => {
  assert.equal(statusTone("running"), "success");
  assert.equal(statusTone("building"), "warning");
  assert.equal(statusTone("cancelled"), "warning");
  assert.equal(statusTone("failed"), "danger");
  assert.equal(statusTone("stopped"), "neutral");
});
