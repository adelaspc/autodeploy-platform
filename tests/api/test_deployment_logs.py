from control_plane.extensions import db
from control_plane.models import DeploymentEvent, PlatformDeployment
from tests.api.project_test_helpers import create_project


def test_get_runtime_log_returns_tailed_content(client, app, tmp_path):
    project_response = create_project(client, name="runtime-log-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": "abc123def456",
            "status": "running",
            "build_status": "succeeded",
            "service_url": "http://127.0.0.1:18082",
        },
    )
    deployment_id = deployment_response.get_json()["id"]
    runtime_log_path = tmp_path / "project-1" / "deployment-1" / "logs" / "runtime.log"
    runtime_log_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_log_path.write_text("line1\nline2\nline3\n", encoding="utf-8")

    with app.app_context():
        app.config["CONTROL_PLANE_WORKSPACE_ROOT"] = str(tmp_path)
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="deployment.apply_succeeded",
                step="deploy",
                level="info",
                status="running",
                message="runtime log available",
                metadata_json={"runtime_log_path": str(runtime_log_path)},
            )
        )
        db.session.commit()

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/runtime-log?tail_lines=2")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["path"] == str(runtime_log_path)
    assert payload["content"] == "line2\nline3"
    assert payload["line_count"] == 3
    assert payload["truncated"] is True


def test_get_runtime_log_rejects_paths_outside_allowed_roots(client, app, tmp_path):
    project_response = create_project(client, name="unsafe-runtime-log-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "running", "build_status": "succeeded"},
    )
    deployment_id = deployment_response.get_json()["id"]
    runtime_log_path = tmp_path / "outside.log"
    runtime_log_path.write_text("nope\n", encoding="utf-8")

    with app.app_context():
        app.config["CONTROL_PLANE_WORKSPACE_ROOT"] = str(tmp_path / "workspaces")
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="deployment.apply_succeeded",
                step="deploy",
                level="info",
                status="running",
                message="unsafe runtime log",
                metadata_json={"runtime_log_path": str(runtime_log_path)},
            )
        )
        db.session.commit()

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/runtime-log")

    assert response.status_code == 409
    assert response.get_json()["error"] == "Runtime log path is outside allowed log roots"


def test_get_build_log_returns_tailed_content(client, app, tmp_path):
    project_response = create_project(client, name="build-log-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": "abc123def456",
            "status": "failed",
            "build_status": "failed",
        },
    )
    deployment_id = deployment_response.get_json()["id"]
    build_log_path = tmp_path / "project-1" / "deployment-1" / "logs" / "build.log"
    latest_log_path = build_log_path.with_name("deploy.log")
    build_log_path.parent.mkdir(parents=True, exist_ok=True)
    build_log_path.write_text("clone\nbuild\ntest\npush\n", encoding="utf-8")
    latest_log_path.write_text("deploy\n", encoding="utf-8")

    with app.app_context():
        app.config["CONTROL_PLANE_WORKSPACE_ROOT"] = str(tmp_path)
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.build.build_log_path = str(build_log_path)
        deployment.build.log_path = str(latest_log_path)
        db.session.commit()

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/build-log?tail_lines=2")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["deployment_id"] == deployment_id
    assert payload["build_id"] == deployment_response.get_json()["build"]["id"]
    assert payload["path"] == str(build_log_path)
    assert payload["content"] == "test\npush"
    assert payload["line_count"] == 4
    assert payload["truncated"] is True


def test_get_build_log_returns_404_when_unavailable(client):
    project_response = create_project(client, name="missing-build-log-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": "abc123def456",
            "status": "failed",
            "build_status": "failed",
        },
    )
    deployment_id = deployment_response.get_json()["id"]

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/build-log")

    assert response.status_code == 404
    assert response.get_json() == {"error": "No build log is available for this deployment"}


def test_get_build_log_rejects_paths_outside_allowed_roots(client, app, tmp_path):
    project_response = create_project(client, name="unsafe-build-log-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": "abc123def456",
            "status": "failed",
            "build_status": "failed",
        },
    )
    deployment_id = deployment_response.get_json()["id"]
    build_log_path = tmp_path / "outside.log"
    build_log_path.write_text("nope\n", encoding="utf-8")

    with app.app_context():
        app.config["CONTROL_PLANE_WORKSPACE_ROOT"] = str(tmp_path / "workspaces")
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.build.log_path = str(build_log_path)
        db.session.commit()

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/build-log")

    assert response.status_code == 409
    assert response.get_json()["error"] == "Build log path is outside allowed log roots"
