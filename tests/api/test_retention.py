from datetime import datetime, timedelta, timezone

from control_plane.extensions import db
from control_plane.models import AuditEvent, DeploymentEvent, PlatformDeployment
from control_plane.retention import cleanup_observability_data
from tests.api.test_projects import create_project


def create_deployment(client, *, name, status):
    project = create_project(client, name=name).get_json()
    response = client.post(
        f"/api/projects/{project['id']}/deployments",
        json={
            "commit_sha": "abc123def456",
            "image_name": name,
            "image_tag": "test",
            "status": status,
            "build_status": "failed" if status == "failed" else "succeeded",
        },
    )
    return response.get_json()["id"]


def prepare_old_deployment(app, deployment_id, workspace_root, *, now):
    deployment = db.session.get(PlatformDeployment, deployment_id)
    deployment.updated_at = now - timedelta(days=45)
    workspace = workspace_root / f"project-{deployment.project_id}" / f"deployment-{deployment.id}"
    logs = workspace / "logs"
    logs.mkdir(parents=True)
    build_log = logs / "build.log"
    build_log.write_text("old build output", encoding="utf-8")
    runtime_log = logs / "runtime.log"
    runtime_log.write_text("old runtime output", encoding="utf-8")
    deployment.build.workspace_path = str(workspace)
    deployment.build.log_path = str(build_log)
    deployment.build.build_log_path = str(build_log)
    db.session.add_all(
        [
            DeploymentEvent(
                deployment=deployment,
                event_type="claim_acquired",
                status=deployment.status,
                message="disposable",
            ),
            DeploymentEvent(
                deployment=deployment,
                event_type="deployment.failed",
                status=deployment.status,
                message="lifecycle history",
                metadata_json={"runtime_log_path": str(runtime_log)},
            ),
        ]
    )
    db.session.commit()
    return workspace


def test_cleanup_observability_defaults_to_dry_run(client, app, tmp_path):
    now = datetime.now(timezone.utc)
    app.config["CONTROL_PLANE_WORKSPACE_ROOT"] = str(tmp_path)
    deployment_id = create_deployment(client, name="retention-dry-run", status="failed")

    with app.app_context():
        workspace = prepare_old_deployment(app, deployment_id, tmp_path, now=now)
        result = cleanup_observability_data(older_than_days=30, now=now)

        assert result["workspace_candidates"] == 1
        assert result["disposable_event_candidates"] == 1
        assert workspace.exists()
        assert DeploymentEvent.query.filter_by(event_type="claim_acquired").count() == 1


def test_cleanup_observability_removes_only_safe_disposable_data(client, app, tmp_path):
    now = datetime.now(timezone.utc)
    app.config["CONTROL_PLANE_WORKSPACE_ROOT"] = str(tmp_path)
    deployment_id = create_deployment(client, name="retention-apply", status="failed")

    with app.app_context():
        workspace = prepare_old_deployment(app, deployment_id, tmp_path, now=now)
        result = cleanup_observability_data(older_than_days=30, apply=True, now=now)

        deployment = db.session.get(PlatformDeployment, deployment_id)
        assert result["workspaces_removed"] == 1
        assert result["disposable_events_removed"] == 1
        assert not workspace.exists()
        assert deployment.build.workspace_path is None
        assert deployment.build.log_path is None
        assert deployment.build.build_log_path is None
        assert DeploymentEvent.query.filter_by(event_type="claim_acquired").count() == 0
        assert DeploymentEvent.query.filter_by(event_type="deployment.failed").count() == 1
        retention_event = DeploymentEvent.query.filter_by(event_type="observability.artifacts_removed").one()
        assert retention_event.metadata_json["removed_artifacts"] == ["build_log", "runtime_log"]
        project_id = deployment.project_id

    summary = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/summary").get_json()
    assert summary["build_log_available"] is False
    assert summary["runtime_log_available"] is False
    assert summary["build_log_state"] == "retention_removed"
    assert summary["runtime_log_state"] == "retention_removed"


def test_cleanup_observability_requires_explicit_audit_retention(client, app, tmp_path):
    now = datetime.now(timezone.utc)
    old = AuditEvent(
        action="project.created",
        resource_type="project",
        status="success",
        created_at=now - timedelta(days=45),
    )
    with app.app_context():
        db.session.add(old)
        db.session.commit()

        cleanup_observability_data(older_than_days=30, apply=True, now=now)
        assert AuditEvent.query.count() == 1

        result = cleanup_observability_data(
            older_than_days=30,
            apply=True,
            include_audit_events=True,
            now=now,
        )
        assert result["audit_events_removed"] == 1
        assert AuditEvent.query.count() == 0


def test_cleanup_observability_cli_is_dry_run_by_default(client, app, tmp_path):
    now = datetime.now(timezone.utc)
    app.config["CONTROL_PLANE_WORKSPACE_ROOT"] = str(tmp_path)
    deployment_id = create_deployment(client, name="retention-cli", status="stopped")
    with app.app_context():
        workspace = prepare_old_deployment(app, deployment_id, tmp_path, now=now)

    result = app.test_cli_runner().invoke(args=["cleanup-observability", "--older-than-days", "30"])

    assert result.exit_code == 0
    assert "DRY-RUN" in result.output
    assert workspace.exists()
