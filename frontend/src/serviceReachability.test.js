import assert from "node:assert/strict";
import test from "node:test";
import { canCleanupDeployment } from "./serviceReachability.js";

test("allows cleanup for Kubernetes deployment summaries", () => {
  assert.equal(
    canCleanupDeployment({ deploy_target: "kubernetes", deployment_status: "running" }),
    true,
  );
  assert.equal(canCleanupDeployment({ deploy_target: "local-docker", deployment_status: "running" }), false);
});
