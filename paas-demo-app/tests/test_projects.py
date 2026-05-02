from backend.api import projects as projects_api
from backend.extensions import db
from backend.models import DeploymentEvent, PlatformDeployment
from worker.executor import ExecutionResult, WorkerExecutionError


def create_project(client, **overrides):
    payload = {
        "name": "paas-control-plane",
        "repo_url": "https://github.com/example/paas-control-plane",
        "branch": "main",
        "dockerfile_path": "Dockerfile",
        "build_context": ".",
        "port": 5000,
        "healthcheck_path": "/health",
        "env_vars": [{"name": "DATABASE_URL", "required": True}],
        "migration_command": "flask db upgrade",
        "cpu": "250m",
        "memory": "512Mi",
        "trigger": "manual",
        "runtime": "dockerfile",
    }
    payload.update(overrides)
    return client.post("/api/projects", json=payload)


def test_create_and_list_projects(client):
    create_response = create_project(client)

    assert create_response.status_code == 201
    created = create_response.get_json()
    assert created["name"] == "paas-control-plane"
    assert created["repo_url"] == "https://github.com/example/paas-control-plane"

    list_response = client.get("/api/projects")
    assert list_response.status_code == 200
    payload = list_response.get_json()
    assert len(payload) == 1
    assert payload[0]["name"] == "paas-control-plane"


def test_create_project_rejects_invalid_trigger(client):
    response = create_project(client, trigger="cron")

    assert response.status_code == 400
    assert "Invalid trigger" in response.get_json()["error"]


def test_create_project_requires_unique_name(client):
    assert create_project(client).status_code == 201

    duplicate_response = create_project(client)

    assert duplicate_response.status_code == 409
    assert duplicate_response.get_json() == {"error": "Project name must be unique"}


def test_update_project(client):
    project_response = create_project(client, name="editable-project")
    project_id = project_response.get_json()["id"]

    update_response = client.patch(
        f"/api/projects/{project_id}",
        json={
            "branch": "develop",
            "port": 8080,
            "trigger": "github_push",
        },
    )

    assert update_response.status_code == 200
    updated = update_response.get_json()
    assert updated["branch"] == "develop"
    assert updated["port"] == 8080
    assert updated["trigger"] == "github_push"


def test_delete_project(client):
    project_response = create_project(client, name="temporary-project")
    project_id = project_response.get_json()["id"]

    delete_response = client.delete(f"/api/projects/{project_id}")

    assert delete_response.status_code == 200
    assert delete_response.get_json() == {"message": "Project deleted"}

    get_response = client.get(f"/api/projects/{project_id}")
    assert get_response.status_code == 404


def test_trigger_deployment_creates_build_and_event_history(client):
    project_response = create_project(client, name="deployable-app")
    project_id = project_response.get_json()["id"]

    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": "abc123def456",
            "registry": "ghcr.io/example",
            "image_name": "deployable-app",
            "image_tag": "abc123def456",
            "build_status": "pending",
            "status": "pending",
            "test_command": "pytest -q",
        },
    )

    assert deployment_response.status_code == 201
    deployment = deployment_response.get_json()
    assert deployment["project_id"] == project_id
    assert deployment["status"] == "pending"
    assert deployment["build"]["commit_sha"] == "abc123def456"
    assert deployment["build"]["image_ref"] == "ghcr.io/example/deployable-app:abc123def456"
    assert len(deployment["events"]) == 1
    assert deployment["events"][0]["event_type"] == "deployment.created"

    deployments_response = client.get(f"/api/projects/{project_id}/deployments")
    assert deployments_response.status_code == 200
    deployments = deployments_response.get_json()
    assert len(deployments) == 1
    assert deployments[0]["build"]["image_tag"] == "abc123def456"

    builds_response = client.get(f"/api/projects/{project_id}/builds")
    assert builds_response.status_code == 200
    builds = builds_response.get_json()
    assert len(builds) == 1
    assert builds[0]["status"] == "pending"

    events_response = client.get(f"/api/projects/{project_id}/deployments/{deployment['id']}/events")
    assert events_response.status_code == 200
    events = events_response.get_json()
    assert len(events) == 1
    assert events[0]["status"] == "pending"


def test_update_deployment_status_tracks_transition_and_events(client):
    project_response = create_project(client, name="transition-app")
    project_id = project_response.get_json()["id"]

    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": "abc123def456",
            "status": "pending",
        },
    )
    deployment_id = deployment_response.get_json()["id"]

    update_response = client.patch(
        f"/api/projects/{project_id}/deployments/{deployment_id}",
        json={
            "status": "cloning",
            "build_status": "cloning",
            "message": "Worker started cloning repository",
        },
    )

    assert update_response.status_code == 200
    updated = update_response.get_json()
    assert updated["status"] == "cloning"
    assert updated["build"]["status"] == "cloning"
    assert "building" in updated["allowed_transitions"]
    assert len(updated["events"]) == 3
    assert updated["events"][-2]["event_type"] == "deployment.status_updated"
    assert updated["events"][-1]["event_type"] == "build.status_updated"


def test_update_deployment_rejects_invalid_transition(client):
    project_response = create_project(client, name="invalid-transition-app")
    project_id = project_response.get_json()["id"]

    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": "abc123def456",
            "status": "pending",
        },
    )
    deployment_id = deployment_response.get_json()["id"]

    update_response = client.patch(
        f"/api/projects/{project_id}/deployments/{deployment_id}",
        json={"status": "running"},
    )

    assert update_response.status_code == 400
    assert "Invalid deployment transition" in update_response.get_json()["error"]


def test_trigger_deployment_rejects_missing_commit_sha(client):
    project_response = create_project(client, name="invalid-deploy-app")
    project_id = project_response.get_json()["id"]

    response = client.post(f"/api/projects/{project_id}/deployments", json={"status": "pending"})

    assert response.status_code == 400
    assert response.get_json() == {"error": "Missing required field: commit_sha"}


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

    monkeypatch.setattr(projects_api, "create_executor_for_deployment", lambda deployment: StopExecutor())

    response = client.patch(
        f"/api/projects/{project_id}/deployments/{deployment_id}",
        json={"status": "stopped", "message": "Stop requested"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "stopped"
    assert payload["service_url"] is None
    assert payload["container_name"] == "paas-stoppable-app-1"
    assert payload["events"][-2]["event_type"] == "deployment.stop_started"
    assert payload["events"][-1]["event_type"] == "deployment.stopped"
    assert payload["events"][-1]["metadata_json"]["log_path"] == "/tmp/stop.log"
    assert payload["events"][-1]["metadata_json"]["runtime_log_path"] == "/tmp/runtime.log"


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

    monkeypatch.setattr(projects_api, "create_executor_for_deployment", lambda deployment: BrokenStopExecutor())

    response = client.patch(
        f"/api/projects/{project_id}/deployments/{deployment_id}",
        json={"status": "stopped"},
    )

    assert response.status_code == 409
    assert response.get_json()["error"] == "Failed to remove container"


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
