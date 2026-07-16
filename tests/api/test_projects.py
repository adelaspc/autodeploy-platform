from pathlib import Path
import base64
import hashlib
import hmac
import json
import subprocess

from control_plane.application.deployments import orchestration as deployment_orchestration_api
from control_plane.extensions import db
from control_plane.models import DeploymentCommand, DeploymentEvent, PlatformDeployment
from worker.execution.contracts import ExecutionResult, WorkerExecutionError
import worker.processing.command_processor as worker_commands


def create_project(client, **overrides):
    payload = {
        "name": "autodeploy-control-plane",
        "repo_url": "https://github.com/example/autodeploy-control-plane",
        "branch": "main",
        "dockerfile_path": "Dockerfile",
        "build_context": ".",
        "port": 5000,
        "healthcheck_path": "/health",
        "env_vars": [{"name": "DATABASE_URL", "required": True}],
        "default_test_command": "pytest -q",
        "migration_command": "flask db upgrade",
        "cpu": "250m",
        "memory": "512Mi",
        "trigger": "manual",
        "runtime": "dockerfile",
    }
    payload.update(overrides)
    return client.post("/api/projects", json=payload)


def process_queued_command(app, client, project_id, deployment_id):
    with app.app_context():
        worker_commands.process_next_pending_command()
    return client.get(f"/api/projects/{project_id}/deployments/{deployment_id}").get_json()


def github_headers(app, payload, *, event="push", delivery_id="delivery-123"):
    body = json.dumps(payload).encode("utf-8")
    secret = app.config["CONTROL_PLANE_GITHUB_WEBHOOK_SECRET"].encode("utf-8")
    digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return body, {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": f"sha256={digest}",
    }


def test_create_and_list_projects(client):
    create_response = create_project(client)

    assert create_response.status_code == 201
    created = create_response.get_json()
    assert created["name"] == "autodeploy-control-plane"
    assert created["repo_url"] == "https://github.com/example/autodeploy-control-plane.git"

    list_response = client.get("/api/projects")
    assert list_response.status_code == 200
    payload = list_response.get_json()
    assert len(payload) == 1
    assert payload[0]["name"] == "autodeploy-control-plane"


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
    assert payload["repo_url"] == "https://github.com/example/private-repo.git"
    assert payload["default_test_command"] == "pytest -q"
    assert "ghp_" not in str(payload)


def test_create_project_rejects_invalid_trigger(client):
    response = create_project(client, trigger="cron")

    assert response.status_code == 400
    assert "Invalid trigger" in response.get_json()["error"]


def test_create_project_rejects_non_github_repo_url_that_does_not_exist(client):
    response = create_project(client, name="invalid-repo-url-app", repo_url="git@github.com:example/private-repo.git")

    assert response.status_code == 400
    assert "Invalid repo_url" in response.get_json()["error"]


def test_create_project_accepts_existing_local_repo_path(client, tmp_path):
    local_repo = tmp_path / "local-repo"
    local_repo.mkdir()

    response = create_project(client, name="local-repo-app", repo_url=str(local_repo))

    assert response.status_code == 201
    assert response.get_json()["repo_url"] == str(local_repo)


def test_create_project_normalizes_github_repo_url_without_git_suffix(client):
    response = create_project(client, name="normalized-github-url-app", repo_url="https://github.com/Example/My-App")

    assert response.status_code == 201
    assert response.get_json()["repo_url"] == "https://github.com/example/my-app.git"


def test_create_project_accepts_github_repo_url_with_git_suffix(client):
    response = create_project(
        client,
        name="git-suffix-github-url-app",
        repo_url="https://github.com/Example/My-App.git",
    )

    assert response.status_code == 201
    assert response.get_json()["repo_url"] == "https://github.com/example/my-app.git"


def test_create_project_rejects_existing_local_repo_path_outside_development(client, app, tmp_path):
    local_repo = tmp_path / "prod-local-repo"
    local_repo.mkdir()
    app.config["CONTROL_PLANE_ENV"] = "production"

    response = create_project(client, name="prod-local-repo-app", repo_url=str(local_repo))

    assert response.status_code == 400
    assert "allowed only when CONTROL_PLANE_ENV=development" in response.get_json()["error"]


def test_create_project_accepts_github_repo_url_outside_development(client, app):
    app.config["CONTROL_PLANE_ENV"] = "production"

    response = create_project(
        client,
        name="prod-github-repo-app",
        repo_url="https://github.com/example/autodeploy-control-plane",
    )

    assert response.status_code == 201
    assert response.get_json()["repo_url"] == "https://github.com/example/autodeploy-control-plane.git"


def test_create_project_rejects_malformed_github_repo_root_url(client):
    response = create_project(client, name="invalid-github-root-app", repo_url="https://github.com/")

    assert response.status_code == 400
    assert "canonical GitHub HTTPS repository URL" in response.get_json()["error"]


def test_create_project_rejects_github_repo_url_with_extra_path_segments(client):
    response = create_project(
        client,
        name="invalid-github-extra-path-app",
        repo_url="https://github.com/example/repo/extra/path",
    )

    assert response.status_code == 400
    assert "canonical GitHub HTTPS repository URL" in response.get_json()["error"]


def test_create_project_rejects_non_github_https_repo_url(client):
    response = create_project(
        client,
        name="non-github-remote-app",
        repo_url="https://gitlab.com/example/repo.git",
    )

    assert response.status_code == 400
    assert "canonical GitHub HTTPS repository URL" in response.get_json()["error"]


def test_create_project_rejects_ssh_github_repo_url(client):
    response = create_project(
        client,
        name="ssh-github-url-app",
        repo_url="git@github.com:example/private-repo.git",
    )

    assert response.status_code == 400
    assert "canonical GitHub HTTPS repository URL" in response.get_json()["error"]


def test_create_project_rejects_absolute_dockerfile_path(client):
    response = create_project(client, name="absolute-dockerfile-app", dockerfile_path="/Dockerfile")

    assert response.status_code == 400
    assert "repository-relative path" in response.get_json()["error"]


def test_create_project_rejects_build_context_parent_traversal(client):
    response = create_project(client, name="parent-build-context-app", build_context="../app")

    assert response.status_code == 400
    assert "Parent directory traversal" in response.get_json()["error"]


def test_create_project_rejects_healthcheck_path_without_leading_slash(client):
    response = create_project(client, name="invalid-healthcheck-app", healthcheck_path="health")

    assert response.status_code == 400
    assert "healthcheck_path" in response.get_json()["error"]


def test_create_project_rejects_shell_control_in_default_test_command(client):
    response = create_project(
        client,
        name="invalid-default-test-command-app",
        default_test_command="pytest -q && curl https://example.test",
    )

    assert response.status_code == 400
    assert "default_test_command" in response.get_json()["error"]
    assert "Shell control" in response.get_json()["error"]


