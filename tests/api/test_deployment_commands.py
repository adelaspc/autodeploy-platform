from control_plane.extensions import db
from control_plane.models import DeploymentCommand, PlatformDeployment
from tests.api.project_test_helpers import create_project, event_by_type, process_queued_command
from worker.execution.contracts import ExecutionResult, WorkerExecutionError
import worker.processing.command_processor as worker_commands
import pytest


def test_stop_deployment_calls_executor_and_records_events(client, app, monkeypatch):
    project_response = create_project(client, name="stoppable-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": "abc123def456",
            "status": "running",
            "build_status": "succeeded",
            "service_url": "http://127.0.0.1:18080",
        },
    )
    deployment_id = deployment_response.get_json()["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "local-docker"
        deployment.container_name = "paas-stoppable-app-1"
        deployment.container_id = "container123"
        deployment.host_port = 18080
        deployment.healthcheck_url = "http://127.0.0.1:18080/health"
        db.session.commit()

    class StopExecutor:
        def stop(self, deployment):
            return ExecutionResult(
                "Container removed successfully.",
                metadata={
                    "executor": "local-docker",
                    "stopped": True,
                    "runtime_log_path": "/tmp/runtime.log",
                    "runtime_log_summary": "app stopped cleanly",
                },
                log_path="/tmp/stop.log",
                deploy_target="local-docker",
                container_name=deployment.container_name,
                container_id=deployment.container_id,
            )

    monkeypatch.setattr(worker_commands, "create_executor_for_deployment", lambda deployment: StopExecutor())

    response = client.patch(
        f"/api/projects/{project_id}/deployments/{deployment_id}",
        json={"status": "stopped", "message": "Stop requested"},
    )

    assert response.status_code == 202
    assert response.get_json()["command"]["status"] == "pending"
    payload = process_queued_command(app, client, project_id, deployment_id)
    assert payload["status"] == "stopped"
    assert payload["service_url"] is None
    assert payload["container_name"] is None
    assert payload["container_id"] is None
    assert payload["host_port"] is None
    assert event_by_type(payload["events"], "deployment.stop_started")
    stopped_event = event_by_type(payload["events"], "deployment.stopped")
    assert stopped_event["metadata_json"]["log_path"] == "/tmp/stop.log"
    assert stopped_event["metadata_json"]["runtime_log_path"] == "/tmp/runtime.log"


def test_stop_deployment_endpoint_calls_executor_and_records_audit_event(client, app, monkeypatch):
    project_response = create_project(client, name="deployer-stop-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": "abc123def456",
            "status": "running",
            "build_status": "succeeded",
            "service_url": "http://127.0.0.1:18080",
        },
    )
    deployment_id = deployment_response.get_json()["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "local-docker"
        deployment.container_name = "paas-deployer-stop-app-1"
        deployment.container_id = "container123"
        db.session.commit()

    class StopExecutor:
        def stop(self, deployment):
            return ExecutionResult(
                "Container removed successfully.",
                metadata={"executor": "local-docker", "stopped": True},
                log_path="/tmp/stop.log",
                deploy_target="local-docker",
                container_name=deployment.container_name,
                container_id=deployment.container_id,
            )

    monkeypatch.setattr(worker_commands, "create_executor_for_deployment", lambda deployment: StopExecutor())

    response = client.post(
        f"/api/projects/{project_id}/deployments/{deployment_id}/stop",
        json={"message": "Stop requested by deployer"},
    )

    assert response.status_code == 202
    payload = process_queued_command(app, client, project_id, deployment_id)
    assert payload["status"] == "stopped"
    assert payload["service_url"] is None
    started_event = event_by_type(payload["events"], "deployment.stop_started")
    assert started_event["message"] == "Stop requested by deployer"
    assert event_by_type(payload["events"], "deployment.stopped")

    audit_events = client.get("/api/audit-events").get_json()["items"]
    assert audit_events[0]["action"] == "deployment.stop_requested"
    assert audit_events[0]["resource_id"] == str(deployment_id)


def test_stop_deployment_endpoint_rejects_non_stop_mutations(client):
    project_response = create_project(client, name="invalid-stop-payload-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": "abc123def456",
            "status": "running",
            "build_status": "succeeded",
        },
    )
    deployment_id = deployment_response.get_json()["id"]

    response = client.post(
        f"/api/projects/{project_id}/deployments/{deployment_id}/stop",
        json={"status": "failed"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "Unsupported stop fields: status"


def test_cleanup_kubernetes_deployment_removes_resources_and_preserves_history(client, app, monkeypatch):
    project_id = create_project(client, name="cleanup-k8s-app").get_json()["id"]
    deployment_id = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": "abc123def456",
            "status": "running",
            "build_status": "succeeded",
            "service_url": "http://paas-deployment-a.127.0.0.1.nip.io",
        },
    ).get_json()["id"]
    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        deployment.healthcheck_url = f"{deployment.service_url}/health"
        db.session.commit()

    class CleanupExecutor:
        def stop(self, _deployment):
            return ExecutionResult(
                "Kubernetes resources deleted successfully.",
                metadata={"executor": "kubernetes", "stopped": True},
                events=[{
                    "event_type": "kubernetes.resources_deleted",
                    "status": "stopped",
                    "message": "Kubernetes resources deleted successfully",
                    "step": "deploy.kubernetes.delete",
                }],
                deploy_target="kubernetes",
            )

    monkeypatch.setattr(
        worker_commands,
        "create_executor_for_deployment",
        lambda _deployment: CleanupExecutor(),
    )

    response = client.post(
        f"/api/projects/{project_id}/deployments/{deployment_id}/cleanup",
        json={"message": "Remove old demo resources"},
    )

    assert response.status_code == 202
    payload = process_queued_command(app, client, project_id, deployment_id)
    assert payload["status"] == "stopped"
    assert payload["service_url"] is None
    assert payload["healthcheck_url"] is None
    assert [event["event_type"] for event in payload["events"][-3:]] == [
        "deployment.cleanup_started",
        "kubernetes.resources_deleted",
        "deployment.cleanup_succeeded",
    ]
    audit_events = client.get("/api/audit-events").get_json()["items"]
    assert audit_events[0]["action"] == "deployment.cleanup_requested"


