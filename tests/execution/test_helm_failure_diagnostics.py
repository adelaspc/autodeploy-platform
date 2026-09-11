"""Exercise Helm failures with chart-compatible Pod labels and partial evidence."""

import json
import subprocess

import pytest

from worker.execution.contracts import WorkerExecutionError
from worker.executors.kubernetes.executor import KubernetesExecutor
from worker.executors.kubernetes.helm_diagnostics import collect_helm_failure_diagnostics
from worker.processing.claims import ClaimLostError, DeploymentCancellationRequested
from test_kubernetes_executor import FailingHelmRunner, make_kubernetes_deployment_stub


RELEASE = "paas-helm-app-production-3"
RESOURCE = RELEASE + "-generic-web-app"
SELECTOR = "app.kubernetes.io/instance=" + RELEASE


def pod(name="demo-pod", release=RELEASE, namespace="apps"):
    return {
        "metadata": {"name": name, "namespace": namespace, "labels": {"app.kubernetes.io/instance": release}},
        "spec": {"containers": [{"name": "app", "image": "example/app:revision"}]},
        "status": {"phase": "Running", "containerStatuses": [{
            "name": "app", "restartCount": 3, "ready": False,
            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
        }]},
    }


def executor(tmp_path, runner):
    return KubernetesExecutor(
        workspace_root=tmp_path, command_timeout=600, runner=runner,
        namespace="apps", deployment_mode="helm", helm_runner_factory=FailingHelmRunner,
    )


def collect(instance, tmp_path):
    return collect_helm_failure_diagnostics(
        instance, make_kubernetes_deployment_stub(), deployment_name=RESOURCE,
        service_name=RESOURCE, pod_selector=SELECTOR, logs_dir=tmp_path,
    )


