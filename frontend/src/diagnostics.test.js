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
      pod_describe_summary: "Warning FailedScheduling ImagePullBackOff",
      pod_logs_summary: "ModuleNotFoundError",
      pod_previous_logs_summary: "Previous ModuleNotFoundError",
    });

    assert.equal(view.stageLabel, "Rollout");
    assert.equal(view.hasResourceContext, true);
    assert.deepEqual(view.resourceFields, [
      { label: "Deployment", value: "paas-demo-1" },
      { label: "Service", value: "paas-demo-1-svc" },
    ]);
    assert.deepEqual(view.podNames, ["paas-demo-1-abc"]);
    assert.equal(view.podDescribeSummary, "Warning FailedScheduling ImagePullBackOff");
    assert.equal(view.podLogsSummary, "ModuleNotFoundError");
    assert.equal(view.podPreviousLogsSummary, "Previous ModuleNotFoundError");
    assert.deepEqual(view.insights, [
      {
        title: "Image pull is failing",
        detail:
          "Kubernetes created the pod, but kubelet cannot pull the image. Verify the image reference, Docker Hub repository, namespace, and imagePullSecret.",
        action:
          "Set CONTROL_PLANE_K8S_IMAGE_PULL_SECRET to an existing dockerconfigjson secret, recreate the runtime, then start a new deployment.",
      },
    ]);
  });

  it("detects CrashLoopBackOff diagnostics", () => {
    const view = buildDiagnosticsView({
      failure_stage: "healthcheck",
      pod_describe_summary: "Warning BackOff Back-off restarting failed container app",
      pod_previous_logs_summary: "RuntimeError: DEPLOYMENT_NOTES_DATABASE_URL must be set",
    });

    assert.equal(view.insights.length, 1);
    assert.equal(view.insights[0].title, "Container is restarting");
  });

  it("formats first-class pod runtime fields", () => {
    const view = buildDiagnosticsView({
      diagnostics_snapshot_at: "2026-09-08T12:00:00Z",
      pod_phase: "Running",
      container_reason: "CrashLoopBackOff",
      restart_count: 3,
      images: ["docker.io/example/app:v1"],
      image_pull_secrets: ["dockerhub-pull"],
    });

    assert.deepEqual(view.podFields, [
      { label: "Pod phase", value: "Running" },
      { label: "Container reason", value: "CrashLoopBackOff" },
      { label: "Restart count", value: "3" },
      { label: "Images", value: "docker.io/example/app:v1" },
      { label: "Image pull secrets", value: "dockerhub-pull" },
    ]);
    assert.equal(view.snapshotAt, "2026-09-08T12:00:00Z");
  });

  it("uses safe fallback values for empty diagnostics", () => {
    const view = buildDiagnosticsView(null);

    assert.equal(view.stageLabel, "No failure");
    assert.equal(view.summary, "No failure summary recorded.");
    assert.equal(view.eventType, "not recorded");
    assert.equal(view.snapshotAt, null);
    assert.equal(view.hasHelm, false);
    assert.equal(view.hasResourceContext, false);
    assert.deepEqual(view.insights, []);
    assert.equal(view.rawJson, "");
  });
});


describe("Helm failure snapshots", () => {
  it("uses structured container reasons and exposes partial collection", () => {
    const view = buildDiagnosticsView({
      failure_stage: "helm", failure_summary: "context deadline exceeded",
      pod_runtime: [{ name: "demo-pod", containers: [{ reason: "CrashLoopBackOff" }] }],
      diagnostics_collection_status: "partial", diagnostics_collected_at: "2026-09-06T10:00:00Z",
      diagnostics_collection_errors: [{ operation: "previous-logs", reason: "previous_logs_unavailable" }],
      pod_logs_summary: "Starting application", deployment_describe_summary: "Available: 0/1",
    });
    assert.equal(view.stage, "helm");
    assert.equal(view.summary, "context deadline exceeded");
    assert.equal(view.insights[0].title, "Container is restarting");
    assert.equal(view.collectionStatus, "partial");
    assert.equal(view.collectedAt, "2026-09-06T10:00:00Z");
    assert.equal(view.collectionErrors[0].reason, "previous_logs_unavailable");
    assert.equal(view.deploymentDescribeSummary, "Available: 0/1");
  });

  it("does not infer CrashLoopBackOff from an old Helm timeout alone", () => {
    const view = buildDiagnosticsView({ failure_stage: "helm", failure_summary: "context deadline exceeded" });
    assert.deepEqual(view.insights, []);
    assert.equal(view.collectionStatus, null);
    assert.equal(view.collectedAt, null);
  });
});