@pytest.mark.parametrize(
    ("command_type", "endpoint", "event_type"),
    [
        ("stop", "stop", "deployment.stop_skipped"),
        ("cleanup", "cleanup", "deployment.cleanup_skipped"),
    ],
)
def test_historical_helm_commands_skip_shared_release_owned_by_newer_deployment(
    client, app, monkeypatch, command_type, endpoint, event_type
):
    project_id = create_project(client, name=f"historical-helm-{command_type}").get_json()["id"]
    old_deployment_id = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "running", "build_status": "succeeded"},
    ).get_json()["id"]
    newer_deployment_id = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "def456abc123", "status": "running", "build_status": "succeeded"},
    ).get_json()["id"]
    with app.app_context():
        deployment = db.session.get(PlatformDeployment, old_deployment_id)
        deployment.deploy_target = "kubernetes"
        deployment.helm_release_name = "paas-historical-helm-production-1"
        deployment.helm_namespace = "default"
        db.session.commit()

    class UnsafeHelmExecutor:
        def stop(self, _deployment):
            raise AssertionError("a historical command must not uninstall the shared Helm release")

    monkeypatch.setattr(worker_commands, "create_executor_for_deployment", lambda _deployment: UnsafeHelmExecutor())

    response = client.post(f"/api/projects/{project_id}/deployments/{old_deployment_id}/{endpoint}")

    assert response.status_code == 202
    payload = process_queued_command(app, client, project_id, old_deployment_id)
    assert payload["status"] == "running"
    skipped_event = event_by_type(payload["events"], event_type)
    assert skipped_event["metadata_json"]["newer_deployment_id"] == newer_deployment_id
    assert skipped_event["metadata_json"]["reason"] == "newer_deployment_owns_shared_helm_release"
    with app.app_context():
        command = db.session.scalar(
            db.select(DeploymentCommand).where(
                DeploymentCommand.deployment_id == old_deployment_id,
                DeploymentCommand.command_type == command_type,
            )
        )
        assert command.status == "skipped"
        assert command.active_key is None


def test_newest_helm_cleanup_uninstalls_shared_release(client, app, monkeypatch):
    project_id = create_project(client, name="newest-helm-cleanup").get_json()["id"]
    deployment_id = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "running", "build_status": "succeeded"},
    ).get_json()["id"]
    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        deployment.helm_release_name = "paas-newest-helm-cleanup-production-1"
        deployment.helm_namespace = "default"
        db.session.commit()

    class HelmExecutor:
        calls = 0

        def stop(self, deployment):
            self.calls += 1
            return ExecutionResult(
                "Helm release uninstalled successfully.",
                metadata={"deployment_mode": "helm", "helm_release_name": deployment.helm_release_name},
                deploy_target="kubernetes",
            )

    executor = HelmExecutor()
    monkeypatch.setattr(worker_commands, "create_executor_for_deployment", lambda _deployment: executor)

    response = client.post(f"/api/projects/{project_id}/deployments/{deployment_id}/cleanup")

    assert response.status_code == 202
    payload = process_queued_command(app, client, project_id, deployment_id)
    assert executor.calls == 1
    assert payload["status"] == "stopped"
    assert event_by_type(payload["events"], "deployment.cleanup_succeeded")


