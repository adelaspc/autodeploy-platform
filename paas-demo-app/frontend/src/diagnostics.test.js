import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { buildDiagnosticsView, diagnosticStageLabel } from "./diagnostics.js";

describe("diagnosticStageLabel", () => {
  it("formats known stages", () => {
    assert.equal(diagnosticStageLabel("helm"), "Helm");
    assert.equal(diagnosticStageLabel("manifest_apply"), "Manifest apply");
    assert.equal(diagnosticStageLabel(null), "No failure");
  });
});

describe("buildDiagnosticsView", () => {
  it("formats Helm deploy failure context", () => {
    const view = buildDiagnosticsView({
      failure_stage: "helm",
      failure_summary: "Error: rendered manifests contain a resource that already exists",
      failure_event_type: "kubernetes.helm_deploy_failed",
      failure_event_at: "2026-06-25T09:30:00Z",
      helm_release_name: "paas-demo-production-1",
      helm_namespace: "apps",
      helm_chart_path: "deploy/helm/generic-web-app",
      helm_returncode: 1,
      helm_stderr_summary: "Error: rendered manifests contain a resource that already exists",
      helm_log_path: "/tmp/helm-upgrade-install.log",
    });

    assert.equal(view.stageLabel, "Helm");
    assert.equal(view.summary, "Error: rendered manifests contain a resource that already exists");
    assert.equal(view.eventType, "kubernetes.helm_deploy_failed");
    assert.equal(view.hasHelm, true);
    assert.deepEqual(view.helmFields, [
      { label: "Release", value: "paas-demo-production-1" },
      { label: "Namespace", value: "apps" },
      { label: "Chart", value: "deploy/helm/generic-web-app" },
      { label: "Return code", value: "1" },
      { label: "Log path", value: "/tmp/helm-upgrade-install.log" },
    ]);
    assert.equal(view.helmStderrSummary, "Error: rendered manifests contain a resource that already exists");
  });

  it("formats Helm reconcile missing-release context", () => {
    const view = buildDiagnosticsView({
      failure_stage: "helm",
      failure_event_type: "reconcile.helm_release_missing",
      helm_release_name: "paas-demo-production-1",
      helm_namespace: "apps",
      diagnostics: {
        release_exists: false,
      },
    });

    assert.equal(view.hasHelm, true);
    assert.equal(view.summary, "No failure summary recorded.");
    assert.deepEqual(view.helmFields, [
      { label: "Release", value: "paas-demo-production-1" },
      { label: "Namespace", value: "apps" },
    ]);
  });

  it("formats preflight resource lists", () => {
    const view = buildDiagnosticsView({
      failure_stage: "preflight",
      failure_summary: "ConfigMap/app-config, Secret/app-secret",
      namespace: "apps",
      missing_resources: [
        { kind: "ConfigMap", name: "app-config" },
        { kind: "Secret", name: "app-secret" },
      ],
      checked_resources: [{ kind: "Secret", name: "registry-creds" }],
    });

    assert.equal(view.stageLabel, "Preflight");
    assert.equal(view.hasResourceContext, true);
    assert.deepEqual(view.resourceFields, [{ label: "Namespace", value: "apps" }]);
    assert.deepEqual(view.missingResources, ["ConfigMap/app-config", "Secret/app-secret"]);
    assert.deepEqual(view.checkedResources, ["Secret/registry-creds"]);
  });

  it("formats pod diagnostics for rollout and healthcheck failures", () => {
    const view = buildDiagnosticsView({
      failure_stage: "rollout",
      deployment_name: "paas-demo-1",
      service_name: "paas-demo-1-svc",
      pod_names: ["paas-demo-1-abc"],
      pod_describe_summary: "Warning FailedScheduling",
      pod_logs_summary: "ModuleNotFoundError",
    });

    assert.equal(view.stageLabel, "Rollout");
    assert.equal(view.hasResourceContext, true);
    assert.deepEqual(view.resourceFields, [
      { label: "Deployment", value: "paas-demo-1" },
      { label: "Service", value: "paas-demo-1-svc" },
    ]);
    assert.deepEqual(view.podNames, ["paas-demo-1-abc"]);
    assert.equal(view.podDescribeSummary, "Warning FailedScheduling");
    assert.equal(view.podLogsSummary, "ModuleNotFoundError");
  });

  it("uses safe fallback values for empty diagnostics", () => {
    const view = buildDiagnosticsView(null);

    assert.equal(view.stageLabel, "No failure");
    assert.equal(view.summary, "No failure summary recorded.");
    assert.equal(view.eventType, "not recorded");
    assert.equal(view.hasHelm, false);
    assert.equal(view.hasResourceContext, false);
    assert.equal(view.rawJson, "");
  });
});
