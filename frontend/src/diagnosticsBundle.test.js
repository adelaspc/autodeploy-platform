import assert from "node:assert/strict";
import test from "node:test";
import {
  DIAGNOSTICS_BUNDLE_NOTICE,
  buildDiagnosticsBundle,
  diagnosticsBundleFilename,
  diagnosticsBundleText,
} from "./diagnosticsBundle.js";

test("builds a deployment diagnostics bundle", () => {
  const parts = {
    summary: { deployment_id: 42, deployment_status: "failed" },
    diagnostics: { pod_phase: "Running", restart_count: 2 },
    events: [{ event_type: "deployment.failed" }],
    buildLog: { content: "build output" },
    runtimeLog: { content: "runtime output" },
  };
  const bundle = buildDiagnosticsBundle(parts);
  assert.equal(bundle.deployment.deployment_id, 42);
  assert.equal(bundle.handling_notice, DIAGNOSTICS_BUNDLE_NOTICE);
  assert.equal(bundle.kubernetes_diagnostics.pod_phase, "Running");
  assert.match(diagnosticsBundleText(parts), /deployment.failed/);
  assert.equal(diagnosticsBundleFilename(parts.summary), "paas-deployment-42-diagnostics.json");
});
