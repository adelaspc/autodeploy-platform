import base64
import subprocess

from control_plane.application.deployments import orchestration as deployment_orchestration_api
from control_plane.extensions import db
from control_plane.models import PlatformDeployment
from tests.api.project_test_helpers import create_project, event_by_type


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
    assert event_by_type(updated["events"], "deployment.status_updated")["status"] == "cloning"
    assert event_by_type(updated["events"], "build.status_updated")["status"] == "cloning"


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


def test_update_deployment_rejects_reusing_running_record_for_rollout(client):
    project_id = create_project(client, name="immutable-running-deployment-app").get_json()["id"]
    deployment = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": "abc123def456",
            "status": "running",
            "build_status": "succeeded",
        },
    ).get_json()

    response = client.patch(
        f"/api/projects/{project_id}/deployments/{deployment['id']}",
        json={"status": "deploying"},
    )

    assert response.status_code == 400
    assert "Invalid deployment transition from running to deploying" in response.get_json()["error"]


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
