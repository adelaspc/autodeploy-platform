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


test("exports persisted Helm evidence even when runtime logs are unavailable", () => {
  const diagnostics = {
    failure_stage: "helm", diagnostics_collection_status: "partial",
    diagnostics_collected_at: "2026-09-06T10:00:00Z",
    diagnostics_collection_errors: [{ operation: "service", reason: "access_denied" }],
    pod_previous_logs_summary: "Boot failed [REDACTED]",
  };
  const bundle = JSON.parse(diagnosticsBundleText({ diagnostics, runtimeLog: null }));
  assert.deepEqual(bundle.kubernetes_diagnostics, diagnostics);
  assert.equal(bundle.runtime_log, null);
});
