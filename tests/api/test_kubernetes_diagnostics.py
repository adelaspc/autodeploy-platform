import subprocess

from control_plane.extensions import db
from control_plane.models import DeploymentEvent, PlatformDeployment
from tests.api.project_test_helpers import create_project


def test_get_kubernetes_diagnostics_returns_structured_failure_view(client, app, monkeypatch):
    project_response = create_project(client, name="k8s-diagnostics-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "failed", "build_status": "failed"},
    )
    deployment_id = deployment_response.get_json()["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        deployment.last_error = "Healthcheck did not succeed within 30 seconds"
        deployment.build.last_error = deployment.last_error
        deployment.build.registry_push_status = "succeeded"
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="kubernetes.healthcheck_failed",
                step="deploy.kubernetes.healthcheck",
                level="error",
                status="failed",
                message=deployment.last_error,
                metadata_json={
                    "namespace": "default",
                    "deployment_name": "paas-k8s-diagnostics-app-1",
                    "service_name": "paas-k8s-diagnostics-app-1-svc",
                    "healthcheck_service_summary": "Endpoints: <none> | Session Affinity: None",
                    "healthcheck_pod_names": ["app-123"],
                    "healthcheck_pod_describe_summary": "app-123: Pod Conditions: | Ready  True",
                    "healthcheck_pod_logs_summary": "app-123: waiting for upstream dependency",
                    "healthcheck_pod_previous_logs_summary": "app-123: previous boot failed before binding port",
                    "healthcheck_pod_phase": "Running",
                    "healthcheck_container_reason": "CrashLoopBackOff",
                    "healthcheck_restart_count": 4,
                    "healthcheck_images": ["docker.io/example/app:v1"],
                    "healthcheck_image_pull_secrets": ["dockerhub-pull"],
                },
            )
        )
        db.session.commit()

    def unexpected_cluster_command(*_args, **_kwargs):
        raise AssertionError("Diagnostics reads must not invoke a cluster command")

    monkeypatch.setattr(subprocess, "run", unexpected_cluster_command)
    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/kubernetes-diagnostics")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["deployment_id"] == deployment_id
    assert payload["deploy_target"] == "kubernetes"
    assert payload["namespace"] == "default"
    assert payload["deployment_name"] == "paas-k8s-diagnostics-app-1"
    assert payload["service_name"] == "paas-k8s-diagnostics-app-1-svc"
    assert payload["failure_stage"] == "healthcheck"
    assert payload["failure_summary"] == "app-123: waiting for upstream dependency"
    assert payload["pod_names"] == ["app-123"]
    assert payload["pod_describe_summary"] == "app-123: Pod Conditions: | Ready  True"
    assert payload["pod_logs_summary"] == "app-123: waiting for upstream dependency"
    assert payload["pod_previous_logs_summary"] == "app-123: previous boot failed before binding port"
    assert payload["pod_phase"] == "Running"
    assert payload["container_reason"] == "CrashLoopBackOff"
    assert payload["restart_count"] == 4
    assert payload["images"] == ["docker.io/example/app:v1"]
    assert payload["image_pull_secrets"] == ["dockerhub-pull"]
    assert payload["diagnostics_snapshot_at"] == payload["failure_event_at"]
    assert payload["diagnostics"]["healthcheck_service_summary"] == "Endpoints: <none> | Session Affinity: None"

    repeated_payload = client.get(
        f"/api/projects/{project_id}/deployments/{deployment_id}/kubernetes-diagnostics"
    ).get_json()
    assert repeated_payload == payload