def test_create_project_rejects_shell_wrapper_in_migration_command(client):
    response = create_project(
        client,
        name="invalid-migration-command-app",
        migration_command="bash -c 'flask db upgrade'",
    )

    assert response.status_code == 400
    assert "migration_command" in response.get_json()["error"]
    assert "Shell wrappers" in response.get_json()["error"]


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
            "default_test_command": "ruff check .",
        },
    )

    assert update_response.status_code == 200
    updated = update_response.get_json()
    assert updated["branch"] == "develop"
    assert updated["port"] == 8080
    assert updated["trigger"] == "github_push"
    assert updated["default_test_command"] == "ruff check ."


def test_update_project_rejects_overlong_command(client):
    project_response = create_project(client, name="overlong-command-app")
    project_id = project_response.get_json()["id"]

    update_response = client.patch(
        f"/api/projects/{project_id}",
        json={"default_test_command": "p" * 256},
    )

    assert update_response.status_code == 400
    assert "at most 255 characters" in update_response.get_json()["error"]


def test_get_project_activity_returns_empty_state(client):
    project_response = create_project(client, name="activity-empty-project")
    project_id = project_response.get_json()["id"]

    response = client.get(f"/api/projects/{project_id}/activity")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok",
        "project": {
            "id": project_id,
            "name": "activity-empty-project",
            "branch": "main",
            "trigger": "manual",
            "repo_url": "https://github.com/example/autodeploy-control-plane.git",
        },
        "latest_deployment_at": None,
        "active_deployment_count": 0,
        "failed_deployment_count": 0,
        "recent_webhook_delivery_count": 0,
        "next_before_deployment_id": None,
        "next_before_webhook_delivery_id": None,
        "pagination": {
            "latest_limit": 10,
            "webhook_limit": 10,
            "next_before_deployment_id": None,
            "next_before_webhook_delivery_id": None,
        },
        "latest_deployments": [],
        "active_deployment": None,
        "latest_failed_deployment": None,
        "recent_webhook_deliveries": [],
    }


def test_get_project_activity_reports_deployments_and_webhooks(client, app):
    project_response = create_project(
        client,
        name="activity-project",
        repo_url="https://github.com/example/activity-project",
        trigger="github_push",
    )
    project_id = project_response.get_json()["id"]
    other_project_id = create_project(client, name="other-activity-project").get_json()["id"]

    first_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "1111111111111111"},
    )
    second_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "2222222222222222"},
    )
    client.post(
        f"/api/projects/{other_project_id}/deployments",
        json={"commit_sha": "3333333333333333"},
    )

    with app.app_context():
        running_deployment = db.session.get(PlatformDeployment, second_response.get_json()["id"])
        running_deployment.status = "running"
        failed_deployment = db.session.get(PlatformDeployment, first_response.get_json()["id"])
        failed_deployment.status = "failed"
        failed_deployment.last_error = "Project-specific failure"
        failed_deployment.build.status = "failed"
        db.session.commit()

    accepted_payload = {
        "ref": "refs/heads/main",
        "after": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "repository": {
            "clone_url": "https://github.com/example/activity-project.git",
        },
    }
    accepted_body, accepted_headers = github_headers(app, accepted_payload, delivery_id="project-accepted")
    accepted_response = client.post("/api/webhooks/github", data=accepted_body, headers=accepted_headers)

    ignored_payload = {
        "ref": "refs/heads/release",
        "after": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "repository": {
            "clone_url": "https://github.com/example/activity-project.git",
        },
    }
    ignored_body, ignored_headers = github_headers(app, ignored_payload, delivery_id="project-ignored")
    ignored_response = client.post("/api/webhooks/github", data=ignored_body, headers=ignored_headers)

    unrelated_payload = {
        "ref": "refs/heads/main",
        "after": "cccccccccccccccccccccccccccccccccccccccc",
        "repository": {
            "clone_url": "https://github.com/example/unrelated-project.git",
        },
    }
    unrelated_body, unrelated_headers = github_headers(app, unrelated_payload, delivery_id="project-unrelated")
    client.post("/api/webhooks/github", data=unrelated_body, headers=unrelated_headers)

    response = client.get(f"/api/projects/{project_id}/activity")
    payload = response.get_json()

    assert accepted_response.status_code == 202
    assert ignored_response.status_code == 202
    assert response.status_code == 200
    assert [item["deployment_id"] for item in payload["latest_deployments"][:2]] == [
        accepted_response.get_json()["deployments"][0]["deployment_id"],
        second_response.get_json()["id"],
    ]
    assert payload["active_deployment"]["deployment_id"] == accepted_response.get_json()["deployments"][0]["deployment_id"]
    assert payload["latest_failed_deployment"]["deployment_id"] == first_response.get_json()["id"]
    assert payload["latest_failed_deployment"]["last_error"] == "Project-specific failure"
    assert payload["latest_deployment_at"] is not None
    assert payload["active_deployment_count"] == 1
    assert payload["failed_deployment_count"] == 1
    assert payload["recent_webhook_delivery_count"] == 2
    assert payload["next_before_deployment_id"] is not None
    assert payload["next_before_webhook_delivery_id"] is not None
    assert payload["pagination"]["latest_limit"] == 10
    assert [item["delivery_id"] for item in payload["recent_webhook_deliveries"]] == [
        "project-ignored",
        "project-accepted",
    ]
    assert [item["reason"] for item in payload["recent_webhook_deliveries"]] == [
        "branch_mismatch",
        None,
    ]


def test_get_project_activity_supports_limits_and_rejects_invalid_values(client, app):
    project_response = create_project(
        client,
        name="activity-limited-project",
        repo_url="https://github.com/example/activity-limited-project",
        trigger="github_push",
    )
    project_id = project_response.get_json()["id"]

    first_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "4444444444444444"},
    )
    second_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "5555555555555555"},
    )

    with app.app_context():
        first_deployment = db.session.get(PlatformDeployment, first_response.get_json()["id"])
        first_deployment.status = "failed"
        first_deployment.last_error = "older failure"
        second_deployment = db.session.get(PlatformDeployment, second_response.get_json()["id"])
        second_deployment.status = "running"
        db.session.commit()

    accepted_payload = {
        "ref": "refs/heads/main",
        "after": "dddddddddddddddddddddddddddddddddddddddd",
        "repository": {
            "clone_url": "https://github.com/example/activity-limited-project.git",
        },
    }
    accepted_body, accepted_headers = github_headers(app, accepted_payload, delivery_id="limited-accepted")
    client.post("/api/webhooks/github", data=accepted_body, headers=accepted_headers)

    ignored_payload = {
        "ref": "refs/heads/release",
        "after": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "repository": {
            "clone_url": "https://github.com/example/activity-limited-project.git",
        },
    }
    ignored_body, ignored_headers = github_headers(app, ignored_payload, delivery_id="limited-ignored")
    client.post("/api/webhooks/github", data=ignored_body, headers=ignored_headers)

    limited_response = client.get(f"/api/projects/{project_id}/activity?latest_limit=1&webhook_limit=1")
    limited_payload = limited_response.get_json()

    assert limited_response.status_code == 200
    assert len(limited_payload["latest_deployments"]) == 1
    assert len(limited_payload["recent_webhook_deliveries"]) == 1
    assert limited_payload["pagination"]["latest_limit"] == 1
    assert limited_payload["recent_webhook_deliveries"][0]["delivery_id"] == "limited-ignored"

    invalid_response = client.get(f"/api/projects/{project_id}/activity?latest_limit=0")
    assert invalid_response.status_code == 400
    assert invalid_response.get_json() == {"error": "latest_limit must be an integer between 1 and 100"}


