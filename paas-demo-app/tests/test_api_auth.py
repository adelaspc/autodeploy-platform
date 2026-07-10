import hashlib
import hmac
import json

import backend.api.deployment_orchestration as deployment_orchestration_api
import pytest

from backend import create_app
from tests.conftest import TestConfig


def bearer_headers(token):
    return {"Authorization": f"Bearer {token}"}


def configure_api_tokens(app):
    app.config["CONTROL_PLANE_API_TOKEN_READ_ONLY"] = "read-token"
    app.config["CONTROL_PLANE_API_TOKEN_DEPLOYER"] = "deployer-token"
    app.config["CONTROL_PLANE_API_TOKEN_ADMIN"] = "admin-token"
    return {
        "read_only": bearer_headers("read-token"),
        "deployer": bearer_headers("deployer-token"),
        "admin": bearer_headers("admin-token"),
    }


def configure_api_tokens_json(app):
    app.config["CONTROL_PLANE_API_TOKENS_JSON"] = json.dumps(
        [
            {"name": "ops-read", "role": "read_only", "token": "json-read-token"},
            {"name": "ci-deployer", "role": "deployer", "token": "json-deployer-token"},
            {"name": "break-glass-admin", "role": "admin", "token": "json-admin-token"},
        ]
    )
    return {
        "read_only": bearer_headers("json-read-token"),
        "deployer": bearer_headers("json-deployer-token"),
        "admin": bearer_headers("json-admin-token"),
    }


def create_project_payload(name="secured-app"):
    return {
        "name": name,
        "repo_url": "https://github.com/example/secured-app",
        "branch": "main",
        "dockerfile_path": "Dockerfile",
        "build_context": ".",
        "port": 5000,
        "healthcheck_path": "/health",
        "env_vars": [],
        "trigger": "manual",
        "runtime": "dockerfile",
    }


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


def assert_bearer_challenge(response):
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_auth_can_be_disabled_only_with_explicit_local_opt_in():
    class LocalAuthDisabledConfig(TestConfig):
        CONTROL_PLANE_ENV = "development"
        CONTROL_PLANE_ALLOW_AUTH_DISABLED = True

        @staticmethod
        def init_app(app):
            from backend.config import Config

            Config.init_app(app)

    app = create_app(LocalAuthDisabledConfig)

    assert app.config["CONTROL_PLANE_ALLOW_AUTH_DISABLED"] is True


def test_auth_disabled_without_explicit_opt_in_fails_fast():
    class MissingAuthConfig(TestConfig):
        CONTROL_PLANE_ENV = "development"
        CONTROL_PLANE_ALLOW_AUTH_DISABLED = False

        @staticmethod
        def init_app(app):
            from backend.config import Config

            Config.init_app(app)

    with pytest.raises(RuntimeError, match="API bearer tokens must be configured"):
        create_app(MissingAuthConfig)


def test_auth_disabled_opt_in_is_rejected_outside_local_envs():
    class ProductionAuthDisabledConfig(TestConfig):
        CONTROL_PLANE_ENV = "production"
        CONTROL_PLANE_ALLOW_AUTH_DISABLED = True

        @staticmethod
        def init_app(app):
            from backend.config import Config

            Config.init_app(app)

    with pytest.raises(RuntimeError, match="API bearer tokens must be configured"):
        create_app(ProductionAuthDisabledConfig)


def test_token_config_allows_production_startup():
    class ProductionTokenConfig(TestConfig):
        CONTROL_PLANE_ENV = "production"
        CONTROL_PLANE_ALLOW_AUTH_DISABLED = False
        CONTROL_PLANE_API_TOKEN_ADMIN = "admin-token"

        @staticmethod
        def init_app(app):
            from backend.config import Config

            Config.init_app(app)

    app = create_app(ProductionTokenConfig)

    assert app.config["CONTROL_PLANE_API_TOKEN_ADMIN"] == "admin-token"