def test_historical_manifest_cleanup_remains_attempt_specific(client, app, monkeypatch):
    project_id = create_project(client, name="historical-manifest-cleanup").get_json()["id"]
    old_deployment_id = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "running", "build_status": "succeeded"},
    ).get_json()["id"]
    client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "def456abc123", "status": "running", "build_status": "succeeded"},
    )
    with app.app_context():
        deployment = db.session.get(PlatformDeployment, old_deployment_id)
        deployment.deploy_target = "kubernetes"
        db.session.commit()

    class ManifestExecutor:
        calls = 0

        def stop(self, _deployment):
            self.calls += 1
            return ExecutionResult("Kubernetes resources deleted successfully.", deploy_target="kubernetes")

    executor = ManifestExecutor()
    monkeypatch.setattr(worker_commands, "create_executor_for_deployment", lambda _deployment: executor)

    response = client.post(f"/api/projects/{project_id}/deployments/{old_deployment_id}/cleanup")

    assert response.status_code == 202
    payload = process_queued_command(app, client, project_id, old_deployment_id)
    assert executor.calls == 1
    assert payload["status"] == "stopped"


def test_stop_command_is_deduplicated_while_active(client, app):
    project_id = create_project(client, name="deduplicated-stop-app").get_json()["id"]
    deployment_id = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "running", "build_status": "succeeded"},
    ).get_json()["id"]
    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "local-docker"
        db.session.commit()

    first = client.post(f"/api/projects/{project_id}/deployments/{deployment_id}/stop")
    second = client.post(f"/api/projects/{project_id}/deployments/{deployment_id}/stop")

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.get_json()["command"]["id"] == first.get_json()["command"]["id"]
    with app.app_context():
        commands = db.session.scalars(
            db.select(DeploymentCommand).where(DeploymentCommand.deployment_id == deployment_id)
        ).all()
        assert len(commands) == 1
        assert commands[0].active_key == "active"


def test_stop_command_heartbeats_and_rejects_stale_reclaim(client, app, monkeypatch):
    project_id = create_project(client, name="heartbeat-stop-app").get_json()["id"]
    deployment_id = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "running", "build_status": "succeeded"},
    ).get_json()["id"]
    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "local-docker"
        db.session.commit()
    client.post(f"/api/projects/{project_id}/deployments/{deployment_id}/stop")

    class HeartbeatingStopExecutor:
        def set_heartbeat(self, heartbeat):
            self.heartbeat = heartbeat

        def stop(self, deployment):
            self.heartbeat()
            app.config["CONTROL_PLANE_WORKER_ID"] = "worker-b"
            try:
                assert worker_commands.claim_next_pending_command(claim_ttl_seconds=30) is None
            finally:
                app.config["CONTROL_PLANE_WORKER_ID"] = "worker"
            return ExecutionResult("Stopped", deploy_target=deployment.deploy_target)

    monkeypatch.setattr(
        worker_commands,
        "create_executor_for_deployment",
        lambda _deployment: HeartbeatingStopExecutor(),
    )

    payload = process_queued_command(app, client, project_id, deployment_id)

    assert payload["status"] == "stopped"
    with app.app_context():
        command = db.session.scalar(
            db.select(DeploymentCommand).where(DeploymentCommand.deployment_id == deployment_id)
        )
        assert command.status == "succeeded"
        assert command.active_key is None
        assert command.claimed_by is None
        assert command.claimed_at is None


def test_stop_command_does_not_persist_result_after_claim_loss(client, app, monkeypatch):
    project_id = create_project(client, name="claim-loss-stop-app").get_json()["id"]
    deployment_id = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "running", "build_status": "succeeded"},
    ).get_json()["id"]
    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "local-docker"
        db.session.commit()
    client.post(f"/api/projects/{project_id}/deployments/{deployment_id}/stop")

    class ClaimLosingStopExecutor:
        def stop(self, deployment):
            command = db.session.scalar(
                db.select(DeploymentCommand).where(DeploymentCommand.deployment_id == deployment.id)
            )
            command.claimed_by = "other-worker"
            command.claimed_at = worker_commands.now_utc()
            db.session.commit()
            return ExecutionResult("Stopped", deploy_target=deployment.deploy_target)

    monkeypatch.setattr(
        worker_commands,
        "create_executor_for_deployment",
        lambda _deployment: ClaimLosingStopExecutor(),
    )

    payload = process_queued_command(app, client, project_id, deployment_id)

    assert payload["status"] == "running"
    with app.app_context():
        command = db.session.scalar(
            db.select(DeploymentCommand).where(DeploymentCommand.deployment_id == deployment_id)
        )
        assert command.status == "claimed"
        assert command.claimed_by == "other-worker"
        assert command.active_key == "active"


