from pathlib import Path

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


def test_create_project_with_git_token_config_returns_only_secret_ref(client):
    response = create_project(
        client,
        name="private-repo-app",
        repo_url="https://github.com/example/private-repo",
        git_auth_type="token",
        git_secret_ref="DEMO_APP",
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["git_auth_type"] == "token"
    assert payload["git_secret_ref"] == "DEMO_APP"
    assert "ghp_" not in str(payload)


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


def test_update_project_allows_git_token_configuration(client):
    project_response = create_project(client, name="editable-private-project")
    project_id = project_response.get_json()["id"]

    update_response = client.patch(
        f"/api/projects/{project_id}",
        json={
            "git_auth_type": "token",
            "git_secret_ref": "PRIVATE_APP",
        },
    )

    assert update_response.status_code == 200
    updated = update_response.get_json()
    assert updated["git_auth_type"] == "token"
    assert updated["git_secret_ref"] == "PRIVATE_APP"


def test_create_project_rejects_token_auth_without_secret_ref(client):
    response = create_project(
        client,
        name="invalid-private-project",
        repo_url="https://github.com/example/private-repo",
        git_auth_type="token",
        git_secret_ref=None,
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "git_secret_ref is required when git_auth_type is 'token'"}


def test_create_project_accepts_kubernetes_env_var_references(client):
    response = create_project(
        client,
        name="k8s-env-project",
        env_vars=[
            {"name": "APP_ENV", "value_source": "configmap_key_ref", "source_name": "my-app-config", "source_key": "app-env"},
            {"name": "DATABASE_URL", "value_source": "secret_key_ref", "source_name": "my-app-secret", "source_key": "database-url"},
        ],
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["env_vars"][0]["value_source"] == "configmap_key_ref"
    assert payload["env_vars"][1]["value_source"] == "secret_key_ref"


def test_create_project_rejects_invalid_kubernetes_env_var_reference_shape(client):
    response = create_project(
        client,
        name="invalid-k8s-env-project",
        env_vars=[
            {"name": "DATABASE_URL", "value_source": "secret_key_ref", "source_name": "my-app-secret"},
        ],
    )

    assert response.status_code == 400
    assert "source_key" in response.get_json()["error"]


def test_create_project_rejects_unsupported_env_ref_field_names(client):
    response = create_project(
        client,
        name="invalid-ref-fields-project",
        env_vars=[
            {"name": "DATABASE_URL", "secret_ref": "my-app-secret", "source_name": "my-app-secret", "source_key": "database-url"},
        ],
    )

    assert response.status_code == 400
    assert "configmap_ref" in response.get_json()["error"]


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


def test_deploy_project_creates_pending_records_from_minimal_input(client, app, monkeypatch):
    project_response = create_project(client, name="deploy-now-app")
    project_id = project_response.get_json()["id"]

    monkeypatch.setattr(projects_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef")

    response = client.post(
        f"/api/projects/{project_id}/deploy",
        json={"test_command": "pytest -q"},
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["project_id"] == project_id
    assert payload["status"] == "pending"
    assert payload["branch"] == "main"
    assert payload["commit_sha"] == "0123456789abcdef"
    assert payload["image_tag"] == "0123456789ab"
    assert payload["image_ref"] == "deploy-now-app:0123456789ab"

    deployment_response = client.get(f"/api/projects/{project_id}/deployments/{payload['deployment_id']}")
    assert deployment_response.status_code == 200
    deployment = deployment_response.get_json()
    assert deployment["status"] == "pending"
    assert deployment["build"]["status"] == "pending"
    assert deployment["build"]["test_command"] == "pytest -q"
    assert deployment["build"]["image_name"] == "deploy-now-app"
    assert deployment["build"]["image_tag"] == "0123456789ab"
    assert deployment["build"]["image_ref"] == "deploy-now-app:0123456789ab"
    assert deployment["events"][0]["event_type"] == "deployment.created"
    assert deployment["events"][0]["message"] == "Deployment requested for branch 'main' at commit '0123456789ab'"


def test_deploy_project_uses_branch_override_and_registry_config(client, app, monkeypatch):
    with app.app_context():
        app.config["CONTROL_PLANE_REGISTRY_ENABLED"] = True
        app.config["CONTROL_PLANE_REGISTRY_URL"] = "docker.io"
        app.config["CONTROL_PLANE_REGISTRY_NAMESPACE"] = "example"

    project_response = create_project(client, name="registry-deploy-app")
    project_id = project_response.get_json()["id"]

    monkeypatch.setattr(projects_api, "resolve_project_commit_sha", lambda project, branch: "fedcba9876543210")

    response = client.post(
        f"/api/projects/{project_id}/deploy",
        json={"branch": "release"},
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["branch"] == "release"
    assert payload["image_ref"] == "docker.io/example/registry-deploy-app:fedcba987654"


def test_deploy_project_returns_conflict_when_commit_resolution_fails(client, monkeypatch):
    project_response = create_project(client, name="broken-deploy-app")
    project_id = project_response.get_json()["id"]

    monkeypatch.setattr(
        projects_api,
        "resolve_project_commit_sha",
        lambda project, branch: (_ for _ in ()).throw(ValueError("Unable to resolve commit for branch 'main': boom")),
    )

    response = client.post(f"/api/projects/{project_id}/deploy", json={})

    assert response.status_code == 409
    assert response.get_json() == {"error": "Unable to resolve commit for branch 'main': boom"}


def test_get_latest_deployment_returns_404_when_project_has_no_deployments(client):
    project_response = create_project(client, name="empty-deployments-app")
    project_id = project_response.get_json()["id"]

    response = client.get(f"/api/projects/{project_id}/deployments/latest")

    assert response.status_code == 404
    assert response.get_json() == {"error": "Project has no deployments"}


def test_get_latest_deployment_returns_most_recent_deployment(client, monkeypatch):
    project_response = create_project(client, name="latest-deploy-app")
    project_id = project_response.get_json()["id"]

    monkeypatch.setattr(projects_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef")
    first_response = client.post(f"/api/projects/{project_id}/deploy", json={})
    first_deployment_id = first_response.get_json()["deployment_id"]

    monkeypatch.setattr(projects_api, "resolve_project_commit_sha", lambda project, branch: "fedcba9876543210")
    second_response = client.post(
        f"/api/projects/{project_id}/deploy",
        json={"branch": "release", "test_command": "pytest -q"},
    )

    assert first_deployment_id != second_response.get_json()["deployment_id"]

    latest_response = client.get(f"/api/projects/{project_id}/deployments/latest")

    assert latest_response.status_code == 200
    payload = latest_response.get_json()
    assert payload["deployment_id"] == second_response.get_json()["deployment_id"]
    assert payload["status"] == "pending"
    assert payload["build_status"] == "pending"
    assert payload["branch"] == "release"
    assert payload["commit_sha"] == "fedcba9876543210"
    assert payload["image_tag"] == "fedcba987654"
    assert payload["image_ref"] == "latest-deploy-app:fedcba987654"
    assert payload["service_url"] is None
    assert payload["created_at"] is not None
    assert payload["updated_at"] is not None


def test_retry_deployment_creates_new_pending_deployment_from_original_settings(client, monkeypatch):
    project_response = create_project(client, name="retry-app")
    project_id = project_response.get_json()["id"]

    monkeypatch.setattr(projects_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef")
    original_response = client.post(
        f"/api/projects/{project_id}/deploy",
        json={"branch": "release", "test_command": "pytest -q"},
    )
    original_deployment_id = original_response.get_json()["deployment_id"]

    monkeypatch.setattr(projects_api, "resolve_project_commit_sha", lambda project, branch: "fedcba9876543210")
    retry_response = client.post(f"/api/projects/{project_id}/deployments/{original_deployment_id}/retry")

    assert retry_response.status_code == 201
    payload = retry_response.get_json()
    assert payload["deployment_id"] != original_deployment_id
    assert payload["retried_from_deployment_id"] == original_deployment_id
    assert payload["project_id"] == project_id
    assert payload["status"] == "pending"
    assert payload["branch"] == "release"
    assert payload["commit_sha"] == "fedcba9876543210"
    assert payload["image_tag"] == "fedcba987654"
    assert payload["image_ref"] == "retry-app:fedcba987654"

    deployment_response = client.get(f"/api/projects/{project_id}/deployments/{payload['deployment_id']}")
    deployment = deployment_response.get_json()
    assert deployment["build"]["test_command"] == "pytest -q"
    assert deployment["events"][0]["message"] == "Deployment retry requested for branch 'release' at commit 'fedcba987654'"


def test_retry_deployment_returns_conflict_when_commit_resolution_fails(client, monkeypatch):
    project_response = create_project(client, name="broken-retry-app")
    project_id = project_response.get_json()["id"]

    monkeypatch.setattr(projects_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef")
    original_response = client.post(f"/api/projects/{project_id}/deploy", json={})
    original_deployment_id = original_response.get_json()["deployment_id"]

    monkeypatch.setattr(
        projects_api,
        "resolve_project_commit_sha",
        lambda project, branch: (_ for _ in ()).throw(ValueError("Unable to resolve commit for branch 'main': boom")),
    )

    response = client.post(f"/api/projects/{project_id}/deployments/{original_deployment_id}/retry")

    assert response.status_code == 409
    assert response.get_json() == {"error": "Unable to resolve commit for branch 'main': boom"}


def test_retry_deployment_does_not_expose_other_project_deployment(client, monkeypatch):
    first_project_response = create_project(client, name="retry-owner-app")
    first_project_id = first_project_response.get_json()["id"]
    second_project_response = create_project(client, name="retry-other-app")
    second_project_id = second_project_response.get_json()["id"]

    monkeypatch.setattr(projects_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef")
    original_response = client.post(f"/api/projects/{first_project_id}/deploy", json={})
    original_deployment_id = original_response.get_json()["deployment_id"]

    response = client.post(f"/api/projects/{second_project_id}/deployments/{original_deployment_id}/retry")

    assert response.status_code == 404


def test_redeploy_project_returns_404_when_project_has_no_deployments(client):
    project_response = create_project(client, name="empty-redeploy-app")
    project_id = project_response.get_json()["id"]

    response = client.post(f"/api/projects/{project_id}/redeploy")

    assert response.status_code == 404
    assert response.get_json() == {"error": "Project has no deployments"}


def test_redeploy_project_creates_new_pending_deployment_from_latest_settings(client, monkeypatch):
    project_response = create_project(client, name="redeploy-app")
    project_id = project_response.get_json()["id"]

    monkeypatch.setattr(projects_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef")
    client.post(f"/api/projects/{project_id}/deploy", json={"branch": "main"})

    monkeypatch.setattr(projects_api, "resolve_project_commit_sha", lambda project, branch: "fedcba9876543210")
    latest_response = client.post(
        f"/api/projects/{project_id}/deploy",
        json={"branch": "release", "test_command": "pytest -q"},
    )
    latest_deployment_id = latest_response.get_json()["deployment_id"]

    monkeypatch.setattr(projects_api, "resolve_project_commit_sha", lambda project, branch: "0011223344556677")
    redeploy_response = client.post(f"/api/projects/{project_id}/redeploy")

    assert redeploy_response.status_code == 201
    payload = redeploy_response.get_json()
    assert payload["deployment_id"] != latest_deployment_id
    assert payload["redeployed_from_deployment_id"] == latest_deployment_id
    assert payload["project_id"] == project_id
    assert payload["status"] == "pending"
    assert payload["branch"] == "release"
    assert payload["commit_sha"] == "0011223344556677"
    assert payload["image_tag"] == "001122334455"
    assert payload["image_ref"] == "redeploy-app:001122334455"

    deployment_response = client.get(f"/api/projects/{project_id}/deployments/{payload['deployment_id']}")
    deployment = deployment_response.get_json()
    assert deployment["build"]["test_command"] == "pytest -q"
    assert deployment["events"][0]["message"] == "Project redeploy requested for branch 'release' at commit '001122334455'"


def test_redeploy_project_returns_conflict_when_commit_resolution_fails(client, monkeypatch):
    project_response = create_project(client, name="broken-redeploy-app")
    project_id = project_response.get_json()["id"]

    monkeypatch.setattr(projects_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef")
    client.post(f"/api/projects/{project_id}/deploy", json={})

    monkeypatch.setattr(
        projects_api,
        "resolve_project_commit_sha",
        lambda project, branch: (_ for _ in ()).throw(ValueError("Unable to resolve commit for branch 'main': boom")),
    )

    response = client.post(f"/api/projects/{project_id}/redeploy")

    assert response.status_code == 409
    assert response.get_json() == {"error": "Unable to resolve commit for branch 'main': boom"}


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
    build_log_path.parent.mkdir(parents=True, exist_ok=True)
    build_log_path.write_text("clone\nbuild\ntest\npush\n", encoding="utf-8")

    with app.app_context():
        app.config["CONTROL_PLANE_WORKSPACE_ROOT"] = str(tmp_path)
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.build.log_path = str(build_log_path)
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


def test_get_deployment_summary_returns_successful_view(client, app, monkeypatch, tmp_path):
    project_response = create_project(client, name="summary-app")
    project_id = project_response.get_json()["id"]
    monkeypatch.setattr(projects_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef")
    deploy_response = client.post(f"/api/projects/{project_id}/deploy", json={"branch": "release"})
    deployment_id = deploy_response.get_json()["deployment_id"]

    runtime_log_path = tmp_path / "project-1" / "deployment-1" / "logs" / "runtime.log"
    runtime_log_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_log_path.write_text("ready\n", encoding="utf-8")

    with app.app_context():
        app.config["CONTROL_PLANE_WORKSPACE_ROOT"] = str(tmp_path)
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.status = "running"
        deployment.deploy_target = "local-docker"
        deployment.service_url = "http://127.0.0.1:18080"
        deployment.started_at = deployment.created_at
        deployment.build.status = "succeeded"
        deployment.build.registry_push_status = "succeeded"
        deployment.build.log_path = str(tmp_path / "project-1" / "deployment-1" / "logs" / "build.log")
        Path(deployment.build.log_path).write_text("build ok\n", encoding="utf-8")
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="deployment.apply_succeeded",
                step="deploy",
                level="info",
                status="deploying",
                message="Container started and passed healthcheck.",
                metadata_json={"runtime_log_path": str(runtime_log_path)},
            )
        )
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="image.push_succeeded",
                step="image.push",
                level="info",
                status="succeeded",
                message="Image pushed successfully",
                metadata_json={
                    "step": "image.push",
                    "success": True,
                    "push_log_available": True,
                    "push_summary": "Image pushed successfully",
                },
            )
        )
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="deployment.running",
                step="deployment",
                level="info",
                status="running",
                message="Deployment is now running",
                metadata_json={"service_url": "http://127.0.0.1:18080"},
            )
        )
        db.session.commit()

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/summary")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["deployment_id"] == deployment_id
    assert payload["deployment_status"] == "running"
    assert payload["build_status"] == "succeeded"
    assert payload["current_step"] == "deployment"
    assert payload["last_meaningful_event"]["event_type"] == "deployment.running"
    assert payload["last_error"] is None
    assert payload["branch"] == "release"
    assert payload["commit_sha"] == "0123456789abcdef"
    assert payload["image_tag"] == "0123456789ab"
    assert payload["image_ref"] == "summary-app:0123456789ab"
    assert payload["registry_push_status"] == "succeeded"
    assert payload["push_log_available"] is True
    assert payload["last_push_error_summary"] == "Image pushed successfully"
    assert payload["service_url"] == "http://127.0.0.1:18080"
    assert payload["deploy_target"] == "local-docker"
    assert payload["build_log_available"] is True
    assert payload["runtime_log_available"] is True
    assert payload["started_at"] is not None
    assert payload["created_at"] is not None
    assert payload["updated_at"] is not None
    assert len(payload["events"]) >= 3


def test_get_deployment_summary_for_failed_deployment_includes_error_and_step(client, app):
    project_response = create_project(client, name="failed-summary-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": "abc123def456",
            "status": "failed",
            "build_status": "failed",
            "message": "Deployment failed",
        },
    )
    deployment_id = deployment_response.get_json()["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.last_error = "Healthcheck did not succeed within 30 seconds"
        deployment.build.last_error = "Healthcheck did not succeed within 30 seconds"
        deployment.build.registry_push_status = "failed"
        deployment.build.log_path = "/tmp/paas-workspaces/project-1/deployment-1/logs/deploy.log"
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="image.push.failed",
                step="image.push",
                level="error",
                status="failed",
                message="Registry push failed",
                metadata_json={
                    "log_path": deployment.build.log_path,
                    "step": "image.push",
                    "success": False,
                    "push_log_available": True,
                    "push_summary": "Registry push failed",
                    "error_message": "Registry push failed",
                },
            )
        )
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="deployment.failed",
                step="deployment",
                level="error",
                status="failed",
                message="Healthcheck did not succeed within 30 seconds",
                metadata_json={"log_path": deployment.build.log_path},
            )
        )
        db.session.commit()

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/summary")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["deployment_status"] == "failed"
    assert payload["build_status"] == "failed"
    assert payload["current_step"] == "deployment"
    assert payload["last_meaningful_event"]["event_type"] == "deployment.failed"
    assert payload["last_error"] == "Healthcheck did not succeed within 30 seconds"
    assert payload["registry_push_status"] == "failed"
    assert payload["push_log_available"] is True
    assert payload["last_push_error_summary"] == "Registry push failed"


def test_get_deployment_summary_returns_404_for_missing_deployment(client):
    project_response = create_project(client, name="missing-summary-app")
    project_id = project_response.get_json()["id"]

    response = client.get(f"/api/projects/{project_id}/deployments/999/summary")

    assert response.status_code == 404


def test_get_deployment_summary_does_not_expose_other_project_deployment(client):
    first_project_response = create_project(client, name="summary-owner-app")
    first_project_id = first_project_response.get_json()["id"]
    second_project_response = create_project(client, name="summary-other-app")
    second_project_id = second_project_response.get_json()["id"]

    deployment_response = client.post(
        f"/api/projects/{first_project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "pending"},
    )
    deployment_id = deployment_response.get_json()["id"]

    response = client.get(f"/api/projects/{second_project_id}/deployments/{deployment_id}/summary")

    assert response.status_code == 404


def test_get_deployment_summary_includes_kubernetes_runtime_metadata(client, app):
    project_response = create_project(client, name="k8s-summary-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "running", "build_status": "succeeded"},
    )
    deployment_id = deployment_response.get_json()["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        deployment.service_url = "http://paas-k8s-summary-app-1-svc.default.svc.cluster.local:5000"
        deployment.build.registry_push_status = "succeeded"
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="kubernetes.healthcheck_succeeded",
                step="deploy.kubernetes.healthcheck",
                level="info",
                status="deploying",
                message="Kubernetes Service passed healthcheck",
                metadata_json={
                    "namespace": "default",
                    "deployment_name": "paas-k8s-summary-app-1",
                    "service_name": "paas-k8s-summary-app-1-svc",
                    "service_url": deployment.service_url,
                    "healthcheck_url": f"{deployment.service_url}/health",
                },
            )
        )
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="deployment.running",
                step="deployment",
                level="info",
                status="running",
                message="Deployment is now running",
                metadata_json={"service_url": deployment.service_url, "deploy_target": "kubernetes"},
            )
        )
        db.session.commit()

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/summary")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["deploy_target"] == "kubernetes"
    assert payload["kubernetes_namespace"] == "default"
    assert payload["kubernetes_deployment_name"] == "paas-k8s-summary-app-1"
    assert payload["kubernetes_service_name"] == "paas-k8s-summary-app-1-svc"
    assert payload["last_kubernetes_failure_stage"] is None
    assert payload["last_kubernetes_failure_summary"] is None