def test_get_project_activity_supports_status_filters(client, app):
    project_response = create_project(
        client,
        name="activity-status-project",
        repo_url="https://github.com/example/activity-status-project",
        trigger="github_push",
    )
    project_id = project_response.get_json()["id"]

    failed_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "6666666666666666"},
    )
    running_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "7777777777777777"},
    )

    with app.app_context():
        failed_deployment = db.session.get(PlatformDeployment, failed_response.get_json()["id"])
        failed_deployment.status = "failed"
        failed_deployment.last_error = "project filter failure"
        running_deployment = db.session.get(PlatformDeployment, running_response.get_json()["id"])
        running_deployment.status = "running"
        db.session.commit()

    accepted_payload = {
        "ref": "refs/heads/main",
        "after": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "repository": {
            "clone_url": "https://github.com/example/activity-status-project.git",
        },
    }
    accepted_body, accepted_headers = github_headers(app, accepted_payload, delivery_id="project-status-accepted")
    client.post("/api/webhooks/github", data=accepted_body, headers=accepted_headers)

    ignored_payload = {
        "ref": "refs/heads/release",
        "after": "ffffffffffffffffffffffffffffffffffffffff",
        "repository": {
            "clone_url": "https://github.com/example/activity-status-project.git",
        },
    }
    ignored_body, ignored_headers = github_headers(app, ignored_payload, delivery_id="project-status-ignored")
    client.post("/api/webhooks/github", data=ignored_body, headers=ignored_headers)

    response = client.get(
        f"/api/projects/{project_id}/activity?deployment_status=running,failed&webhook_status=ignored"
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert [item["status"] for item in payload["latest_deployments"]] == ["running", "failed"]
    assert payload["active_deployment"]["status"] == "running"
    assert payload["latest_failed_deployment"]["status"] == "failed"
    assert [item["delivery_id"] for item in payload["recent_webhook_deliveries"]] == ["project-status-ignored"]


def test_get_project_activity_supports_active_only_and_include_flags(client, app):
    project_response = create_project(
        client,
        name="activity-flags-project",
        repo_url="https://github.com/example/activity-flags-project",
        trigger="github_push",
    )
    project_id = project_response.get_json()["id"]

    failed_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "8888888888888888"},
    )
    running_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "9999999999999999"},
    )

    with app.app_context():
        failed_deployment = db.session.get(PlatformDeployment, failed_response.get_json()["id"])
        failed_deployment.status = "failed"
        failed_deployment.last_error = "hidden failure"
        running_deployment = db.session.get(PlatformDeployment, running_response.get_json()["id"])
        running_deployment.status = "running"
        db.session.commit()

    accepted_payload = {
        "ref": "refs/heads/main",
        "after": "1212121212121212121212121212121212121212",
        "repository": {
            "clone_url": "https://github.com/example/activity-flags-project.git",
        },
    }
    accepted_body, accepted_headers = github_headers(app, accepted_payload, delivery_id="project-flags-accepted")
    client.post("/api/webhooks/github", data=accepted_body, headers=accepted_headers)

    response = client.get(
        f"/api/projects/{project_id}/activity?active_only=true&include_latest_failed=false&include_webhooks=false"
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert [item["status"] for item in payload["latest_deployments"]] == ["pending", "running"]
    assert payload["latest_failed_deployment"] is None
    assert payload["recent_webhook_deliveries"] == []


def test_get_project_activity_supports_before_id_pagination(client):
    project_response = create_project(client, name="activity-before-project")
    project_id = project_response.get_json()["id"]

    first = client.post(f"/api/projects/{project_id}/deployments", json={"commit_sha": "1414141414141414"})
    second = client.post(f"/api/projects/{project_id}/deployments", json={"commit_sha": "1515151515151515"})
    third = client.post(f"/api/projects/{project_id}/deployments", json={"commit_sha": "1616161616161616"})

    response = client.get(
        f"/api/projects/{project_id}/activity?latest_limit=2&before_deployment_id={third.get_json()['id']}"
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert [item["deployment_id"] for item in payload["latest_deployments"]] == [
        second.get_json()["id"],
        first.get_json()["id"],
    ]
    assert payload["pagination"]["next_before_deployment_id"] == first.get_json()["id"]


def test_get_project_activity_rejects_invalid_status_filters(client):
    project_response = create_project(client, name="invalid-activity-filter-project")
    project_id = project_response.get_json()["id"]

    response = client.get(f"/api/projects/{project_id}/activity?deployment_status=wat")

    assert response.status_code == 400
    assert response.get_json() == {
        "error": (
            "deployment_status contains invalid values: wat. Allowed: "
            "building, cloning, deploying, failed, pending, pushing_image, running, stopped, testing"
        )
    }


def test_get_project_activity_rejects_invalid_boolean_flags(client):
    project_response = create_project(client, name="invalid-activity-boolean-project")
    project_id = project_response.get_json()["id"]

    response = client.get(f"/api/projects/{project_id}/activity?active_only=maybe")

    assert response.status_code == 400
    assert response.get_json() == {"error": "active_only must be a boolean"}


def test_get_project_status_reports_latest_active_failed_and_readiness(client, app):
    project_response = create_project(client, name="status-summary-project")
    project_id = project_response.get_json()["id"]

    failed_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abababababababab"},
    )
    running_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "cdcdcdcdcdcdcdcd"},
    )

    with app.app_context():
        failed_deployment = db.session.get(PlatformDeployment, failed_response.get_json()["id"])
        failed_deployment.status = "failed"
        failed_deployment.last_error = "status summary failure"
        running_deployment = db.session.get(PlatformDeployment, running_response.get_json()["id"])
        running_deployment.status = "running"
        db.session.commit()
        app.config["CONTROL_PLANE_EXECUTOR"] = "kubernetes"
        app.config["CONTROL_PLANE_REGISTRY_ENABLED"] = True
        app.config["CONTROL_PLANE_REGISTRY_URL"] = "docker.io"
        app.config["CONTROL_PLANE_REGISTRY_NAMESPACE"] = None
        app.config["CONTROL_PLANE_KUBECONFIG"] = "/tmp/kubeconfig"

    response = client.get(f"/api/projects/{project_id}/status")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["project"]["id"] == project_id
    assert payload["deployment_creation_ready"] is False
    assert "CONTROL_PLANE_REGISTRY_NAMESPACE" in payload["deployment_creation_error"]
    assert payload["latest_deployment_at"] is not None
    assert payload["active_deployment_count"] == 1
    assert payload["failed_deployment_count"] == 1
    assert payload["latest_deployment"]["deployment_id"] == running_response.get_json()["id"]
    assert payload["active_deployment"]["deployment_id"] == running_response.get_json()["id"]
    assert payload["latest_failed_deployment"]["deployment_id"] == failed_response.get_json()["id"]