def test_unexpected_stop_exception_marks_command_failed(client, app, monkeypatch):
    project_id = create_project(client, name="unexpected-stop-app").get_json()["id"]
    deployment_id = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "running", "build_status": "succeeded"},
    ).get_json()["id"]
    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "local-docker"
        db.session.commit()
    client.post(f"/api/projects/{project_id}/deployments/{deployment_id}/stop")

    class UnexpectedStopExecutor:
        def stop(self, _deployment):
            raise RuntimeError("sensitive stop implementation detail")

    monkeypatch.setattr(
        worker_commands,
        "create_executor_for_deployment",
        lambda _deployment: UnexpectedStopExecutor(),
    )

    payload = process_queued_command(app, client, project_id, deployment_id)

    assert payload["status"] == "running"
    with app.app_context():
        command = db.session.scalar(
            db.select(DeploymentCommand).where(DeploymentCommand.deployment_id == deployment_id)
        )
        assert command.status == "failed"
        assert command.active_key is None
        assert command.claimed_by is None
        assert command.last_error == "Worker encountered an unexpected internal error"
        failure_event = next(
            event for event in command.deployment.events if event.event_type == "deployment.stop_failed"
        )
        assert failure_event.metadata_json["error_type"] == "RuntimeError"
        assert "sensitive stop implementation detail" not in failure_event.message


def test_cleanup_rejects_non_kubernetes_deployment(client, app):
    project_id = create_project(client, name="cleanup-docker-app").get_json()["id"]
    deployment_id = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "failed", "build_status": "failed"},
    ).get_json()["id"]
    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "local-docker"
        db.session.commit()

    response = client.post(f"/api/projects/{project_id}/deployments/{deployment_id}/cleanup")

    assert response.status_code == 409
    assert response.get_json()["error"] == "Cleanup is available only for Kubernetes deployments"


def test_stop_deployment_persists_helm_runtime_metadata(client, app, monkeypatch):
    project_response = create_project(client, name="helm-stop-metadata-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": "abc123def456",
            "status": "running",
            "build_status": "succeeded",
            "service_url": "http://helm-stop-metadata-app.apps.svc.cluster.local:5000",
        },
    )
    deployment_id = deployment_response.get_json()["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        db.session.commit()

    class StopExecutor:
        def stop(self, deployment):
            return ExecutionResult(
                "Helm release uninstalled successfully.",
                metadata={
                    "deployment_mode": "helm",
                    "helm_release_name": "paas-helm-stop-metadata-app-production-1",
                    "namespace": "apps",
                    "chart_path": "deploy/helm/generic-web-app",
                    "stopped": True,
                },
                log_path="/tmp/helm-uninstall.log",
                deploy_target="kubernetes",
            )

    monkeypatch.setattr(worker_commands, "create_executor_for_deployment", lambda deployment: StopExecutor())

    response = client.patch(
        f"/api/projects/{project_id}/deployments/{deployment_id}",
        json={"status": "stopped"},
    )

    assert response.status_code == 202
    payload = process_queued_command(app, client, project_id, deployment_id)
    assert payload["status"] == "stopped"
    assert payload["helm_release_name"] == "paas-helm-stop-metadata-app-production-1"
    assert payload["helm_namespace"] == "apps"
    assert payload["helm_chart_path"] == "deploy/helm/generic-web-app"


def test_stop_deployment_returns_error_when_cleanup_fails(client, app, monkeypatch):
    project_response = create_project(client, name="broken-stop-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": "abc123def456",
            "status": "running",
            "build_status": "succeeded",
            "service_url": "http://127.0.0.1:18081",
        },
    )
    deployment_id = deployment_response.get_json()["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "local-docker"
        deployment.container_name = "paas-broken-stop-app-1"
        db.session.commit()

    class BrokenStopExecutor:
        def stop(self, deployment):
            raise WorkerExecutionError(
                "deploy.container_stop",
                "Failed to remove container",
                metadata={"container_name": deployment.container_name},
            )

    monkeypatch.setattr(
        worker_commands, "create_executor_for_deployment", lambda deployment: BrokenStopExecutor()
    )

    response = client.patch(
        f"/api/projects/{project_id}/deployments/{deployment_id}",
        json={"status": "stopped"},
    )

    assert response.status_code == 202
    payload = process_queued_command(app, client, project_id, deployment_id)
    assert payload["status"] == "running"
    with app.app_context():
        command = db.session.scalar(db.select(DeploymentCommand).where(DeploymentCommand.deployment_id == deployment_id))
        assert command.status == "failed"
        assert command.last_error == "Failed to remove container"