def test_get_deployment_summary_includes_last_kubernetes_failure_context(client, app):
    project_response = create_project(client, name="k8s-summary-failure-app")
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
                message="Healthcheck did not succeed within 30 seconds",
                metadata_json={
                    "namespace": "default",
                    "deployment_name": "paas-k8s-summary-failure-app-1",
                    "service_name": "paas-k8s-summary-failure-app-1-svc",
                    "healthcheck_service_summary": "Endpoints: <none> | Session Affinity: None",
                    "healthcheck_deployment_summary": "Conditions: | Available  True",
                },
            )
        )
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="deployment.failed",
                step="deployment",
                level="error",
                status="failed",
                message=deployment.last_error,
                metadata_json={"deploy_target": "kubernetes"},
            )
        )
        db.session.commit()

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/summary")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["deploy_target"] == "kubernetes"
    assert payload["kubernetes_namespace"] == "default"
    assert payload["kubernetes_deployment_name"] == "paas-k8s-summary-failure-app-1"
    assert payload["kubernetes_service_name"] == "paas-k8s-summary-failure-app-1-svc"
    assert payload["last_kubernetes_failure_stage"] == "healthcheck"
    assert payload["last_kubernetes_failure_summary"] == "Endpoints: <none> | Session Affinity: None"


def test_get_deployment_summary_includes_kubernetes_preflight_failure_context(client, app):
    project_response = create_project(client, name="k8s-summary-preflight-app")
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
                message="Missing Kubernetes referenced resources: ConfigMap/my-app-config, Secret/dockerhub-pull-secret",
                metadata_json={
                    "namespace": "default",
                    "missing_resources": [
                        {"kind": "ConfigMap", "name": "my-app-config"},
                        {"kind": "Secret", "name": "dockerhub-pull-secret", "usage": "image_pull_secret"},
                    ],
                },
            )
        )
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="deployment.failed",
                step="deployment",
                level="error",
                status="failed",
                message=deployment.last_error,
                metadata_json={"deploy_target": "kubernetes"},
            )
        )
        db.session.commit()

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/summary")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["deploy_target"] == "kubernetes"
    assert payload["kubernetes_namespace"] == "default"
    assert payload["last_kubernetes_failure_stage"] == "preflight"
    assert payload["last_kubernetes_failure_summary"] == "ConfigMap/my-app-config, Secret/dockerhub-pull-secret"
    assert payload["last_kubernetes_failure_missing_resources"] == [
        {"kind": "ConfigMap", "name": "my-app-config"},
        {"kind": "Secret", "name": "dockerhub-pull-secret", "usage": "image_pull_secret"},
    ]


def test_get_kubernetes_diagnostics_returns_structured_failure_view(client, app):
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
                },
            )
        )
        db.session.commit()

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
    assert payload["diagnostics"]["healthcheck_service_summary"] == "Endpoints: <none> | Session Affinity: None"


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
