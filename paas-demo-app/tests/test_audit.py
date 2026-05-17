import backend.api.deployment_orchestration as deployment_orchestration_api


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


def create_project_payload(name="audit-app", **overrides):
    payload = {
        "name": name,
        "repo_url": "https://github.com/example/audit-app",
        "branch": "main",
        "dockerfile_path": "Dockerfile",
        "build_context": ".",
        "port": 5000,
        "healthcheck_path": "/health",
        "env_vars": [],
        "trigger": "manual",
        "runtime": "dockerfile",
    }
    payload.update(overrides)
    return payload


def test_audit_endpoint_requires_authentication(client, app):
    configure_api_tokens(app)

    response = client.get("/api/audit-events")

    assert response.status_code == 401
    payload = response.get_json()
    assert payload["error"] == "Missing bearer token"
    assert payload["request_id"] == response.headers["X-Request-ID"]


def test_read_only_token_can_view_audit_events(client, app):
    headers = configure_api_tokens(app)
    client.post("/api/projects", json=create_project_payload(), headers=headers["admin"])

    response = client.get("/api/audit-events", headers=headers["read_only"])

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["pagination"]["limit"] == 20
    assert len(payload["items"]) >= 1
    assert payload["items"][0]["action"] == "project.created"


def test_mutating_actions_create_audit_events(client, app, monkeypatch):
    headers = configure_api_tokens(app)
    create_response = client.post(
        "/api/projects",
        json=create_project_payload(name="audit-mutations-app"),
        headers=headers["admin"],
    )
    project_id = create_response.get_json()["id"]

    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef"
    )
    deploy_response = client.post(f"/api/projects/{project_id}/deploy", json={}, headers=headers["deployer"])
    deployment_id = deploy_response.get_json()["deployment_id"]

    patch_response = client.patch(
        f"/api/projects/{project_id}/deployments/{deployment_id}",
        json={"status": "failed", "message": "marking failed"},
        headers=headers["admin"],
    )

    audit_response = client.get("/api/audit-events", headers=headers["read_only"])

    assert patch_response.status_code == 200
    assert audit_response.status_code == 200
    actions = [item["action"] for item in audit_response.get_json()["items"]]
    assert "project.created" in actions
    assert "deployment.deploy_triggered" in actions
    assert "deployment.patched" in actions


def test_read_only_mutation_attempt_is_audited_without_sensitive_values(client, app):
    headers = configure_api_tokens(app)

    response = client.post(
        "/api/projects",
        json=create_project_payload(
            name="audit-denied-app",
            env_vars=[{"name": "DATABASE_URL", "value": "postgres://secret"}],
            git_auth_type="token",
            git_secret_ref="PROD_TOKEN",
        ),
        headers=headers["read_only"],
    )
    audit_response = client.get("/api/audit-events", headers=headers["read_only"])

    assert response.status_code == 403
    assert audit_response.status_code == 200
    denied_event = audit_response.get_json()["items"][0]
    assert denied_event["action"] == "api.access_denied"
    assert denied_event["status"] == "failure"
    assert denied_event["actor_role"] == "read_only"
    metadata = denied_event["metadata_json"]
    assert metadata["reason"] == "insufficient_role"
    assert "token" not in str(metadata).lower()
    assert "postgres://secret" not in str(metadata)
    assert "PROD_TOKEN" not in str(metadata)


def test_project_create_audit_metadata_does_not_store_env_vars_or_secret_material(client, app):
    headers = configure_api_tokens(app)

    client.post(
        "/api/projects",
        json=create_project_payload(
            name="audit-redaction-app",
            env_vars=[{"name": "DATABASE_URL", "value": "postgres://secret"}],
            git_auth_type="token",
            git_secret_ref="PROD_TOKEN",
        ),
        headers=headers["admin"],
    )
    audit_response = client.get("/api/audit-events", headers=headers["read_only"])

    assert audit_response.status_code == 200
    created_event = audit_response.get_json()["items"][0]
    assert created_event["action"] == "project.created"
    metadata = created_event["metadata_json"]
    assert "env_vars" not in metadata
    assert "git_secret_ref" not in metadata
    assert "postgres://secret" not in str(metadata)
    assert "PROD_TOKEN" not in str(metadata)


def test_audit_endpoint_supports_limit_and_before_id_pagination(client, app):
    headers = configure_api_tokens(app)
    client.post("/api/projects", json=create_project_payload(name="audit-page-1"), headers=headers["admin"])
    client.post("/api/projects", json=create_project_payload(name="audit-page-2"), headers=headers["admin"])

    first_page = client.get("/api/audit-events?limit=1", headers=headers["read_only"])

    assert first_page.status_code == 200
    first_payload = first_page.get_json()
    assert len(first_payload["items"]) == 1
    before_id = first_payload["pagination"]["next_before_id"]

    second_page = client.get(f"/api/audit-events?limit=1&before_id={before_id}", headers=headers["read_only"])

    assert second_page.status_code == 200
    second_payload = second_page.get_json()
    assert len(second_payload["items"]) == 1
    assert second_payload["items"][0]["id"] < first_payload["items"][0]["id"]


def test_audit_metadata_redaction_handles_nested_secret_values(client, app):
    headers = configure_api_tokens(app)
    client.post("/api/projects", json=create_project_payload(name="audit-nested-redaction"), headers=headers["admin"])

    from backend.api.audit_service import record_audit_event

    with app.test_request_context("/api/audit-events", headers=headers["admin"]):
        record_audit_event(
            action="deployment.patched",
            resource_type="deployment",
            resource_id="1",
            metadata={
                "outer": {
                    "authorization_header": "Bearer super-secret-token",
                    "details": [{"env_vars": [{"name": "DATABASE_URL", "value": "postgres://secret", "is_secret": True}]}],
                }
            },
        )

    payload = client.get("/api/audit-events", headers=headers["read_only"]).get_json()
    nested_event = next(item for item in payload["items"] if item["action"] == "deployment.patched")
    assert "super-secret-token" not in str(nested_event)
    assert "postgres://secret" not in str(nested_event)
    assert "[REDACTED]" in str(nested_event)