def test_get_project_status_reports_empty_state(client):
    project_response = create_project(client, name="empty-status-project")
    project_id = project_response.get_json()["id"]

    response = client.get(f"/api/projects/{project_id}/status")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok",
        "project": {
            "id": project_id,
            "name": "empty-status-project",
            "branch": "main",
            "trigger": "manual",
            "repo_url": "https://github.com/example/autodeploy-control-plane.git",
        },
        "deployment_creation_ready": True,
        "deployment_creation_error": None,
        "latest_deployment_at": None,
        "active_deployment_count": 0,
        "failed_deployment_count": 0,
        "latest_deployment": None,
        "active_deployment": None,
        "latest_failed_deployment": None,
    }


def test_list_project_deployments_supports_filters_and_pagination(client, app):
    project_response = create_project(client, name="list-deployments-project")
    project_id = project_response.get_json()["id"]

    first = client.post(f"/api/projects/{project_id}/deployments", json={"commit_sha": "2121212121212121"})
    second = client.post(f"/api/projects/{project_id}/deployments", json={"commit_sha": "2222222222222222"})
    third = client.post(f"/api/projects/{project_id}/deployments", json={"commit_sha": "2323232323232323"})

    with app.app_context():
        first_deployment = db.session.get(PlatformDeployment, first.get_json()["id"])
        first_deployment.status = "failed"
        second_deployment = db.session.get(PlatformDeployment, second.get_json()["id"])
        second_deployment.status = "running"
        db.session.commit()

    response = client.get(f"/api/projects/{project_id}/deployments?limit=2&deployment_status=running,failed")
    payload = response.get_json()

    assert response.status_code == 200
    assert [item["id"] for item in payload["items"]] == [second.get_json()["id"], first.get_json()["id"]]
    assert payload["pagination"] == {
        "limit": 2,
        "count": 2,
        "next_before_deployment_id": first.get_json()["id"],
    }

    paged_response = client.get(
        f"/api/projects/{project_id}/deployments?limit=2&before_deployment_id={third.get_json()['id']}"
    )
    paged_payload = paged_response.get_json()
    assert paged_response.status_code == 200
    assert [item["id"] for item in paged_payload["items"]] == [second.get_json()["id"], first.get_json()["id"]]


def test_list_project_deployments_rejects_invalid_filters(client):
    project_response = create_project(client, name="invalid-list-deployments-project")
    project_id = project_response.get_json()["id"]

    response = client.get(f"/api/projects/{project_id}/deployments?deployment_status=wat")

    assert response.status_code == 400
    assert response.get_json() == {
        "error": (
            "deployment_status contains invalid values: wat. Allowed: "
            "building, cloning, deploying, failed, pending, pushing_image, running, stopped, testing"
        )
    }


def test_failed_activity_order_uses_updated_at_desc(client, app):
    first_project_id = create_project(client, name="updated-order-first").get_json()["id"]
    first = client.post(f"/api/projects/{first_project_id}/deployments", json={"commit_sha": "3131313131313131"})
    second = client.post(f"/api/projects/{first_project_id}/deployments", json={"commit_sha": "3232323232323232"})

    with app.app_context():
        first_deployment = db.session.get(PlatformDeployment, first.get_json()["id"])
        second_deployment = db.session.get(PlatformDeployment, second.get_json()["id"])
        first_deployment.status = "failed"
        second_deployment.status = "failed"
        db.session.flush()
        original_second_updated_at = second_deployment.updated_at
        first_deployment.updated_at = original_second_updated_at.replace(microsecond=max(original_second_updated_at.microsecond - 1, 0))
        second_deployment.updated_at = original_second_updated_at
        db.session.commit()

    response = client.get("/health/activity?deployment_status=failed")
    payload = response.get_json()

    assert response.status_code == 200
    assert [item["deployment_id"] for item in payload["failed_deployments"][:2]] == [
        second.get_json()["id"],
        first.get_json()["id"],
    ]


def test_update_project_normalizes_github_repo_url(client):
    project_response = create_project(client, name="editable-normalized-repo-project")
    project_id = project_response.get_json()["id"]

    update_response = client.patch(
        f"/api/projects/{project_id}",
        json={"repo_url": "https://github.com/Example/Updated-Repo"},
    )

    assert update_response.status_code == 200
    assert update_response.get_json()["repo_url"] == "https://github.com/example/updated-repo.git"


def test_update_project_allows_clearing_default_test_command(client):
    project_response = create_project(client, name="clear-default-test-command-project")
    project_id = project_response.get_json()["id"]

    update_response = client.patch(
        f"/api/projects/{project_id}",
        json={"default_test_command": None},
    )

    assert update_response.status_code == 200
    assert update_response.get_json()["default_test_command"] is None


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


def test_update_project_rejects_invalid_healthcheck_path(client):
    project_response = create_project(client, name="editable-healthcheck-project")
    project_id = project_response.get_json()["id"]

    update_response = client.patch(
        f"/api/projects/{project_id}",
        json={"healthcheck_path": "ready"},
    )

    assert update_response.status_code == 400
    assert "healthcheck_path" in update_response.get_json()["error"]


def test_update_project_rejects_absolute_build_context(client):
    project_response = create_project(client, name="editable-build-context-project")
    project_id = project_response.get_json()["id"]

    update_response = client.patch(
        f"/api/projects/{project_id}",
        json={"build_context": "/workspace"},
    )

    assert update_response.status_code == 400
    assert "repository-relative path" in update_response.get_json()["error"]