def test_get_kubernetes_diagnostics_includes_helm_deploy_failure_context(client, app):
    project_response = create_project(client, name="helm-diagnostics-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "failed", "build_status": "failed"},
    )
    deployment_id = deployment_response.get_json()["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        deployment.helm_release_name = "paas-helm-diagnostics-app-production-1"
        deployment.helm_namespace = "apps"
        deployment.helm_chart_path = "deploy/helm/generic-web-app"
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="kubernetes.helm_deploy_failed",
                step="deploy.kubernetes.helm",
                level="error",
                status="failed",
                message="Helm release failed to deploy",
                metadata_json={
                    "deployment_mode": "helm",
                    "helm_release_name": deployment.helm_release_name,
                    "namespace": deployment.helm_namespace,
                    "chart_path": deployment.helm_chart_path,
                    "helm_returncode": 1,
                    "helm_stdout_summary": "",
                    "helm_stderr_summary": "Error: rendered manifests contain a resource that already exists",
                    "helm_log_path": "/tmp/helm-upgrade-install.log",
                },
            )
        )
        db.session.commit()

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/kubernetes-diagnostics")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["failure_stage"] == "helm"
    assert payload["failure_event_type"] == "kubernetes.helm_deploy_failed"
    assert payload["failure_summary"] == "Error: rendered manifests contain a resource that already exists"
    assert payload["helm_release_name"] == "paas-helm-diagnostics-app-production-1"
    assert payload["helm_namespace"] == "apps"
    assert payload["helm_chart_path"] == "deploy/helm/generic-web-app"
    assert payload["helm_returncode"] == 1
    assert payload["helm_stderr_summary"] == "Error: rendered manifests contain a resource that already exists"
    assert payload["helm_log_path"] == "/tmp/helm-upgrade-install.log"
    assert payload["diagnostics"]["deployment_mode"] == "helm"
    assert payload["diagnostics_collection_status"] is None
    assert payload["diagnostics_collected_at"] is None
    assert payload["pod_previous_logs_summary"] is None


def test_get_kubernetes_diagnostics_includes_helm_reconcile_context(client, app):
    project_response = create_project(client, name="helm-reconcile-diagnostics-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "failed", "build_status": "failed"},
    )
    deployment_id = deployment_response.get_json()["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        deployment.helm_release_name = "paas-helm-reconcile-diagnostics-app-production-1"
        deployment.helm_namespace = "apps"
        deployment.helm_chart_path = "deploy/helm/generic-web-app"
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="reconcile.helm_release_missing",
                step="reconcile.helm_release_missing",
                level="error",
                status="failed",
                message="Marked running deployment failed because its Helm release is missing",
                metadata_json={
                    "release_exists": False,
                    "helm_release_name": deployment.helm_release_name,
                    "namespace": deployment.helm_namespace,
                    "chart_path": deployment.helm_chart_path,
                    "helm_stderr_summary": "Error: release: not found",
                },
            )
        )
        db.session.commit()

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/kubernetes-diagnostics")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["failure_stage"] == "helm"
    assert payload["failure_event_type"] == "reconcile.helm_release_missing"
    assert payload["failure_summary"] == "Error: release: not found"
    assert payload["helm_release_name"] == "paas-helm-reconcile-diagnostics-app-production-1"
    assert payload["helm_namespace"] == "apps"
    assert payload["helm_release_status"] is None
    assert payload["diagnostics"]["release_exists"] is False


def test_secret_values_are_redacted_from_diagnostics_summary_and_logs(client, app, tmp_path):
    project_response = create_project(
        client,
        name="secret-redaction-diagnostics-app",
        env_vars=[
            {"name": "DATABASE_URL", "value": "postgres://user:super-secret@db/app", "is_secret": True},
        ],
    )
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "failed", "build_status": "failed"},
    )
    deployment_id = deployment_response.get_json()["id"]

    build_log_path = tmp_path / "build-secret.log"
    runtime_log_path = tmp_path / "runtime-secret.log"
    build_log_path.write_text("DATABASE_URL=postgres://user:super-secret@db/app\n", encoding="utf-8")
    runtime_log_path.write_text("booting with postgres://user:super-secret@db/app\n", encoding="utf-8")

    with app.app_context():
        app.config["CONTROL_PLANE_WORKSPACE_ROOT"] = str(tmp_path)
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        deployment.last_error = "failed to connect to postgres://user:super-secret@db/app"
        deployment.build.last_error = deployment.last_error
        deployment.build.log_path = str(build_log_path)
        deployment.build.registry_push_status = "succeeded"
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="kubernetes.healthcheck_failed",
                step="deploy.kubernetes.healthcheck",
                level="error",
                status="failed",
                message=deployment.last_error,
                metadata_json={
                    "namespace": "default",
                    "deployment_name": "paas-secret-redaction-diagnostics-app-1",
                    "service_name": "paas-secret-redaction-diagnostics-app-1-svc",
                    "healthcheck_pod_logs_summary": "app-123: postgres://user:super-secret@db/app",
                    "runtime_log_path": str(runtime_log_path),
                    "env_vars": deployment.project.env_vars,
                },
            )
        )
        db.session.commit()

    deployment_payload = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}").get_json()
    assert "super-secret" not in str(deployment_payload)
    assert "[REDACTED]" in str(deployment_payload)

    summary_payload = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/summary").get_json()
    assert "super-secret" not in str(summary_payload)
    assert "[REDACTED]" in str(summary_payload)

    diagnostics_payload = client.get(
        f"/api/projects/{project_id}/deployments/{deployment_id}/kubernetes-diagnostics"
    ).get_json()
    assert "super-secret" not in str(diagnostics_payload)
    assert "[REDACTED]" in str(diagnostics_payload)

    build_log_payload = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/build-log").get_json()
    assert "super-secret" not in build_log_payload["content"]
    assert "[REDACTED]" in build_log_payload["content"]

    runtime_log_payload = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/runtime-log").get_json()
    assert "super-secret" not in runtime_log_payload["content"]
    assert "[REDACTED]" in runtime_log_payload["content"]


