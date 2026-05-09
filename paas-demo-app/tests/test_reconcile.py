from datetime import timedelta

from backend.extensions import db
from backend.models import PlatformDeployment
from worker.executor import ExecutionResult
from worker.reconcile import reconcile_deployments
from worker.executor import WorkerExecutionError
from worker.service import now_utc

from tests.test_projects import create_project


def create_deployment(client, *, name, status="pending", build_status="pending"):
    project_response = create_project(client, name=name)
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": "abc123def456",
            "image_name": name,
            "image_tag": "abc123def456",
            "status": status,
            "build_status": build_status,
        },
    )
    return deployment_response.get_json()


def test_reconciler_clears_stale_pending_claim(client, app):
    deployment_payload = create_deployment(client, name="stale-pending")
    deployment_id = deployment_payload["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.claimed_by = "dead-worker"
        deployment.claimed_at = now_utc() - timedelta(seconds=600)
        db.session.commit()

        changes = reconcile_deployments()

        assert changes == 1
        updated = db.session.get(PlatformDeployment, deployment_id)
        assert updated.status == "pending"
        assert updated.claimed_by is None
        assert updated.claimed_at is None
        assert any(event.event_type == "reconcile.claim_cleared" for event in updated.events)


def test_reconciler_marks_running_deployment_failed_when_container_missing(client, app, monkeypatch):
    deployment_payload = create_deployment(client, name="missing-container", status="running", build_status="succeeded")
    deployment_id = deployment_payload["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "local-docker"
        deployment.container_name = "missing-container"
        deployment.service_url = "http://127.0.0.1:18080"
        db.session.commit()

    class MissingContainerExecutor:
        def container_exists(self, deployment):
            return False

        def cleanup_workspace(self, deployment):
            return {"workspace_removed": False, "log_removed": False}

    import worker.reconcile as reconcile_module

    monkeypatch.setattr(reconcile_module, "create_executor_for_deployment", lambda deployment: MissingContainerExecutor())

    with app.app_context():
        changes = reconcile_deployments()
        assert changes == 1
        updated = db.session.get(PlatformDeployment, deployment_id)
        assert updated.status == "failed"
        assert updated.service_url is None
        assert any(event.event_type == "reconcile.container_missing" for event in updated.events)


def test_reconciler_cleans_orphan_container_and_workspace(client, app, monkeypatch, tmp_path):
    deployment_payload = create_deployment(client, name="failed-cleanup", status="failed", build_status="failed")
    deployment_id = deployment_payload["id"]
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    log_file = tmp_path / "build.log"
    log_file.write_text("build failed\n", encoding="utf-8")

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "local-docker"
        deployment.container_name = "orphan-container"
        deployment.build.workspace_path = str(workspace_dir)
        deployment.build.log_path = str(log_file)
        db.session.commit()

    class CleanupExecutor:
        def container_exists(self, deployment):
            return True

        def stop(self, deployment):
            return ExecutionResult(
                "Container removed successfully.",
                metadata={"executor": "local-docker", "stopped": True, "container_name": deployment.container_name},
                log_path="/tmp/reconcile-stop.log",
            )

        def cleanup_workspace(self, deployment):
            removed_paths = []
            if workspace_dir.exists():
                workspace_dir.rmdir()
                removed_paths.append(str(workspace_dir))
            if log_file.exists():
                log_file.unlink()
                removed_paths.append(str(log_file))
            return {"workspace_removed": True, "log_removed": True, "removed_paths": removed_paths}

    import worker.reconcile as reconcile_module

    monkeypatch.setattr(reconcile_module, "create_executor_for_deployment", lambda deployment: CleanupExecutor())

    with app.app_context():
        changes = reconcile_deployments()
        assert changes == 1
        updated = db.session.get(PlatformDeployment, deployment_id)
        event_types = [event.event_type for event in updated.events]
        assert "reconcile.container_removed" in event_types
        assert "reconcile.workspace_removed" in event_types
        assert not workspace_dir.exists()
        assert not log_file.exists()


def test_reconciler_keeps_running_kubernetes_deployment_when_resources_exist(client, app, monkeypatch):
    deployment_payload = create_deployment(client, name="k8s-present", status="running", build_status="succeeded")
    deployment_id = deployment_payload["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        deployment.service_url = "http://paas-k8s-present-1-svc.default.svc.cluster.local:5000"
        db.session.commit()

    class HealthyKubernetesExecutor:
        def runtime_resource_status(self, deployment):
            return {
                "namespace": "default",
                "deployment_name": f"paas-{deployment.project.name}-{deployment.id}",
                "service_name": f"paas-{deployment.project.name}-{deployment.id}-svc",
                "deployment_exists": True,
                "service_exists": True,
            }

        def cleanup_workspace(self, deployment):
            return {"workspace_removed": False, "log_removed": False}

    import worker.reconcile as reconcile_module

    monkeypatch.setattr(reconcile_module, "create_executor_for_deployment", lambda deployment: HealthyKubernetesExecutor())

    with app.app_context():
        changes = reconcile_deployments()
        assert changes == 0
        updated = db.session.get(PlatformDeployment, deployment_id)
        assert updated.status == "running"
        assert not any(event.event_type == "reconcile.kubernetes_missing_resource" for event in updated.events)


def test_reconciler_marks_running_kubernetes_deployment_failed_when_deployment_missing(client, app, monkeypatch):
    deployment_payload = create_deployment(client, name="k8s-missing-deploy", status="running", build_status="succeeded")
    deployment_id = deployment_payload["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        deployment.service_url = "http://paas-k8s-missing-deploy-1-svc.default.svc.cluster.local:5000"
        db.session.commit()

    class MissingDeploymentExecutor:
        def runtime_resource_status(self, deployment):
            return {
                "namespace": "default",
                "deployment_name": f"paas-{deployment.project.name}-{deployment.id}",
                "service_name": f"paas-{deployment.project.name}-{deployment.id}-svc",
                "deployment_exists": False,
                "service_exists": True,
            }

        def runtime_pod_diagnostics(self, deployment, *, prefix="reconcile"):
            return {
                "reconcile_pod_names": ["app-123"],
                "reconcile_pod_describe_summary": "app-123: Pod Events: | Warning  Failed  kubelet  Node lost",
                "reconcile_pod_logs_summary": "app-123: last known log line",
            }

        def cleanup_workspace(self, deployment):
            return {"workspace_removed": False, "log_removed": False}

    import worker.reconcile as reconcile_module

    monkeypatch.setattr(reconcile_module, "create_executor_for_deployment", lambda deployment: MissingDeploymentExecutor())

    with app.app_context():
        changes = reconcile_deployments()
        assert changes == 1
        updated = db.session.get(PlatformDeployment, deployment_id)
        assert updated.status == "failed"
        assert "Deployment" in updated.last_error
        event = next(event for event in updated.events if event.event_type == "reconcile.kubernetes_missing_resource")
        assert event.step == "reconcile.kubernetes_missing_resource"
        assert "Deployment" in event.message
        assert event.metadata_json["namespace"] == "default"
        assert event.metadata_json["deployment_exists"] is False
        assert event.metadata_json["service_exists"] is True
        assert event.metadata_json["reconcile_pod_names"] == ["app-123"]
        assert event.metadata_json["reconcile_pod_describe_summary"] == "app-123: Pod Events: | Warning  Failed  kubelet  Node lost"
        assert event.metadata_json["reconcile_pod_logs_summary"] == "app-123: last known log line"


def test_reconciler_marks_running_kubernetes_deployment_failed_when_service_missing(client, app, monkeypatch):
    deployment_payload = create_deployment(client, name="k8s-missing-service", status="running", build_status="succeeded")
    deployment_id = deployment_payload["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        deployment.service_url = "http://paas-k8s-missing-service-1-svc.default.svc.cluster.local:5000"
        db.session.commit()

    class MissingServiceExecutor:
        def runtime_resource_status(self, deployment):
            return {
                "namespace": "default",
                "deployment_name": f"paas-{deployment.project.name}-{deployment.id}",
                "service_name": f"paas-{deployment.project.name}-{deployment.id}-svc",
                "deployment_exists": True,
                "service_exists": False,
            }

        def cleanup_workspace(self, deployment):
            return {"workspace_removed": False, "log_removed": False}

    import worker.reconcile as reconcile_module

    monkeypatch.setattr(reconcile_module, "create_executor_for_deployment", lambda deployment: MissingServiceExecutor())

    with app.app_context():
        changes = reconcile_deployments()
        assert changes == 1
        updated = db.session.get(PlatformDeployment, deployment_id)
        assert updated.status == "failed"
        assert "Service" in updated.last_error
        event = next(event for event in updated.events if event.event_type == "reconcile.kubernetes_missing_resource")
        assert event.step == "reconcile.kubernetes_missing_resource"
        assert "Service" in event.message
        assert event.metadata_json["deployment_exists"] is True
        assert event.metadata_json["service_exists"] is False


def test_reconciler_records_cleanup_failed_when_kubernetes_resource_check_errors(client, app, monkeypatch):
    deployment_payload = create_deployment(client, name="k8s-check-error", status="running", build_status="succeeded")
    deployment_id = deployment_payload["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        db.session.commit()

    class BrokenKubernetesExecutor:
        def runtime_resource_status(self, deployment):
            raise WorkerExecutionError(
                "reconcile.kubernetes_resource_exists",
                "kubectl get failed",
                metadata={"namespace": "default", "deployment_name": "demo", "service_name": "demo-svc"},
            )

        def cleanup_workspace(self, deployment):
            return {"workspace_removed": False, "log_removed": False}

    import worker.reconcile as reconcile_module

    monkeypatch.setattr(reconcile_module, "create_executor_for_deployment", lambda deployment: BrokenKubernetesExecutor())

    with app.app_context():
        changes = reconcile_deployments()
        assert changes == 0
        updated = db.session.get(PlatformDeployment, deployment_id)
        assert updated.status == "running"
        event = next(event for event in updated.events if event.event_type == "reconcile.cleanup_failed")
        assert "kubectl get failed" in event.message


def test_reconciler_removes_leftover_kubernetes_resources_from_failed_deployment(client, app, monkeypatch):
    deployment_payload = create_deployment(client, name="k8s-failed-leftover", status="failed", build_status="failed")
    deployment_id = deployment_payload["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        db.session.commit()

    class LeftoverKubernetesExecutor:
        def runtime_resource_status(self, deployment):
            return {
                "namespace": "default",
                "deployment_name": f"paas-{deployment.project.name}-{deployment.id}",
                "service_name": f"paas-{deployment.project.name}-{deployment.id}-svc",
                "deployment_exists": True,
                "service_exists": True,
            }

        def runtime_pod_diagnostics(self, deployment, *, prefix="reconcile_cleanup"):
            return {
                "reconcile_cleanup_pod_names": ["app-456"],
                "reconcile_cleanup_pod_describe_summary": "app-456: Pod Events: | Normal  Killing  kubelet  Stopping container",
            }

        def stop(self, deployment):
            return ExecutionResult(
                "Kubernetes resources deleted successfully.",
                metadata={
                    "executor": "kubernetes",
                    "deployment_name": f"paas-{deployment.project.name}-{deployment.id}",
                    "service_name": f"paas-{deployment.project.name}-{deployment.id}-svc",
                    "namespace": "default",
                    "stopped": True,
                },
                log_path="/tmp/reconcile-k8s-delete.log",
            )

        def cleanup_workspace(self, deployment):
            return {"workspace_removed": False, "log_removed": False}

    import worker.reconcile as reconcile_module

    monkeypatch.setattr(reconcile_module, "create_executor_for_deployment", lambda deployment: LeftoverKubernetesExecutor())

    with app.app_context():
        changes = reconcile_deployments()
        assert changes == 1
        updated = db.session.get(PlatformDeployment, deployment_id)
        event = next(event for event in updated.events if event.event_type == "reconcile.kubernetes_resources_removed")
        assert event.metadata_json["namespace"] == "default"
        assert event.metadata_json["leftover_resources"] == ["Deployment", "Service"]
        assert event.metadata_json["reconcile_cleanup_pod_names"] == ["app-456"]
        assert (
            event.metadata_json["reconcile_cleanup_pod_describe_summary"]
            == "app-456: Pod Events: | Normal  Killing  kubelet  Stopping container"
        )


def test_reconciler_removes_leftover_kubernetes_resources_from_stopped_deployment(client, app, monkeypatch):
    deployment_payload = create_deployment(client, name="k8s-stopped-leftover", status="stopped", build_status="succeeded")
    deployment_id = deployment_payload["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        db.session.commit()

    class LeftoverStoppedKubernetesExecutor:
        def runtime_resource_status(self, deployment):
            return {
                "namespace": "default",
                "deployment_name": f"paas-{deployment.project.name}-{deployment.id}",
                "service_name": f"paas-{deployment.project.name}-{deployment.id}-svc",
                "deployment_exists": True,
                "service_exists": False,
            }

        def stop(self, deployment):
            return ExecutionResult(
                "Kubernetes resources deleted successfully.",
                metadata={
                    "executor": "kubernetes",
                    "deployment_name": f"paas-{deployment.project.name}-{deployment.id}",
                    "service_name": f"paas-{deployment.project.name}-{deployment.id}-svc",
                    "namespace": "default",
                    "stopped": True,
                },
                log_path="/tmp/reconcile-k8s-delete.log",
            )

        def cleanup_workspace(self, deployment):
            return {"workspace_removed": False, "log_removed": False}

    import worker.reconcile as reconcile_module

    monkeypatch.setattr(reconcile_module, "create_executor_for_deployment", lambda deployment: LeftoverStoppedKubernetesExecutor())

    with app.app_context():
        changes = reconcile_deployments()
        assert changes == 1
        updated = db.session.get(PlatformDeployment, deployment_id)
        event = next(event for event in updated.events if event.event_type == "reconcile.kubernetes_resources_removed")
        assert event.metadata_json["leftover_resources"] == ["Deployment"]


def test_reconciler_records_cleanup_failed_when_kubernetes_resource_removal_errors(client, app, monkeypatch):
    deployment_payload = create_deployment(client, name="k8s-leftover-error", status="failed", build_status="failed")
    deployment_id = deployment_payload["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        db.session.commit()

    class BrokenLeftoverKubernetesExecutor:
        def runtime_resource_status(self, deployment):
            return {
                "namespace": "default",
                "deployment_name": f"paas-{deployment.project.name}-{deployment.id}",
                "service_name": f"paas-{deployment.project.name}-{deployment.id}-svc",
                "deployment_exists": True,
                "service_exists": True,
            }

        def stop(self, deployment):
            raise WorkerExecutionError(
                "deploy.kubernetes.delete",
                "kubectl delete failed",
                metadata={"namespace": "default"},
            )

        def cleanup_workspace(self, deployment):
            return {"workspace_removed": False, "log_removed": False}

    import worker.reconcile as reconcile_module

    monkeypatch.setattr(reconcile_module, "create_executor_for_deployment", lambda deployment: BrokenLeftoverKubernetesExecutor())

    with app.app_context():
        changes = reconcile_deployments()
        assert changes == 0
        updated = db.session.get(PlatformDeployment, deployment_id)
        event = next(event for event in updated.events if event.event_type == "reconcile.cleanup_failed")
        assert "kubectl delete failed" in event.message