def test_update_project_rejects_local_repo_path_outside_development(client, app, tmp_path):
    project_response = create_project(client, name="editable-repo-url-project")
    project_id = project_response.get_json()["id"]
    local_repo = tmp_path / "editable-local-repo"
    local_repo.mkdir()
    app.config["CONTROL_PLANE_ENV"] = "production"

    update_response = client.patch(
        f"/api/projects/{project_id}",
        json={"repo_url": str(local_repo)},
    )

    assert update_response.status_code == 400
    assert "allowed only when CONTROL_PLANE_ENV=development" in update_response.get_json()["error"]


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
    assert payload["env_vars"][1]["is_secret"] is True


def test_create_project_redacts_literal_secret_env_values_in_api_payloads(client):
    response = create_project(
        client,
        name="literal-secret-project",
        env_vars=[
            {"name": "APP_ENV", "value": "production"},
            {"name": "DATABASE_URL", "value": "postgres://user:super-secret@db/app", "is_secret": True},
        ],
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["env_vars"][0]["value"] == "production"
    assert payload["env_vars"][1]["value"] == "[REDACTED]"
    assert payload["env_vars"][1]["is_secret"] is True

    project_id = payload["id"]
    fetched = client.get(f"/api/projects/{project_id}").get_json()
    assert fetched["env_vars"][1]["value"] == "[REDACTED]"
    listed = client.get("/api/projects").get_json()
    assert listed[0]["env_vars"][1]["value"] == "[REDACTED]"


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


def test_create_project_rejects_invalid_kubernetes_env_var_name_in_kubernetes_mode(client, app):
    with app.app_context():
        app.config["CONTROL_PLANE_EXECUTOR"] = "kubernetes"

    response = create_project(
        client,
        name="invalid-k8s-env-name-project",
        env_vars=[
            {"name": "APP-NAME", "value_source": "configmap_key_ref", "source_name": "my-app-config", "source_key": "app-env"},
        ],
    )

    assert response.status_code == 400
    assert "valid Kubernetes environment variable name" in response.get_json()["error"]


def test_create_project_rejects_invalid_kubernetes_source_type_in_kubernetes_mode(client, app):
    with app.app_context():
        app.config["CONTROL_PLANE_EXECUTOR"] = "kubernetes"

    response = create_project(
        client,
        name="invalid-k8s-source-type-project",
        env_vars=[
            {"name": "APP_ENV", "value_source": "configmap_ref", "source_name": "my-app-config", "source_key": "app-env"},
        ],
    )

    assert response.status_code == 400
    assert "value_source" in response.get_json()["error"]


def test_create_project_rejects_invalid_kubernetes_resource_name_in_kubernetes_mode(client, app):
    with app.app_context():
        app.config["CONTROL_PLANE_EXECUTOR"] = "kubernetes"

    response = create_project(
        client,
        name="invalid-k8s-resource-name-project",
        env_vars=[
            {"name": "APP_ENV", "value_source": "configmap_key_ref", "source_name": "My_Config", "source_key": "app-env"},
        ],
    )

    assert response.status_code == 400
    assert "valid Kubernetes resource name" in response.get_json()["error"]


def test_create_project_rejects_empty_kubernetes_source_key_in_kubernetes_mode(client, app):
    with app.app_context():
        app.config["CONTROL_PLANE_EXECUTOR"] = "kubernetes"

    response = create_project(
        client,
        name="invalid-k8s-source-key-project",
        env_vars=[
            {"name": "APP_ENV", "value_source": "configmap_key_ref", "source_name": "my-app-config", "source_key": ""},
        ],
    )

    assert response.status_code == 400
    assert "source_key" in response.get_json()["error"]


def test_create_project_rejects_duplicate_kubernetes_env_var_names_in_kubernetes_mode(client, app):
    with app.app_context():
        app.config["CONTROL_PLANE_EXECUTOR"] = "kubernetes"

    response = create_project(
        client,
        name="duplicate-k8s-env-name-project",
        env_vars=[
            {"name": "APP_ENV", "value_source": "configmap_key_ref", "source_name": "my-app-config", "source_key": "app-env"},
            {"name": "APP_ENV", "value_source": "secret_key_ref", "source_name": "my-app-secret", "source_key": "database-url"},
        ],
    )

    assert response.status_code == 400
    assert "Duplicate env var name 'APP_ENV'" in response.get_json()["error"]


def test_create_project_accepts_valid_kubernetes_env_refs_in_kubernetes_mode(client, app):
    with app.app_context():
        app.config["CONTROL_PLANE_EXECUTOR"] = "kubernetes"

    response = create_project(
        client,
        name="valid-k8s-env-ref-project",
        env_vars=[
            {"name": "APP_ENV", "value_source": "configmap_key_ref", "source_name": "my-app-config", "source_key": "app-env"},
            {"name": "DATABASE_URL", "value_source": "secret_key_ref", "source_name": "my-app-secret", "source_key": "database-url"},
        ],
    )

    assert response.status_code == 201
    assert response.get_json()["env_vars"][0]["source_name"] == "my-app-config"


def test_create_project_rejects_literal_secret_env_values_in_kubernetes_mode(client, app):
    with app.app_context():
        app.config["CONTROL_PLANE_EXECUTOR"] = "kubernetes"

    response = create_project(
        client,
        name="invalid-k8s-literal-secret-project",
        env_vars=[
            {"name": "DATABASE_URL", "value": "postgres://secret", "is_secret": True},
        ],
    )

    assert response.status_code == 400
    assert "secret_key_ref" in response.get_json()["error"]


def test_create_project_allows_duplicate_env_var_names_outside_kubernetes_mode(client, app):
    with app.app_context():
        app.config["CONTROL_PLANE_EXECUTOR"] = "fake"

    response = create_project(
        client,
        name="duplicate-nonk8s-env-name-project",
        env_vars=[
            {"name": "APP_ENV", "value": "dev"},
            {"name": "APP_ENV", "value": "prod"},
        ],
    )

    assert response.status_code == 201


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
    assert len(deployments["items"]) == 1
    assert deployments["pagination"] == {"limit": 20, "count": 1, "next_before_deployment_id": deployment["id"]}
    assert deployments["items"][0]["build"]["image_tag"] == "abc123def456"

    builds_response = client.get(f"/api/projects/{project_id}/builds")
    assert builds_response.status_code == 200
    builds = builds_response.get_json()
    assert len(builds) == 1
    assert builds[0]["status"] == "pending"


def test_trigger_deployment_rejects_shell_control_test_command(client):
    project_response = create_project(client, name="invalid-deployment-command-app")
    project_id = project_response.get_json()["id"]

    response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": "abc123def456",
            "registry": "ghcr.io/example",
            "image_name": "invalid-deployment-command-app",
            "image_tag": "abc123def456",
            "build_status": "pending",
            "status": "pending",
            "test_command": "pytest -q > test-output.txt",
        },
    )

    assert response.status_code == 400
    assert "test_command" in response.get_json()["error"]
    assert "Shell control" in response.get_json()["error"]


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


