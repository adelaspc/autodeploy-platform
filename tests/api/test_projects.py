from control_plane.extensions import db
from control_plane.models import PlatformDeployment
from tests.api.project_test_helpers import create_project, github_headers


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


def test_create_project_rejects_github_repo_url_with_empty_path_segment(client):
    response = create_project(
        client,
        name="invalid-github-empty-segment-app",
        repo_url="https://github.com/example//repo",
    )

    assert response.status_code == 400
    assert "canonical GitHub HTTPS repository URL" in response.get_json()["error"]


def test_create_project_rejects_github_repo_url_with_repeated_git_suffix(client):
    response = create_project(
        client,
        name="invalid-github-repeated-suffix-app",
        repo_url="https://github.com/example/repo.git.git",
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


def test_create_project_rejects_env_var_without_value_or_source(client):
    response = create_project(
        client,
        name="incomplete-env-project",
        env_vars=[{"name": "OPTIONAL_SETTING"}],
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "Invalid env_vars[0]. Provide a literal 'value' or a supported 'value_source'"
    }


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
