from datetime import timedelta

from backend.extensions import db
from backend.models import PlatformDeployment
from worker.executor import ExecutionResult
from worker.reconcile import reconcile_deployments
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