def test_update_deployment_rejects_service_url(client):
    project_response = create_project(client, name="service-url-patch-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "pending"},
    )

    response = client.patch(
        f"/api/projects/{project_id}/deployments/{deployment_response.get_json()['id']}",
        json={"service_url": "http://127.0.0.1:2375"},
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "Provide at least one updatable field: status, build_status"}


def test_trigger_deployment_rejects_missing_commit_sha(client):
    project_response = create_project(client, name="invalid-deploy-app")
    project_id = project_response.get_json()["id"]

    response = client.post(f"/api/projects/{project_id}/deployments", json={"status": "pending"})

    assert response.status_code == 400
    assert response.get_json() == {"error": "Missing required field: commit_sha"}


def test_api_rejects_request_body_over_configured_limit(client, app):
    app.config["MAX_CONTENT_LENGTH"] = 64

    response = client.post(
        "/api/projects",
        data=b'{"name":"' + (b"x" * 128) + b'"}',
        content_type="application/json",
    )

    assert response.status_code == 413


def test_create_project_deployment_rejects_kubernetes_executor_without_required_settings(client, app):
    project_response = create_project(client, name="k8s-prereq-build-app")
    project_id = project_response.get_json()["id"]

    with app.app_context():
        app.config["CONTROL_PLANE_EXECUTOR"] = "kubernetes"
        app.config["CONTROL_PLANE_REGISTRY_ENABLED"] = False
        app.config["CONTROL_PLANE_REGISTRY_URL"] = None
        app.config["CONTROL_PLANE_REGISTRY_NAMESPACE"] = None
        app.config["CONTROL_PLANE_KUBECONFIG"] = None

    response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "pending"},
    )

    assert response.status_code == 409
    assert "Kubernetes executor is not ready for deployments" in response.get_json()["error"]
    assert "CONTROL_PLANE_REGISTRY_ENABLED=true" in response.get_json()["error"]
    assert "CONTROL_PLANE_KUBECONFIG" in response.get_json()["error"]


def test_deploy_project_creates_pending_records_from_minimal_input(client, app, monkeypatch):
    project_response = create_project(client, name="deploy-now-app")
    project_id = project_response.get_json()["id"]

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef"
    )

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

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "fedcba9876543210"
    )

    response = client.post(
        f"/api/projects/{project_id}/deploy",
        json={"branch": "release"},
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["branch"] == "release"
    assert payload["image_ref"] == "docker.io/example/registry-deploy-app:fedcba987654"


def test_deploy_project_allows_explicit_test_command_override(client, monkeypatch):
    project_response = create_project(client, name="override-test-command-app", default_test_command="pytest -q")
    project_id = project_response.get_json()["id"]

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "fedcba9876543210"
    )

    response = client.post(
        f"/api/projects/{project_id}/deploy",
        json={"test_command": "ruff check ."},
    )

    assert response.status_code == 201
    deployment_id = response.get_json()["deployment_id"]
    deployment = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}").get_json()
    assert deployment["build"]["test_command"] == "ruff check ."


def test_deploy_project_allows_explicit_null_test_command_override(client, monkeypatch):
    project_response = create_project(client, name="disable-default-test-command-app", default_test_command="pytest -q")
    project_id = project_response.get_json()["id"]

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "fedcba9876543210"
    )

    response = client.post(
        f"/api/projects/{project_id}/deploy",
        json={"test_command": None},
    )

    assert response.status_code == 201
    deployment_id = response.get_json()["deployment_id"]
    deployment = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}").get_json()
    assert deployment["build"]["test_command"] is None


def test_deploy_project_rejects_shell_wrapper_test_command(client, monkeypatch):
    project_response = create_project(client, name="invalid-deploy-command-app")
    project_id = project_response.get_json()["id"]

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "fedcba9876543210"
    )

    response = client.post(
        f"/api/projects/{project_id}/deploy",
        json={"test_command": "sh -c true"},
    )

    assert response.status_code == 400
    assert "test_command" in response.get_json()["error"]
    assert "Shell wrappers" in response.get_json()["error"]


def test_deploy_project_returns_conflict_when_commit_resolution_fails(client, monkeypatch):
    project_response = create_project(client, name="broken-deploy-app")
    project_id = project_response.get_json()["id"]

    monkeypatch.setattr(
        deployment_orchestration_api,
        "resolve_project_commit_sha",
        lambda project, branch: (_ for _ in ()).throw(ValueError("Unable to resolve commit for branch 'main': boom")),
    )

    response = client.post(f"/api/projects/{project_id}/deploy", json={})

    assert response.status_code == 409
    assert response.get_json() == {"error": "Unable to resolve commit for branch 'main': boom"}


def test_deploy_project_resolves_private_github_commit_with_token_env(client, monkeypatch):
    calls = []

    def fake_run(command, capture_output, text, timeout, check, env=None):
        calls.append({"command": command, "env": env})
        return subprocess.CompletedProcess(command, 0, stdout="0123456789abcdef\trefs/heads/main\n", stderr="")

    monkeypatch.setenv("CONTROL_PLANE_GIT_TOKEN_GITHUB", "unit-test-github-token")
    monkeypatch.setattr(deployment_orchestration_api.subprocess, "run", fake_run)
    project_response = create_project(
        client,
        name="private-github-deploy-app",
        repo_url="https://github.com/example/private-repo",
        git_auth_type="token",
        git_secret_ref="GITHUB",
    )
    project_id = project_response.get_json()["id"]

    response = client.post(f"/api/projects/{project_id}/deploy", json={})

    assert response.status_code == 201
    assert response.get_json()["commit_sha"] == "0123456789abcdef"
    assert calls[0]["command"] == [
        "git",
        "ls-remote",
        "https://github.com/example/private-repo.git",
        "refs/heads/main",
    ]
    assert calls[0]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert calls[0]["env"]["GIT_CONFIG_KEY_0"] == "http.extraheader"
    encoded = calls[0]["env"]["GIT_CONFIG_VALUE_0"].split("basic ", 1)[1]
    assert base64.b64decode(encoded).decode("utf-8") == "x-access-token:unit-test-github-token"


def test_deploy_project_reports_missing_git_token_env_for_private_repo(client, monkeypatch):
    monkeypatch.delenv("CONTROL_PLANE_GIT_TOKEN_GITHUB", raising=False)
    project_response = create_project(
        client,
        name="missing-private-github-token-app",
        repo_url="https://github.com/example/private-repo",
        git_auth_type="token",
        git_secret_ref="GITHUB",
    )
    project_id = project_response.get_json()["id"]

    response = client.post(f"/api/projects/{project_id}/deploy", json={})

    assert response.status_code == 409
    assert response.get_json() == {
        "error": (
            "Unable to resolve commit for branch 'main': "
            "Git token environment variable 'CONTROL_PLANE_GIT_TOKEN_GITHUB' is not set"
        )
    }