def test_public_health_endpoint_remains_open(client, app):
    configure_api_tokens(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_protected_route_rejects_missing_bearer_token(client, app):
    configure_api_tokens(app)

    response = client.get("/health/db")

    assert response.status_code == 401
    payload = response.get_json()
    assert payload["error"] == "Missing bearer token"
    assert payload["request_id"] == response.headers["X-Request-ID"]
    assert_bearer_challenge(response)


def test_protected_route_rejects_invalid_bearer_token(client, app):
    configure_api_tokens(app)

    response = client.get("/api/projects", headers=bearer_headers("wrong-token"))

    assert response.status_code == 401
    payload = response.get_json()
    assert payload["error"] == "Invalid bearer token"
    assert payload["request_id"] == response.headers["X-Request-ID"]
    assert_bearer_challenge(response)


def test_protected_route_rejects_malformed_authorization_header(client, app):
    configure_api_tokens(app)

    responses = [
        client.get("/api/projects", headers={"Authorization": "Bearer"}),
        client.get("/api/projects", headers={"Authorization": "Basic read-token"}),
        client.get("/api/projects", headers={"Authorization": "read-token"}),
    ]

    for response in responses:
        assert response.status_code == 401
        payload = response.get_json()
        assert payload["error"] == "Malformed bearer token"
        assert payload["request_id"] == response.headers["X-Request-ID"]
        assert_bearer_challenge(response)


def test_json_configured_tokens_can_authenticate_requests(client, app, monkeypatch):
    headers = configure_api_tokens_json(app)

    create_response = client.post("/api/projects", json=create_project_payload(name="json-auth-app"), headers=headers["admin"])
    project_id = create_response.get_json()["id"]

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef"
    )
    deploy_response = client.post(f"/api/projects/{project_id}/deploy", json={}, headers=headers["deployer"])
    deployment_id = deploy_response.get_json()["deployment_id"]
    read_response = client.get(
        f"/api/projects/{project_id}/deployments/{deployment_id}/summary",
        headers=headers["read_only"],
    )
    forbidden_response = client.patch(
        f"/api/projects/{project_id}",
        json={"branch": "release"},
        headers=headers["deployer"],
    )

    assert create_response.status_code == 201
    assert deploy_response.status_code == 201
    assert read_response.status_code == 200
    assert forbidden_response.status_code == 403


def test_json_and_legacy_token_config_are_additive(client, app):
    app.config["CONTROL_PLANE_API_TOKEN_ADMIN"] = "legacy-admin-token"
    app.config["CONTROL_PLANE_API_TOKENS_JSON"] = json.dumps(
        [{"name": "ops-read", "role": "read_only", "token": "json-read-token"}]
    )

    read_response = client.get("/health/platform", headers=bearer_headers("json-read-token"))
    admin_response = client.post(
        "/api/projects",
        json=create_project_payload(name="mixed-token-config-app"),
        headers=bearer_headers("legacy-admin-token"),
    )

    assert read_response.status_code == 200
    assert read_response.get_json()["api_auth"]["configured_roles"] == ["read_only", "admin"]
    assert admin_response.status_code == 201


def test_read_only_token_can_access_protected_read_endpoints(client, app, monkeypatch):
    headers = configure_api_tokens(app)
    create_response = client.post("/api/projects", json=create_project_payload(), headers=headers["admin"])
    project_id = create_response.get_json()["id"]

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef"
    )
    deploy_response = client.post(f"/api/projects/{project_id}/deploy", json={}, headers=headers["deployer"])
    deployment_id = deploy_response.get_json()["deployment_id"]

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/summary", headers=headers["read_only"])
    platform_response = client.get("/health/platform", headers=headers["read_only"])

    assert response.status_code == 200
    assert response.get_json()["deployment_id"] == deployment_id
    assert platform_response.status_code == 200


def test_read_only_token_cannot_mutate_projects(client, app):
    headers = configure_api_tokens(app)

    response = client.post("/api/projects", json=create_project_payload(), headers=headers["read_only"])

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["error"] == "Forbidden"
    assert payload["request_id"] == response.headers["X-Request-ID"]
    assert "WWW-Authenticate" not in response.headers


