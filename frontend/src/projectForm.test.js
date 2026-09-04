import assert from "node:assert/strict";
import test from "node:test";

import { blankProjectForm, projectPayload, projectToForm, syncEnvVarSecretFlag } from "./projectForm.js";

test("blankProjectForm returns independent state", () => {
  const first = blankProjectForm();
  const second = blankProjectForm();
  first.env_vars.push({ name: "A" });
  assert.equal(second.env_vars.length, 0);
});

test("projectToForm hides a redacted secret literal", () => {
  const form = projectToForm({ env_vars: [{ name: "TOKEN", value: "[REDACTED]", is_secret: true }] });
  assert.equal(form.env_vars[0].value, "");
  assert.equal(form.env_vars[0].is_secret, true);
});

test("projectPayload normalizes references and optional fields", () => {
  const form = blankProjectForm();
  Object.assign(form, { name: " demo ", repo_url: " https://example.test/repo ", healthcheck_path: "/health" });
  form.env_vars.push({
    name: " DATABASE_URL ", value_source: "secret_key_ref", source_name: " app-secret ", source_key: " url ", is_secret: false,
  });
  const payload = projectPayload(form);
  assert.equal(payload.name, "demo");
  assert.deepEqual(payload.env_vars[0], {
    name: "DATABASE_URL", value_source: "secret_key_ref", source_name: "app-secret", source_key: "url", is_secret: true,
  });
});

test("selecting a secret key reference marks the environment variable as secret", () => {
  const row = { value_source: "secret_key_ref", is_secret: false };
  syncEnvVarSecretFlag(row);
  assert.equal(row.is_secret, true);
});

test("projectPayload rejects incomplete environment variables", () => {
  const form = blankProjectForm();
  form.env_vars.push({ name: "TOKEN", value_source: "literal", value: "", is_secret: true });
  assert.throws(() => projectPayload(form), /needs a literal value/);
});