def test_deploy_project_rejects_kubernetes_executor_without_required_settings(client, app):
    project_response = create_project(client, name="k8s-prereq-deploy-app")
    project_id = project_response.get_json()["id"]

    with app.app_context():
        app.config["CONTROL_PLANE_EXECUTOR"] = "kubernetes"
        app.config["CONTROL_PLANE_REGISTRY_ENABLED"] = True
        app.config["CONTROL_PLANE_REGISTRY_URL"] = "docker.io"
        app.config["CONTROL_PLANE_REGISTRY_NAMESPACE"] = "example"
        app.config["CONTROL_PLANE_KUBECONFIG"] = None

    response = client.post(f"/api/projects/{project_id}/deploy", json={})

    assert response.status_code == 409
    assert "CONTROL_PLANE_KUBECONFIG" in response.get_json()["error"]


def test_get_latest_deployment_returns_404_when_project_has_no_deployments(client):
    project_response = create_project(client, name="empty-deployments-app")
    project_id = project_response.get_json()["id"]

    response = client.get(f"/api/projects/{project_id}/deployments/latest")

    assert response.status_code == 404
    assert response.get_json() == {"error": "Project has no deployments"}


def test_get_latest_deployment_returns_most_recent_deployment(client, monkeypatch):
    project_response = create_project(client, name="latest-deploy-app")
    project_id = project_response.get_json()["id"]

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef"
    )
    first_response = client.post(f"/api/projects/{project_id}/deploy", json={})
    first_deployment_id = first_response.get_json()["deployment_id"]

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "fedcba9876543210"
    )
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
    assert payload["preflight_status"] is None
    assert payload["preflight_summary"] is None
    assert payload["preflight_completed_at"] is None
    assert payload["service_url"] is None
    assert payload["created_at"] is not None
    assert payload["updated_at"] is not None


def test_retry_deployment_creates_new_pending_deployment_from_original_settings(client, monkeypatch):
    project_response = create_project(client, name="retry-app")
    project_id = project_response.get_json()["id"]

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef"
    )
    original_response = client.post(
        f"/api/projects/{project_id}/deploy",
        json={"branch": "release", "test_command": "pytest -q"},
    )
    original_deployment_id = original_response.get_json()["deployment_id"]

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "fedcba9876543210"
    )
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
    assert payload["preflight_status"] is None
    assert payload["preflight_summary"] is None
    assert payload["preflight_completed_at"] is None

    deployment_response = client.get(f"/api/projects/{project_id}/deployments/{payload['deployment_id']}")
    deployment = deployment_response.get_json()
    assert deployment["build"]["test_command"] == "pytest -q"
    assert deployment["events"][0]["message"] == "Deployment retry requested for branch 'release' at commit 'fedcba987654'"


def test_retry_deployment_returns_conflict_when_commit_resolution_fails(client, monkeypatch):
    project_response = create_project(client, name="broken-retry-app")
    project_id = project_response.get_json()["id"]

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef"
    )
    original_response = client.post(f"/api/projects/{project_id}/deploy", json={})
    original_deployment_id = original_response.get_json()["deployment_id"]

    monkeypatch.setattr(
        deployment_orchestration_api,
        "resolve_project_commit_sha",
        lambda project, branch: (_ for _ in ()).throw(ValueError("Unable to resolve commit for branch 'main': boom")),
    )

    response = client.post(f"/api/projects/{project_id}/deployments/{original_deployment_id}/retry")

    assert response.status_code == 409
    assert response.get_json() == {"error": "Unable to resolve commit for branch 'main': boom"}


def test_retry_deployment_rejects_kubernetes_executor_without_required_settings(client, app, monkeypatch):
    project_response = create_project(client, name="k8s-prereq-retry-app")
    project_id = project_response.get_json()["id"]

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef"
    )
    original_response = client.post(f"/api/projects/{project_id}/deploy", json={})
    original_deployment_id = original_response.get_json()["deployment_id"]

    with app.app_context():
        app.config["CONTROL_PLANE_EXECUTOR"] = "kubernetes"
        app.config["CONTROL_PLANE_REGISTRY_ENABLED"] = True
        app.config["CONTROL_PLANE_REGISTRY_URL"] = None
        app.config["CONTROL_PLANE_REGISTRY_NAMESPACE"] = "example"
        app.config["CONTROL_PLANE_KUBECONFIG"] = "/tmp/kubeconfig"

    response = client.post(f"/api/projects/{project_id}/deployments/{original_deployment_id}/retry")

    assert response.status_code == 409
    assert "CONTROL_PLANE_REGISTRY_URL" in response.get_json()["error"]


def test_retry_deployment_does_not_expose_other_project_deployment(client, monkeypatch):
    first_project_response = create_project(client, name="retry-owner-app")
    first_project_id = first_project_response.get_json()["id"]
    second_project_response = create_project(client, name="retry-other-app")
    second_project_id = second_project_response.get_json()["id"]

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef"
    )
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

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef"
    )
    client.post(f"/api/projects/{project_id}/deploy", json={"branch": "main"})

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "fedcba9876543210"
    )
    latest_response = client.post(
        f"/api/projects/{project_id}/deploy",
        json={"branch": "release", "test_command": "pytest -q"},
    )
    latest_deployment_id = latest_response.get_json()["deployment_id"]

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "0011223344556677"
    )
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
    assert payload["preflight_status"] is None
    assert payload["preflight_summary"] is None
    assert payload["preflight_completed_at"] is None

    deployment_response = client.get(f"/api/projects/{project_id}/deployments/{payload['deployment_id']}")
    deployment = deployment_response.get_json()
    assert deployment["build"]["test_command"] == "pytest -q"
    assert deployment["events"][0]["message"] == "Project redeploy requested for branch 'release' at commit '001122334455'"


