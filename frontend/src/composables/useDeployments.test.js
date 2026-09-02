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

test("active deployment polling refreshes events and stops at a terminal state", async () => {
  const summaries = [
    { deployment_id: 8, deployment_status: "pending", build_status: "pending", deploy_target: null },
    { deployment_id: 8, deployment_status: "building", build_status: "building", deploy_target: "local-docker" },
    { deployment_id: 8, deployment_status: "running", build_status: "succeeded", deploy_target: "local-docker" },
  ];
  let summaryIndex = 0;
  let eventRequestCount = 0;
  let scheduledCallback = null;
  const cancelled = [];
  const request = async (path) => {
    if (path.endsWith("/summary")) return summaries[Math.min(summaryIndex++, summaries.length - 1)];
    if (path.endsWith("/events")) {
      eventRequestCount += 1;
      return Array.from({ length: eventRequestCount }, (_, index) => ({ id: index + 1 }));
    }
    if (path.includes("build-log") || path.includes("runtime-log")) return null;
    throw new Error(`unexpected request: ${path}`);
  };
  const state = useDeployments(request, {
    schedule: (callback) => { scheduledCallback = callback; return 51; },
    cancel: (timer) => cancelled.push(timer),
  });

  await state.selectDeployment(3, { deployment_id: 8 });
  assert.equal(state.deploymentSummary.value.deployment_status, "pending");
  assert.equal(state.deploymentEvents.value.length, 1);
  assert.equal(typeof scheduledCallback, "function");

  await scheduledCallback();
  assert.equal(state.deploymentSummary.value.deployment_status, "building");
  assert.equal(state.deploymentEvents.value.length, 2);
  assert.deepEqual(cancelled, []);

  await scheduledCallback();
  assert.equal(state.deploymentSummary.value.deployment_status, "running");
  assert.equal(state.deploymentEvents.value.length, 3);
  assert.deepEqual(cancelled, [51]);
});
