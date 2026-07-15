import assert from "node:assert/strict";
import test from "node:test";

import { useDeployments } from "./useDeployments.js";

test("selectDeployment loads summary, events, and optional artifacts", async () => {
  const request = async (path) => {
    if (path.endsWith("/summary")) return { deployment_id: 8, deployment_status: "running", build_status: "succeeded", deploy_target: "local-docker" };
    if (path.endsWith("/events")) return [{ id: 1 }];
    if (path.includes("build-log")) return { content: "build" };
    if (path.includes("runtime-log")) return { content: "runtime" };
    throw new Error(`unexpected request: ${path}`);
  };
  const state = useDeployments(request);
  const summary = await state.selectDeployment(3, { deployment_id: 8 });
  assert.equal(summary.deployment_status, "running");
  assert.equal(state.deploymentEvents.value.length, 1);
  assert.equal(state.buildLog.value.content, "build");
  assert.equal(state.diagnostics.value, null);
});

test("optional log 404 is represented as unavailable", async () => {
  const request = async (path) => {
    if (path.endsWith("/summary")) return { deployment_id: 8, deployment_status: "failed", build_status: "failed", deploy_target: "local-docker" };
    if (path.endsWith("/events")) return [];
    const error = new Error("missing"); error.status = 404; throw error;
  };
  const state = useDeployments(request);
  await state.selectDeployment(3, { deployment_id: 8 });
  assert.equal(state.buildLog.value, null);
  assert.equal(state.runtimeLog.value, null);
});