def test_get_kubernetes_diagnostics_returns_preflight_missing_resources(client, app):
    project_response = create_project(client, name="k8s-diagnostics-preflight-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "failed", "build_status": "failed"},
    )
    deployment_id = deployment_response.get_json()["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        deployment.last_error = "Missing Kubernetes referenced resources"
        deployment.build.last_error = deployment.last_error
        deployment.build.registry_push_status = "succeeded"
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="kubernetes.preflight_failed",
                step="deploy.kubernetes.preflight",
                level="error",
                status="failed",
                message="Missing Kubernetes referenced resources: ConfigMap/my-app-config",
                metadata_json={
                    "namespace": "default",
                    "checked_resources": ["configmap/my-app-config"],
                    "missing_resources": [{"kind": "ConfigMap", "name": "my-app-config"}],
                    "configmap_refs_used": ["my-app-config"],
                    "secret_refs_used": [],
                },
            )
        )
        db.session.commit()

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/kubernetes-diagnostics")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["failure_stage"] == "preflight"
    assert payload["failure_summary"] == "ConfigMap/my-app-config"
    assert payload["missing_resources"] == [{"kind": "ConfigMap", "name": "my-app-config"}]
    assert payload["checked_resources"] == ["configmap/my-app-config"]
    assert payload["configmap_refs_used"] == ["my-app-config"]
    assert payload["pod_names"] is None