def test_helm_failure_captures_scoped_pods_logs_and_event_metadata(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("CONTROL_PLANE_GIT_TOKEN_GITHUB", "known-git-secret")

    def runner(args, **kwargs):
        calls.append(args)
        assert 0 < kwargs["timeout"] <= 5
        if "json" in args:
            assert args[args.index("-l") + 1] == SELECTOR
            output = json.dumps({"items": [pod(), pod("unrelated", "another-release"), pod("foreign", namespace="other")]})
        elif "--previous" in args:
            output = "Boot failed: missing required configuration known-git-secret"
        else:
            output = "CrashLoopBackOff\npassword=credential\nknown-git-secret"
        return subprocess.CompletedProcess(args, 0, output, "")

    instance = executor(tmp_path, runner)
    with pytest.raises(WorkerExecutionError) as raised:
        instance.deploy(make_kubernetes_deployment_stub())
    error = raised.value
    assert error.step == "deploy.kubernetes.helm"
    assert error.metadata["helm_stderr_summary"] == "helm failed"
    assert error.metadata["diagnostics_collection_status"] == "complete"
    assert error.metadata["deployment_name"] == RESOURCE
    assert error.metadata["helm_pod_names"] == ["demo-pod"]
    assert error.metadata["helm_container_reason"] == "CrashLoopBackOff"
    assert error.metadata["helm_restart_count"] == 3
    assert "Boot failed" in error.metadata["helm_pod_previous_logs_summary"]
    assert error.metadata["diagnostics_collected_at"]
    event = next(item for item in error.events if item["event_type"] == "kubernetes.helm_deploy_failed")
    assert event["metadata_json"]["helm_pod_previous_logs_summary"] == error.metadata["helm_pod_previous_logs_summary"]
    assert all("unrelated" not in " ".join(args) and "foreign" not in " ".join(args) for args in calls)
    assert all(args[:3] == ["kubectl", "--namespace", "apps"] for args in calls)
    assert all(args[-2:] == ["--tail", "50"] for args in calls if "logs" in args)
    persisted = json.dumps(error.metadata) + "".join(p.read_text() for p in tmp_path.rglob("*.log"))
    assert "known-git-secret" not in persisted
    assert "credential" not in persisted


@pytest.mark.parametrize("failure,reason", [
    ("Error from server (Forbidden): access denied", "access_denied"),
    ("Error from server (NotFound): deployment not found", "resource_not_found"),
])
def test_unavailable_diagnostics_do_not_hide_helm_error(tmp_path, failure, reason):
    def runner(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", failure)

    with pytest.raises(WorkerExecutionError) as raised:
        executor(tmp_path, runner).deploy(make_kubernetes_deployment_stub())
    assert raised.value.metadata["diagnostics_collection_status"] == "unavailable"
    assert raised.value.metadata["diagnostics_collection_errors"][0]["reason"] == reason
    assert raised.value.metadata["helm_stderr_summary"] == "helm failed"
    assert raised.value.metadata.get("helm_pod_names") is None


def test_no_pods_are_reported_as_missing_evidence(tmp_path):
    def runner(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, '{"items": []}' if "json" in args else "Deployment not ready", "")

    metadata = collect(executor(tmp_path, runner), tmp_path)
    assert metadata["diagnostics_collection_status"] == "partial"
    assert metadata["diagnostics_collection_errors"] == [{"operation": "helm-pods", "reason": "no_matching_pods"}]
    assert metadata["helm_pod_logs_summary"] is None


def test_missing_previous_logs_keep_current_evidence(tmp_path):
    def runner(args, **kwargs):
        if "--previous" in args:
            return subprocess.CompletedProcess(args, 1, "", "previous terminated container not found")
        return subprocess.CompletedProcess(args, 0, json.dumps({"items": [pod()]}) if "json" in args else "current evidence", "")

    metadata = collect(executor(tmp_path, runner), tmp_path)
    assert metadata["diagnostics_collection_status"] == "partial"
    assert metadata["helm_pod_previous_logs_summary"] is None
    assert "current evidence" in metadata["helm_pod_logs_summary"]
    assert metadata["diagnostics_collection_errors"][0]["reason"] == "previous_logs_unavailable"


def test_collection_budget_retains_partial_results(tmp_path, monkeypatch):
    now = [0]
    monkeypatch.setattr("worker.executors.kubernetes.helm_diagnostics.time.monotonic", lambda: now[0])
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        if "json" in args:
            return subprocess.CompletedProcess(args, 0, json.dumps({"items": [pod(f"pod-{i}") for i in range(4)]}), "")
        now[0] += kwargs["timeout"]
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    metadata = collect(executor(tmp_path, runner), tmp_path)
    assert now[0] == 30
    assert len(calls) == 7
    assert metadata["diagnostics_collection_status"] == "partial"
    assert len(metadata["helm_pod_names"]) == 3
    reasons = {item["reason"] for item in metadata["diagnostics_collection_errors"]}
    assert {"pod_limit_reached", "command_timed_out", "collection_budget_exhausted"} <= reasons


@pytest.mark.parametrize("error", [
    ClaimLostError(7, "worker", "other", None, "deploying"),
    DeploymentCancellationRequested(7, 1, "stop"),
])
def test_collection_propagates_ownership_and_cancellation(tmp_path, error):
    instance = executor(tmp_path, lambda *args, **kwargs: pytest.fail("No command should run"))

    def heartbeat():
        raise error

    instance.heartbeat = heartbeat
    with pytest.raises(type(error)):
        instance.deploy(make_kubernetes_deployment_stub())


def test_log_write_failure_and_malformed_json_preserve_helm_failure(tmp_path, monkeypatch):
    instance = executor(tmp_path, lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "not JSON", ""))
    monkeypatch.setattr(instance, "_write_log", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read-only")))
    with pytest.raises(WorkerExecutionError) as raised:
        instance.deploy(make_kubernetes_deployment_stub())
    assert raised.value.metadata["helm_log_error"] == "log_write_failed"
    assert raised.value.metadata["helm_stderr_summary"] == "helm failed"
    assert "invalid_resource_response" in {e["reason"] for e in raised.value.metadata["diagnostics_collection_errors"]}


@pytest.mark.parametrize("method", ["_collect_pod_runtime_metadata", "_collect_healthcheck_diagnostics"])
def test_shared_collectors_accept_helm_selector(tmp_path, method):
    selectors = []

    def runner(args, **kwargs):
        if "-l" in args:
            selectors.append(args[args.index("-l") + 1])
        output = json.dumps({"items": [pod()]}) if "json" in args else "pod/demo-pod" if "name" in args else "evidence"
        return subprocess.CompletedProcess(args, 0, output, "")

    instance = executor(tmp_path, runner)
    kwargs = {"logs_dir": tmp_path, "pod_selector": SELECTOR}
    kwargs.update({"prefix": "healthcheck"} if method == "_collect_pod_runtime_metadata" else {"service_name": RESOURCE})
    metadata = getattr(instance, method)(RESOURCE, **kwargs)
    assert metadata["healthcheck_container_reason"] == "CrashLoopBackOff"
    assert selectors and set(selectors) == {SELECTOR}