def test_get_project_deployment_and_list_include_preflight_fields(client, app):
    project_response = create_project(client, name="deployment-preflight-fields-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "failed", "build_status": "failed"},
    )
    deployment_id = deployment_response.get_json()["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.preflight_status = "failed"
        deployment.preflight_summary = "Missing Kubernetes referenced resources: ConfigMap/my-app-config"
        deployment.preflight_metadata_json = {"missing_resources": [{"kind": "ConfigMap", "name": "my-app-config"}]}
        deployment.preflight_completed_at = deployment.created_at
        db.session.commit()

    get_response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}")
    list_response = client.get(f"/api/projects/{project_id}/deployments")

    assert get_response.status_code == 200
    get_payload = get_response.get_json()
    assert get_payload["preflight_status"] == "failed"
    assert get_payload["preflight_summary"] == "Missing Kubernetes referenced resources: ConfigMap/my-app-config"
    assert get_payload["preflight_completed_at"] is not None

    assert list_response.status_code == 200
    list_payload = list_response.get_json()
    assert list_payload["items"][0]["preflight_status"] == "failed"
    assert list_payload["items"][0]["preflight_summary"] == "Missing Kubernetes referenced resources: ConfigMap/my-app-config"
    assert list_payload["items"][0]["preflight_completed_at"] is not None


def test_deployment_live_health_endpoint_returns_safe_probe_result(client, monkeypatch):
    project_response = create_project(client, name="live-health-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "running", "build_status": "succeeded"},
    )
    deployment_id = deployment_response.get_json()["id"]

    monkeypatch.setattr(
        "control_plane.api.projects.probe_deployment_health",
        lambda deployment: {
            "status": "unhealthy",
            "message": "Workload healthcheck returned HTTP 503.",
            "http_status": 503,
            "checked_at": "2026-06-30T12:00:00+00:00",
        },
    )

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/live-health")

    assert response.status_code == 200
    assert response.get_json()["status"] == "unhealthy"
    assert response.get_json()["http_status"] == 503


def test_retry_deployment_preserves_explicitly_disabled_test_command(client, app, monkeypatch):
    project_response = create_project(client, name="retry-default-test-command-app", default_test_command="pytest -q")
    project_id = project_response.get_json()["id"]

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef"
    )
    original_response = client.post(
        f"/api/projects/{project_id}/deploy",
        json={"test_command": None},
    )
    original_deployment_id = original_response.get_json()["deployment_id"]

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "fedcba9876543210"
    )
    retry_response = client.post(f"/api/projects/{project_id}/deployments/{original_deployment_id}/retry")

    assert retry_response.status_code == 201
    deployment_id = retry_response.get_json()["deployment_id"]
    deployment = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}").get_json()
    assert deployment["build"]["test_command"] is None


def test_redeploy_project_falls_back_to_project_default_test_command(client, monkeypatch):
    project_response = create_project(client, name="redeploy-default-test-command-app", default_test_command="pytest -q")
    project_id = project_response.get_json()["id"]

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef"
    )
    client.post(
        f"/api/projects/{project_id}/deploy",
        json={"test_command": None},
    )

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "fedcba9876543210"
    )
    redeploy_response = client.post(f"/api/projects/{project_id}/redeploy")

    assert redeploy_response.status_code == 201
    deployment_id = redeploy_response.get_json()["deployment_id"]
    deployment = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}").get_json()
    assert deployment["build"]["test_command"] == "pytest -q"


def test_redeploy_project_returns_conflict_when_commit_resolution_fails(client, monkeypatch):
    project_response = create_project(client, name="broken-redeploy-app")
    project_id = project_response.get_json()["id"]

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef"
    )
    client.post(f"/api/projects/{project_id}/deploy", json={})

    monkeypatch.setattr(
        deployment_orchestration_api,
        "resolve_project_commit_sha",
        lambda project, branch: (_ for _ in ()).throw(ValueError("Unable to resolve commit for branch 'main': boom")),
    )

    response = client.post(f"/api/projects/{project_id}/redeploy")

    assert response.status_code == 409
    assert response.get_json() == {"error": "Unable to resolve commit for branch 'main': boom"}


def test_redeploy_project_rejects_kubernetes_executor_without_required_settings(client, app, monkeypatch):
    project_response = create_project(client, name="k8s-prereq-redeploy-app")
    project_id = project_response.get_json()["id"]

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef"
    )
    client.post(f"/api/projects/{project_id}/deploy", json={})

    with app.app_context():
        app.config["CONTROL_PLANE_EXECUTOR"] = "kubernetes"
        app.config["CONTROL_PLANE_REGISTRY_ENABLED"] = True
        app.config["CONTROL_PLANE_REGISTRY_URL"] = "docker.io"
        app.config["CONTROL_PLANE_REGISTRY_NAMESPACE"] = None
        app.config["CONTROL_PLANE_KUBECONFIG"] = "/tmp/kubeconfig"

    response = client.post(f"/api/projects/{project_id}/redeploy")

    assert response.status_code == 409
    assert "CONTROL_PLANE_REGISTRY_NAMESPACE" in response.get_json()["error"]


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
    assert payload["events"][-2]["event_type"] == "deployment.stop_started"
    assert payload["events"][-1]["event_type"] == "deployment.stopped"
    assert payload["events"][-1]["metadata_json"]["log_path"] == "/tmp/stop.log"
    assert payload["events"][-1]["metadata_json"]["runtime_log_path"] == "/tmp/runtime.log"


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
    assert payload["events"][-2]["event_type"] == "deployment.stop_started"
    assert payload["events"][-2]["message"] == "Stop requested by deployer"
    assert payload["events"][-1]["event_type"] == "deployment.stopped"

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


def test_get_deployment_summary_returns_successful_view(client, app, monkeypatch, tmp_path):
    project_response = create_project(client, name="summary-app")
    project_id = project_response.get_json()["id"]
    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef"
    )
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
    assert payload["helm_release_name"] is None
    assert payload["helm_namespace"] is None
    assert payload["helm_chart_path"] is None
    assert payload["kubernetes_namespace"] == "default"
    assert payload["kubernetes_deployment_name"] == "paas-k8s-summary-app-1"
    assert payload["kubernetes_service_name"] == "paas-k8s-summary-app-1-svc"
    assert payload["last_kubernetes_failure_stage"] is None
    assert payload["last_kubernetes_failure_summary"] is None


def test_get_deployment_summary_includes_persisted_helm_runtime_metadata(client, app):
    project_response = create_project(client, name="helm-summary-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "running", "build_status": "succeeded"},
    )
    deployment_id = deployment_response.get_json()["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        deployment.service_url = "http://paas-helm-summary-app-production-1-generic-web-app.apps.svc.cluster.local:5000"
        deployment.helm_release_name = "paas-helm-summary-app-production-1"
        deployment.helm_namespace = "apps"
        deployment.helm_chart_path = "deploy/helm/generic-web-app"
        db.session.commit()

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/summary")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["helm_release_name"] == "paas-helm-summary-app-production-1"
    assert payload["helm_namespace"] == "apps"
    assert payload["helm_chart_path"] == "deploy/helm/generic-web-app"
    assert payload["kubernetes_namespace"] == "apps"


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
    assert payload["diagnostics"]["healthcheck_service_summary"] == "Endpoints: <none> | Session Affinity: None"


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