def test_get_deployment_summary_prefers_persisted_preflight_failure_context(client, app):
    project_response = create_project(client, name="k8s-summary-persisted-preflight-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "failed", "build_status": "failed"},
    )
    deployment_id = deployment_response.get_json()["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        deployment.preflight_status = "failed"
        deployment.preflight_summary = "Missing Kubernetes referenced resources: ConfigMap/my-app-config"
        deployment.preflight_metadata_json = {
            "namespace": "default",
            "missing_resources": [{"kind": "ConfigMap", "name": "my-app-config"}],
        }
        db.session.commit()

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/summary")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["last_kubernetes_failure_stage"] == "preflight"
    assert payload["last_kubernetes_failure_summary"] == "ConfigMap/my-app-config"
    assert payload["last_kubernetes_failure_missing_resources"] == [
        {"kind": "ConfigMap", "name": "my-app-config"}
    ]


def test_get_kubernetes_diagnostics_prefers_persisted_preflight_failure_context(client, app):
    project_response = create_project(client, name="k8s-diagnostics-persisted-preflight-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "failed", "build_status": "failed"},
    )
    deployment_id = deployment_response.get_json()["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        deployment.preflight_status = "failed"
        deployment.preflight_summary = "Missing Kubernetes referenced resources: ConfigMap/my-app-config"
        deployment.preflight_metadata_json = {
            "namespace": "default",
            "checked_resources": ["configmap/my-app-config"],
            "missing_resources": [{"kind": "ConfigMap", "name": "my-app-config"}],
            "configmap_refs_used": ["my-app-config"],
            "secret_refs_used": [],
        }
        db.session.commit()

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/kubernetes-diagnostics")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["failure_stage"] == "preflight"
    assert payload["failure_event_type"] == "deployment.preflight_failed"
    assert payload["failure_summary"] == "ConfigMap/my-app-config"
    assert payload["missing_resources"] == [{"kind": "ConfigMap", "name": "my-app-config"}]
    assert payload["checked_resources"] == ["configmap/my-app-config"]
    assert payload["configmap_refs_used"] == ["my-app-config"]


def test_get_kubernetes_diagnostics_marks_legacy_records_without_a_capture_time(client, app):
    project_response = create_project(client, name="legacy-k8s-diagnostics-app")
    project_id = project_response.get_json()["id"]
    deployment_id = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "running", "build_status": "succeeded"},
    ).get_json()["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        db.session.commit()

    payload = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/kubernetes-diagnostics").get_json()

    assert payload["diagnostics_snapshot_at"] is None
    assert payload["failure_event_at"] is None


def test_get_kubernetes_diagnostics_returns_404_for_non_kubernetes_deployment(client):
    project_response = create_project(client, name="non-k8s-diagnostics-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "running", "build_status": "succeeded"},
    )
    deployment_id = deployment_response.get_json()["id"]

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/kubernetes-diagnostics")

    assert response.status_code == 404
    assert response.get_json() == {"error": "Deployment does not use the Kubernetes target"}


def test_helm_snapshot_survives_workspace_removal_and_preserves_partial_evidence(client, app, tmp_path):
    from worker.processing.events import record_auxiliary_events

    project_id = create_project(client, name="helm-snapshot").get_json()["id"]
    deployment_id = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc1234", "status": "failed", "build_status": "failed"},
    ).get_json()["id"]
    log_path = tmp_path / "previous.log"
    log_path.write_text("Application boot failed")
    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        record_auxiliary_events(deployment, [{
            "event_type": "kubernetes.helm_deploy_failed", "status": "failed",
            "step": "deploy.kubernetes.helm", "level": "error", "message": "Helm release failed to deploy",
            "metadata_json": {
                "namespace": "apps", "deployment_name": "demo-generic-web-app", "service_name": "demo-generic-web-app",
                "helm_release_name": "demo", "helm_returncode": 1, "helm_stderr_summary": "context deadline exceeded",
                "diagnostics_collected_at": "2026-09-06T10:00:00+00:00",
                "diagnostics_collection_status": "partial",
                "diagnostics_collection_errors": [{"operation": "helm-service", "reason": "access_denied"}],
                "helm_pod_names": ["demo-pod"], "helm_container_reason": "CrashLoopBackOff",
                "helm_restart_count": 3, "helm_pod_logs_summary": "Current boot attempt",
                "helm_pod_previous_logs_summary": log_path.read_text(),
                "helm_pod_describe_summary": "Back-off restarting failed container",
                "helm_deployment_summary": "Available: 0/1",
                "helm_pod_runtime": [{"name": "demo-pod", "containers": [{"reason": "CrashLoopBackOff"}]}],
            },
        }])
        db.session.commit()
    log_path.unlink()
    payload = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/kubernetes-diagnostics").get_json()
    assert payload["failure_stage"] == "helm"
    assert payload["failure_summary"] == "context deadline exceeded"
    assert payload["container_reason"] == "CrashLoopBackOff"
    assert payload["pod_names"] == ["demo-pod"]
    assert payload["restart_count"] == 3
    assert payload["pod_logs_summary"] == "Current boot attempt"
    assert payload["pod_previous_logs_summary"] == "Application boot failed"
    assert payload["pod_describe_summary"] == "Back-off restarting failed container"
    assert payload["deployment_describe_summary"] == "Available: 0/1"
    assert payload["diagnostics_collection_status"] == "partial"
    assert payload["diagnostics_collected_at"] == "2026-09-06T10:00:00+00:00"
    assert payload["diagnostics_snapshot_at"] == "2026-09-06T10:00:00+00:00"
    assert payload["diagnostics_collection_errors"][0]["reason"] == "access_denied"
