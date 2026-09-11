import assert from "node:assert/strict";
import test from "node:test";

import { useLiveHealth } from "./useLiveHealth.js";

test("check records live health", async () => {
  const requests = [];
  const monitor = useLiveHealth(async (path) => {
    requests.push(path);
    return { status: "healthy", message: "ok" };
  });
  await monitor.check(3, { deployment_id: 7, deployment_status: "running" });
  assert.equal(monitor.serviceReachability.value.status, "healthy");
  assert.deepEqual(requests, ["/api/projects/3/deployments/7/live-health"]);
});

test("check converts request failures to unavailable", async () => {
  const monitor = useLiveHealth(async () => { throw new Error("offline"); });
  await monitor.check(3, { deployment_id: 7, deployment_status: "running" });
  assert.match(monitor.serviceReachability.value.message, /offline/);
});

test("start and stop own one scheduler", () => {
  const cancelled = [];
  const monitor = useLiveHealth(async () => ({}), { schedule: () => 42, cancel: (id) => cancelled.push(id) });
  monitor.start(3, () => ({ deployment_status: "running" }));
  monitor.stop();
  assert.deepEqual(cancelled, [42]);
});
