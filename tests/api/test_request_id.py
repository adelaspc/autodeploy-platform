import hashlib
import hmac
import json
import re


def bearer_headers(token, request_id=None):
    headers = {"Authorization": f"Bearer {token}"}
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    return headers


def configure_api_tokens(app):
    app.config["CONTROL_PLANE_API_TOKEN_READ_ONLY"] = "read-token"
    app.config["CONTROL_PLANE_API_TOKEN_DEPLOYER"] = "deployer-token"
    app.config["CONTROL_PLANE_API_TOKEN_ADMIN"] = "admin-token"


def create_project_payload(name="request-id-app"):
    return {
        "name": name,
        "repo_url": "https://github.com/example/request-id-app",
        "branch": "main",
        "dockerfile_path": "Dockerfile",
        "build_context": ".",
        "port": 5000,
        "healthcheck_path": "/health",
        "env_vars": [],
        "trigger": "github_push",
        "runtime": "dockerfile",
    }


def github_headers(app, payload, *, event="push", delivery_id="delivery-123", request_id=None):
    body = json.dumps(payload).encode("utf-8")
    secret = app.config["CONTROL_PLANE_GITHUB_WEBHOOK_SECRET"].encode("utf-8")
    digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": f"sha256={digest}",
    }
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    return body, headers


def test_public_health_response_includes_generated_request_id(client):
    response = client.get("/health")

    request_id = response.headers.get("X-Request-ID")
    assert response.status_code == 200
    assert request_id is not None
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)


def test_response_preserves_valid_client_provided_request_id(client):
    response = client.get("/health", headers={"X-Request-ID": "client-request-123"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "client-request-123"


def test_invalid_or_too_long_request_id_is_replaced(client):
    response = client.get("/health", headers={"X-Request-ID": "x" * 300})

    request_id = response.headers["X-Request-ID"]
    assert response.status_code == 200
    assert request_id != "x" * 300
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)


def test_audit_events_include_request_id(client, app):
    configure_api_tokens(app)

    request_id = "audit-request-123"
    create_response = client.post(
        "/api/projects",
        json=create_project_payload(name="request-id-audit-app"),
        headers=bearer_headers("admin-token", request_id=request_id),
    )
    audit_response = client.get("/api/audit-events", headers=bearer_headers("read-token"))

    assert create_response.status_code == 201
    assert create_response.headers["X-Request-ID"] == request_id
    created_event = audit_response.get_json()["items"][0]
    assert created_event["action"] == "project.created"
    assert created_event["request_id"] == request_id


def test_auth_error_response_includes_request_id(client, app):
    configure_api_tokens(app)

    response = client.get("/api/projects")

    payload = response.get_json()
    assert response.status_code == 401
    assert payload["error"] == "Missing bearer token"
    assert payload["request_id"] == response.headers["X-Request-ID"]


def test_webhook_route_remains_compatible_with_request_id(client, app):
    configure_api_tokens(app)
    client.post(
        "/api/projects",
        json=create_project_payload(name="request-id-webhook-app"),
        headers=bearer_headers("admin-token"),
    )
    payload = {
        "ref": "refs/heads/main",
        "after": "0123456789abcdef0123456789abcdef01234567",
        "repository": {
            "clone_url": "https://github.com/example/request-id-app.git",
        },
    }
    body, headers = github_headers(app, payload, delivery_id="request-id-webhook", request_id="webhook-request-1")

    response = client.post("/api/webhooks/github", data=body, headers=headers)

    assert response.status_code == 202
    assert response.headers["X-Request-ID"] == "webhook-request-1"