def test_deployer_token_can_trigger_deploy_retry_and_redeploy_actions(client, app, monkeypatch):
    headers = configure_api_tokens(app)
    create_response = client.post("/api/projects", json=create_project_payload(name="deployer-app"), headers=headers["admin"])
    project_id = create_response.get_json()["id"]

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef"
    )
    deploy_response = client.post(f"/api/projects/{project_id}/deploy", json={}, headers=headers["deployer"])
    deployment_id = deploy_response.get_json()["deployment_id"]

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "fedcba9876543210"
    )
    retry_response = client.post(
        f"/api/projects/{project_id}/deployments/{deployment_id}/retry",
        headers=headers["deployer"],
    )

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "0011223344556677"
    )
    redeploy_response = client.post(f"/api/projects/{project_id}/redeploy", headers=headers["deployer"])

    assert deploy_response.status_code == 201
    assert retry_response.status_code == 201
    assert redeploy_response.status_code == 201


def test_deployer_token_cannot_perform_admin_only_project_update(client, app):
    headers = configure_api_tokens(app)
    create_response = client.post(
        "/api/projects",
        json=create_project_payload(name="deployer-forbidden-app"),
        headers=headers["admin"],
    )
    project_id = create_response.get_json()["id"]

    forbidden_response = client.patch(
        f"/api/projects/{project_id}",
        json={"branch": "release"},
        headers=headers["deployer"],
    )

    assert forbidden_response.status_code == 403
    payload = forbidden_response.get_json()
    assert payload["error"] == "Forbidden"
    assert payload["request_id"] == forbidden_response.headers["X-Request-ID"]


def test_deployer_token_can_stop_deployment_through_dedicated_endpoint_only(client, app):
    headers = configure_api_tokens(app)
    create_response = client.post(
        "/api/projects",
        json=create_project_payload(name="deployer-stop-app"),
        headers=headers["admin"],
    )
    project_id = create_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "running", "build_status": "succeeded"},
        headers=headers["admin"],
    )
    deployment_id = deployment_response.get_json()["id"]

    forbidden_patch_response = client.patch(
        f"/api/projects/{project_id}/deployments/{deployment_id}",
        json={"status": "stopped"},
        headers=headers["deployer"],
    )
    stop_response = client.post(
        f"/api/projects/{project_id}/deployments/{deployment_id}/stop",
        headers=headers["deployer"],
    )

    assert forbidden_patch_response.status_code == 403
    assert stop_response.status_code == 200
    assert stop_response.get_json()["status"] == "stopped"


def test_admin_token_can_manage_projects_and_deployments(client, app):
    headers = configure_api_tokens(app)
    create_response = client.post("/api/projects", json=create_project_payload(name="admin-app"), headers=headers["admin"])
    project_id = create_response.get_json()["id"]

    patch_response = client.patch(
        f"/api/projects/{project_id}",
        json={"branch": "release"},
        headers=headers["admin"],
    )
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "running", "build_status": "succeeded"},
        headers=headers["admin"],
    )
    deployment_id = deployment_response.get_json()["id"]
    stop_response = client.patch(
        f"/api/projects/{project_id}/deployments/{deployment_id}",
        json={"status": "stopped"},
        headers=headers["admin"],
    )
    delete_response = client.delete(f"/api/projects/{project_id}", headers=headers["admin"])

    assert create_response.status_code == 201
    assert patch_response.status_code == 200
    assert deployment_response.status_code == 201
    assert stop_response.status_code == 200
    assert delete_response.status_code == 200


def test_webhook_authentication_remains_separate_from_bearer_tokens(client, app):
    configure_api_tokens(app)
    client.post(
        "/api/projects",
        json=create_project_payload(name="webhook-app") | {"trigger": "github_push"},
        headers=bearer_headers("admin-token"),
    )
    payload = {
        "ref": "refs/heads/main",
        "after": "0123456789abcdef0123456789abcdef01234567",
        "repository": {
            "clone_url": "https://github.com/example/secured-app.git",
        },
    }
    body, headers = github_headers(app, payload, delivery_id="push-auth-separate")

    invalid_response = client.post(
        "/api/webhooks/github",
        data=json.dumps(payload),
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": "sha256=invalid",
        },
    )
    valid_response = client.post("/api/webhooks/github", data=body, headers=headers)

    assert invalid_response.status_code == 401
    invalid_payload = invalid_response.get_json()
    assert invalid_payload["error"] == "Invalid GitHub webhook signature"
    assert invalid_payload["request_id"] == invalid_response.headers["X-Request-ID"]
    assert valid_response.status_code == 202
    assert valid_response.get_json()["status"] == "accepted"
