import hashlib
import hmac
import json
from pathlib import Path

from backend.extensions import db
from backend.models import PlatformDeployment


def create_project(client, **overrides):
    payload = {
        "name": "activity-app",
        "repo_url": "https://github.com/example/activity-app",
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
    return client.post("/api/projects", json=payload)


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


def test_health_check(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_frontend_fallback_serves_built_index(client, app):
    dist_dir = Path(app.static_folder)
    dist_dir.mkdir(parents=True, exist_ok=True)
    index_path = dist_dir / "index.html"
    index_path.write_text("<!doctype html><title>PaaS Control Plane</title>", encoding="utf-8")

    response = client.get("/")

    assert response.status_code == 200
    assert b"PaaS Control Plane" in response.data


def test_frontend_fallback_does_not_shadow_api_routes(client):
    response = client.get("/api/projects")

    assert response.status_code == 200
    assert response.get_json() == []


def test_frontend_fallback_does_not_serve_index_for_unknown_api_route(client, app):
    dist_dir = Path(app.static_folder)
    dist_dir.mkdir(parents=True, exist_ok=True)
    (dist_dir / "index.html").write_text("<!doctype html><title>PaaS Control Plane</title>", encoding="utf-8")

    response = client.get("/api/not-real")

    assert response.status_code == 404
    assert b"PaaS Control Plane" not in response.data


def test_database_health_check(client):
    response = client.get("/health/db")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "database": "reachable"}


def test_platform_health_check_reports_default_executor_state(client):
    response = client.get("/health/platform")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok",
        "environment": "development",
        "executor": "fake",
        "executor_contract": {
            "name": "fake",
            "deploy_target": "fake",
            "runtime": "simulated",
            "healthcheck_strategy": "simulated",
            "requires_registry_push": False,
            "supports_runtime_logs": False,
            "supports_runtime_reconciliation": False,
            "managed_resources": [],
            "required_config": [],
            "optional_config": [],
        },
        "local_repo_paths_allowed": True,
        "deployment_creation_ready": True,
        "deployment_creation_error": None,
        "api_auth": {
            "enabled": False,
            "configured_roles": [],
            "token_transport": "bearer",
            "public_routes": ["/health"],
            "protected_health_routes": [
                "/health/db",
                "/health/platform",
                "/health/activity",
            ],
            "webhook_auth_mode": "github_signature",
        },
        "registry": {
            "enabled": False,
            "required_for_current_executor": False,
            "configured": True,
            "missing_settings": [],
        },
        "kubernetes": {
            "selected": False,
            "namespace": "default",
            "kubeconfig_configured": False,
            "image_pull_secret_configured": False,
            "deployment_prereqs_ready": True,
            "missing_deployment_prereqs": [],
        },
    }


def test_platform_health_check_reports_local_docker_contract_state(client, app):
    app.config["CONTROL_PLANE_ENV"] = "production"
    app.config["CONTROL_PLANE_EXECUTOR"] = "local-docker"
    app.config["CONTROL_PLANE_REGISTRY_ENABLED"] = False
    app.config["CONTROL_PLANE_REGISTRY_URL"] = ""
    app.config["CONTROL_PLANE_REGISTRY_NAMESPACE"] = ""

    response = client.get("/health/platform")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok",
        "environment": "production",
        "executor": "local-docker",
        "executor_contract": {
            "name": "local-docker",
            "deploy_target": "local-docker",
            "runtime": "container",
            "healthcheck_strategy": "direct-http",
            "requires_registry_push": False,
            "supports_runtime_logs": True,
            "supports_runtime_reconciliation": True,
            "managed_resources": [
                "docker-image",
                "docker-container",
            ],
            "required_config": [],
            "optional_config": [
                "CONTROL_PLANE_REGISTRY_ENABLED",
                "CONTROL_PLANE_REGISTRY_URL",
                "CONTROL_PLANE_REGISTRY_NAMESPACE",
                "CONTROL_PLANE_REGISTRY_USERNAME",
                "CONTROL_PLANE_REGISTRY_PASSWORD",
                "CONTROL_PLANE_DEPLOY_HOST",
                "CONTROL_PLANE_HEALTHCHECK_TIMEOUT_SECONDS",
                "CONTROL_PLANE_HEALTHCHECK_INTERVAL_SECONDS",
                "CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS",
            ],
        },
        "local_repo_paths_allowed": False,
        "deployment_creation_ready": True,
        "deployment_creation_error": None,
        "api_auth": {
            "enabled": False,
            "configured_roles": [],
            "token_transport": "bearer",
            "public_routes": ["/health"],
            "protected_health_routes": [
                "/health/db",
                "/health/platform",
                "/health/activity",
            ],
            "webhook_auth_mode": "github_signature",
        },
        "registry": {
            "enabled": False,
            "required_for_current_executor": False,
            "configured": False,
            "missing_settings": [
                "CONTROL_PLANE_REGISTRY_URL",
                "CONTROL_PLANE_REGISTRY_NAMESPACE",
            ],
        },
        "kubernetes": {
            "selected": False,
            "namespace": "default",
            "kubeconfig_configured": False,
            "image_pull_secret_configured": False,
            "deployment_prereqs_ready": True,
            "missing_deployment_prereqs": [],
        },
    }


def test_platform_health_check_reports_kubernetes_prereq_gaps(client, app):
    app.config["CONTROL_PLANE_ENV"] = "production"
    app.config["CONTROL_PLANE_EXECUTOR"] = "kubernetes"
    app.config["CONTROL_PLANE_REGISTRY_ENABLED"] = False
    app.config["CONTROL_PLANE_REGISTRY_URL"] = ""
    app.config["CONTROL_PLANE_REGISTRY_NAMESPACE"] = ""
    app.config["CONTROL_PLANE_KUBECONFIG"] = ""

    response = client.get("/health/platform")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "degraded",
        "environment": "production",
        "executor": "kubernetes",
        "executor_contract": {
            "name": "kubernetes",
            "deploy_target": "kubernetes",
            "runtime": "kubernetes",
            "healthcheck_strategy": "service-port-forward",
            "requires_registry_push": True,
            "supports_runtime_logs": True,
            "supports_runtime_reconciliation": True,
            "managed_resources": [
                "docker-image",
                "kubernetes-deployment",
                "kubernetes-service",
            ],
            "required_config": [
                "CONTROL_PLANE_REGISTRY_ENABLED=true",
                "CONTROL_PLANE_REGISTRY_URL",
                "CONTROL_PLANE_REGISTRY_NAMESPACE",
                "CONTROL_PLANE_KUBECONFIG",
            ],
            "optional_config": [
                "CONTROL_PLANE_K8S_NAMESPACE",
                "CONTROL_PLANE_K8S_IMAGE_PULL_SECRET",
                "CONTROL_PLANE_K8S_DEPLOYMENT_MODE",
                "CONTROL_PLANE_REGISTRY_USERNAME",
                "CONTROL_PLANE_REGISTRY_PASSWORD",
                "CONTROL_PLANE_HEALTHCHECK_TIMEOUT_SECONDS",
                "CONTROL_PLANE_HEALTHCHECK_INTERVAL_SECONDS",
                "CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS",
            ],
        },
        "local_repo_paths_allowed": False,
        "deployment_creation_ready": False,
        "deployment_creation_error": (
            "Kubernetes executor is not ready for deployments. Missing required settings: "
            "CONTROL_PLANE_REGISTRY_ENABLED=true, CONTROL_PLANE_REGISTRY_URL, "
            "CONTROL_PLANE_REGISTRY_NAMESPACE, CONTROL_PLANE_KUBECONFIG"
        ),
        "api_auth": {
            "enabled": False,
            "configured_roles": [],
            "token_transport": "bearer",
            "public_routes": ["/health"],
            "protected_health_routes": [
                "/health/db",
                "/health/platform",
                "/health/activity",
            ],
            "webhook_auth_mode": "github_signature",
        },
        "registry": {
            "enabled": False,
            "required_for_current_executor": True,
            "configured": False,
            "missing_settings": [
                "CONTROL_PLANE_REGISTRY_URL",
                "CONTROL_PLANE_REGISTRY_NAMESPACE",
            ],
        },
        "kubernetes": {
            "selected": True,
            "namespace": "default",
            "kubeconfig_configured": False,
            "image_pull_secret_configured": False,
            "deployment_prereqs_ready": False,
            "missing_deployment_prereqs": [
                "CONTROL_PLANE_REGISTRY_ENABLED=true",
                "CONTROL_PLANE_REGISTRY_URL",
                "CONTROL_PLANE_REGISTRY_NAMESPACE",
                "CONTROL_PLANE_KUBECONFIG",
            ],
        },
    }


def test_platform_health_check_reports_kubernetes_ready_state(client, app):
    app.config["CONTROL_PLANE_ENV"] = "production"
    app.config["CONTROL_PLANE_EXECUTOR"] = "kubernetes"
    app.config["CONTROL_PLANE_REGISTRY_ENABLED"] = True
    app.config["CONTROL_PLANE_REGISTRY_URL"] = "registry.example.com"
    app.config["CONTROL_PLANE_REGISTRY_NAMESPACE"] = "paas"
    app.config["CONTROL_PLANE_KUBECONFIG"] = "/tmp/kubeconfig"
    app.config["CONTROL_PLANE_K8S_NAMESPACE"] = "apps"
    app.config["CONTROL_PLANE_K8S_IMAGE_PULL_SECRET"] = "regcred"

    response = client.get("/health/platform")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["deployment_creation_ready"] is True
    assert payload["deployment_creation_error"] is None
    assert payload["executor"] == "kubernetes"
    assert payload["api_auth"] == {
        "enabled": False,
        "configured_roles": [],
        "token_transport": "bearer",
        "public_routes": ["/health"],
        "protected_health_routes": [
            "/health/db",
            "/health/platform",
            "/health/activity",
        ],
        "webhook_auth_mode": "github_signature",
    }
    assert payload["executor_contract"] == {
        "name": "kubernetes",
        "deploy_target": "kubernetes",
        "runtime": "kubernetes",
        "healthcheck_strategy": "service-port-forward",
        "requires_registry_push": True,
        "supports_runtime_logs": True,
        "supports_runtime_reconciliation": True,
        "managed_resources": [
            "docker-image",
            "kubernetes-deployment",
            "kubernetes-service",
        ],
        "required_config": [
            "CONTROL_PLANE_REGISTRY_ENABLED=true",
            "CONTROL_PLANE_REGISTRY_URL",
            "CONTROL_PLANE_REGISTRY_NAMESPACE",
            "CONTROL_PLANE_KUBECONFIG",
        ],
        "optional_config": [
            "CONTROL_PLANE_K8S_NAMESPACE",
            "CONTROL_PLANE_K8S_IMAGE_PULL_SECRET",
            "CONTROL_PLANE_K8S_DEPLOYMENT_MODE",
            "CONTROL_PLANE_REGISTRY_USERNAME",
            "CONTROL_PLANE_REGISTRY_PASSWORD",
            "CONTROL_PLANE_HEALTHCHECK_TIMEOUT_SECONDS",
            "CONTROL_PLANE_HEALTHCHECK_INTERVAL_SECONDS",
            "CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS",
        ],
    }
    assert payload["local_repo_paths_allowed"] is False
    assert payload["registry"] == {
        "enabled": True,
        "required_for_current_executor": True,
        "configured": True,
        "missing_settings": [],
    }
    assert payload["kubernetes"] == {
        "selected": True,
        "namespace": "apps",
        "kubeconfig_configured": True,
        "image_pull_secret_configured": True,
        "deployment_prereqs_ready": True,
        "missing_deployment_prereqs": [],
    }


def test_platform_health_check_reports_api_auth_posture(client, app):
    app.config["CONTROL_PLANE_API_TOKEN_READ_ONLY"] = "read-token"
    app.config["CONTROL_PLANE_API_TOKEN_DEPLOYER"] = "deployer-token"
    app.config["CONTROL_PLANE_API_TOKEN_ADMIN"] = "admin-token"
    app.config["CONTROL_PLANE_API_TOKENS_JSON"] = json.dumps(
        [{"name": "ops-admin", "role": "admin", "token": "json-admin-token"}]
    )

    response = client.get("/health/platform", headers={"Authorization": "Bearer read-token"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["api_auth"] == {
        "enabled": True,
        "configured_roles": ["read_only", "deployer", "admin"],
        "token_transport": "bearer",
        "public_routes": ["/health"],
        "protected_health_routes": [
            "/health/db",
            "/health/platform",
            "/health/activity",
        ],
        "webhook_auth_mode": "github_signature",
    }
    assert "read-token" not in str(payload)
    assert "deployer-token" not in str(payload)
    assert "admin-token" not in str(payload)
    assert "json-admin-token" not in str(payload)
    assert "ops-admin" not in str(payload)


def test_platform_activity_health_check_reports_recent_and_active_deployments(client, app):
    first_project_id = create_project(client, name="activity-first").get_json()["id"]
    second_project_id = create_project(client, name="activity-second").get_json()["id"]

    first_pending = client.post(
        f"/api/projects/{first_project_id}/deployments",
        json={"commit_sha": "1111111111111111"},
    )
    second_running = client.post(
        f"/api/projects/{second_project_id}/deployments",
        json={"commit_sha": "2222222222222222"},
    )
    stopped = client.post(
        f"/api/projects/{first_project_id}/deployments",
        json={"commit_sha": "3333333333333333"},
    )

    with app.app_context():
        running_deployment = db.session.get(PlatformDeployment, second_running.get_json()["id"])
        running_deployment.status = "running"
        stopped_deployment = db.session.get(PlatformDeployment, stopped.get_json()["id"])
        stopped_deployment.status = "stopped"
        stopped_deployment.last_error = "Manually stopped"
        db.session.commit()

    response = client.get("/health/activity")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert [item["deployment_id"] for item in payload["latest_deployments"]] == [
        stopped.get_json()["id"],
        second_running.get_json()["id"],
        first_pending.get_json()["id"],
    ]
    assert [item["status"] for item in payload["latest_deployments"]] == ["stopped", "running", "pending"]
    assert [item["project_name"] for item in payload["active_deployments"]] == ["activity-second", "activity-first"]
    assert [item["status"] for item in payload["active_deployments"]] == ["running", "pending"]
    assert payload["active_deployments"][0]["commit_sha"] == "2222222222222222"
    assert payload["latest_deployments"][0]["last_error"] == "Manually stopped"
    assert payload["latest_deployment_at"] is not None
    assert payload["active_deployment_count"] == 2
    assert payload["failed_deployment_count"] == 0
    assert payload["recent_webhook_delivery_count"] == 0
    assert payload["next_before_deployment_id"] == first_pending.get_json()["id"]
    assert payload["next_before_webhook_delivery_id"] is None
    assert payload["pagination"]["latest_limit"] == 10
    assert payload["pagination"]["next_before_deployment_id"] == first_pending.get_json()["id"]
    assert payload["failed_deployments"] == []
    assert payload["ignored_webhook_deliveries"] == []
    assert payload["accepted_webhook_deliveries"] == []


def test_platform_activity_health_check_reports_failed_deployments_and_ignored_webhooks(client, app):
    project_id = create_project(
        client,
        name="activity-failure-app",
        repo_url="https://github.com/example/activity-failure-app",
        trigger="github_push",
    ).get_json()["id"]
    failed_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "4444444444444444"},
    )
    older_failed_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "5555555555555555"},
    )

    with app.app_context():
        failed_deployment = db.session.get(PlatformDeployment, failed_response.get_json()["id"])
        failed_deployment.status = "failed"
        failed_deployment.last_error = "Healthcheck failed"
        failed_deployment.build.status = "failed"
        failed_deployment.build.last_error = "curl timeout"

        older_failed_deployment = db.session.get(PlatformDeployment, older_failed_response.get_json()["id"])
        older_failed_deployment.status = "failed"
        older_failed_deployment.build.status = "failed"
        older_failed_deployment.build.last_error = "docker build failed"
        db.session.commit()

    unmatched_payload = {
        "ref": "refs/heads/main",
        "after": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "repository": {
            "clone_url": "https://github.com/example/unmatched-webhook-app.git",
        },
    }
    unmatched_body, unmatched_headers = github_headers(app, unmatched_payload, delivery_id="ignored-unmatched")
    unmatched_response = client.post("/api/webhooks/github", data=unmatched_body, headers=unmatched_headers)

    branch_payload = {
        "ref": "refs/heads/release",
        "after": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "repository": {
            "clone_url": "https://github.com/example/activity-failure-app.git",
        },
    }
    branch_body, branch_headers = github_headers(app, branch_payload, delivery_id="ignored-branch")
    branch_response = client.post("/api/webhooks/github", data=branch_body, headers=branch_headers)

    assert unmatched_response.status_code == 202
    assert branch_response.status_code == 202

    response = client.get("/health/activity")
    payload = response.get_json()

    assert response.status_code == 200
    assert [item["status"] for item in payload["failed_deployments"]] == ["failed", "failed"]
    assert payload["failed_deployments"][0]["deployment_id"] == older_failed_response.get_json()["id"]
    assert payload["failed_deployments"][0]["last_error"] == "docker build failed"
    assert payload["failed_deployments"][1]["deployment_id"] == failed_response.get_json()["id"]
    assert payload["failed_deployments"][1]["last_error"] == "Healthcheck failed"
    assert [item["delivery_id"] for item in payload["ignored_webhook_deliveries"]] == [
        "ignored-branch",
        "ignored-unmatched",
    ]
    assert [item["reason"] for item in payload["ignored_webhook_deliveries"]] == [
        "branch_mismatch",
        "unmatched_repository",
    ]
    assert payload["failed_deployment_count"] == 2
    assert payload["recent_webhook_delivery_count"] == 2
    assert payload["next_before_webhook_delivery_id"] is not None
    assert payload["pagination"]["next_before_webhook_delivery_id"] == payload["next_before_webhook_delivery_id"]
    assert payload["accepted_webhook_deliveries"] == []


def test_platform_activity_health_check_reports_recent_accepted_webhook_deliveries(client, app):
    create_project(
        client,
        name="activity-accepted-app",
        repo_url="https://github.com/example/activity-accepted-app",
        trigger="github_push",
    )
    payload = {
        "ref": "refs/heads/main",
        "after": "cccccccccccccccccccccccccccccccccccccccc",
        "repository": {
            "clone_url": "https://github.com/example/activity-accepted-app.git",
        },
    }
    body, headers = github_headers(app, payload, delivery_id="accepted-push")

    response = client.post("/api/webhooks/github", data=body, headers=headers)
    activity_response = client.get("/health/activity")
    activity_payload = activity_response.get_json()

    assert response.status_code == 202
    assert activity_response.status_code == 200
    assert [item["delivery_id"] for item in activity_payload["accepted_webhook_deliveries"]] == ["accepted-push"]
    assert activity_payload["accepted_webhook_deliveries"][0]["status"] == "accepted"
    assert activity_payload["accepted_webhook_deliveries"][0]["deployment_id"] is not None


def test_platform_activity_health_check_supports_limit_filters(client, app):
    first_project_id = create_project(client, name="limit-first").get_json()["id"]
    second_project_id = create_project(client, name="limit-second").get_json()["id"]

    client.post(f"/api/projects/{first_project_id}/deployments", json={"commit_sha": "1010101010101010"})
    client.post(
        f"/api/projects/{second_project_id}/deployments",
        json={"commit_sha": "2020202020202020"},
    )
    failed_response = client.post(f"/api/projects/{first_project_id}/deployments", json={"commit_sha": "3030303030303030"})

    with app.app_context():
        failed_deployment = db.session.get(PlatformDeployment, failed_response.get_json()["id"])
        failed_deployment.status = "failed"
        failed_deployment.last_error = "limit failure"
        db.session.commit()

    create_project(
        client,
        name="limit-webhook-app",
        repo_url="https://github.com/example/limit-webhook-app",
        trigger="github_push",
    )
    accepted_payload = {
        "ref": "refs/heads/main",
        "after": "dddddddddddddddddddddddddddddddddddddddd",
        "repository": {
            "clone_url": "https://github.com/example/limit-webhook-app.git",
        },
    }
    accepted_body, accepted_headers = github_headers(app, accepted_payload, delivery_id="accepted-limit")
    accepted_response = client.post("/api/webhooks/github", data=accepted_body, headers=accepted_headers)

    ignored_payload = {
        "ref": "refs/heads/main",
        "after": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "repository": {
            "clone_url": "https://github.com/example/unmatched-limit-app.git",
        },
    }
    ignored_body, ignored_headers = github_headers(app, ignored_payload, delivery_id="ignored-limit")
    client.post("/api/webhooks/github", data=ignored_body, headers=ignored_headers)

    response = client.get(
        "/health/activity"
        "?latest_limit=1&active_limit=1&failed_limit=1&ignored_webhook_limit=1&accepted_webhook_limit=1"
    )
    payload = response.get_json()

    assert response.status_code == 200
    accepted_deployment_id = accepted_response.get_json()["deployments"][0]["deployment_id"]
    assert [item["deployment_id"] for item in payload["latest_deployments"]] == [accepted_deployment_id]
    assert [item["deployment_id"] for item in payload["active_deployments"]] == [accepted_deployment_id]
    assert [item["deployment_id"] for item in payload["failed_deployments"]] == [failed_response.get_json()["id"]]
    assert [item["delivery_id"] for item in payload["ignored_webhook_deliveries"]] == ["ignored-limit"]
    assert [item["delivery_id"] for item in payload["accepted_webhook_deliveries"]] == ["accepted-limit"]


def test_platform_activity_health_check_supports_status_filters(client, app):
    first_project_id = create_project(client, name="status-first").get_json()["id"]
    second_project_id = create_project(client, name="status-second").get_json()["id"]

    client.post(
        f"/api/projects/{first_project_id}/deployments",
        json={"commit_sha": "6161616161616161"},
    )
    running_response = client.post(
        f"/api/projects/{second_project_id}/deployments",
        json={"commit_sha": "6262626262626262"},
    )
    failed_response = client.post(
        f"/api/projects/{first_project_id}/deployments",
        json={"commit_sha": "6363636363636363"},
    )

    with app.app_context():
        running_deployment = db.session.get(PlatformDeployment, running_response.get_json()["id"])
        running_deployment.status = "running"
        failed_deployment = db.session.get(PlatformDeployment, failed_response.get_json()["id"])
        failed_deployment.status = "failed"
        failed_deployment.last_error = "status-filter failure"
        db.session.commit()

    create_project(
        client,
        name="status-webhook-app",
        repo_url="https://github.com/example/status-webhook-app",
        trigger="github_push",
    )
    accepted_payload = {
        "ref": "refs/heads/main",
        "after": "ffffffffffffffffffffffffffffffffffffffff",
        "repository": {
            "clone_url": "https://github.com/example/status-webhook-app.git",
        },
    }
    accepted_body, accepted_headers = github_headers(app, accepted_payload, delivery_id="status-accepted")
    client.post("/api/webhooks/github", data=accepted_body, headers=accepted_headers)

    ignored_payload = {
        "ref": "refs/heads/main",
        "after": "abababababababababababababababababababab",
        "repository": {
            "clone_url": "https://github.com/example/unmatched-status-webhook-app.git",
        },
    }
    ignored_body, ignored_headers = github_headers(app, ignored_payload, delivery_id="status-ignored")
    client.post("/api/webhooks/github", data=ignored_body, headers=ignored_headers)

    response = client.get("/health/activity?deployment_status=failed,running&webhook_status=ignored")
    payload = response.get_json()

    assert response.status_code == 200
    assert [item["status"] for item in payload["latest_deployments"]] == ["failed", "running"]
    assert [item["status"] for item in payload["active_deployments"]] == ["running"]
    assert [item["status"] for item in payload["failed_deployments"]] == ["failed"]
    assert [item["delivery_id"] for item in payload["ignored_webhook_deliveries"]] == ["status-ignored"]
    assert payload["accepted_webhook_deliveries"] == []


def test_platform_activity_health_check_supports_project_id_filter(client, app):
    first_project_id = create_project(
        client,
        name="project-filter-first",
        repo_url="https://github.com/example/project-filter-first",
        trigger="github_push",
    ).get_json()["id"]
    second_project_id = create_project(
        client,
        name="project-filter-second",
        repo_url="https://github.com/example/project-filter-second",
        trigger="github_push",
    ).get_json()["id"]

    first_deployment = client.post(
        f"/api/projects/{first_project_id}/deployments",
        json={"commit_sha": "7171717171717171"},
    )
    client.post(
        f"/api/projects/{second_project_id}/deployments",
        json={"commit_sha": "7272727272727272"},
    )

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, first_deployment.get_json()["id"])
        deployment.status = "running"
        db.session.commit()

    first_payload = {
        "ref": "refs/heads/main",
        "after": "1111111111111111111111111111111111111111",
        "repository": {
            "clone_url": "https://github.com/example/project-filter-first.git",
        },
    }
    first_body, first_headers = github_headers(app, first_payload, delivery_id="project-filter-first-accepted")
    client.post("/api/webhooks/github", data=first_body, headers=first_headers)

    second_payload = {
        "ref": "refs/heads/main",
        "after": "2222222222222222222222222222222222222222",
        "repository": {
            "clone_url": "https://github.com/example/project-filter-second.git",
        },
    }
    second_body, second_headers = github_headers(app, second_payload, delivery_id="project-filter-second-accepted")
    client.post("/api/webhooks/github", data=second_body, headers=second_headers)

    response = client.get(f"/health/activity?project_id={first_project_id}")
    payload = response.get_json()

    assert response.status_code == 200
    assert all(item["project_id"] == first_project_id for item in payload["latest_deployments"])
    assert all(item["project_id"] == first_project_id for item in payload["active_deployments"])
    assert [item["delivery_id"] for item in payload["accepted_webhook_deliveries"]] == [
        "project-filter-first-accepted"
    ]
    assert payload["project"]["id"] == first_project_id
    assert payload["pagination"]["latest_limit"] == 10
    assert payload["ignored_webhook_deliveries"] == []


def test_platform_activity_health_check_supports_before_id_pagination(client, app):
    project_id = create_project(client, name="before-id-project").get_json()["id"]
    first = client.post(f"/api/projects/{project_id}/deployments", json={"commit_sha": "8181818181818181"})
    second = client.post(f"/api/projects/{project_id}/deployments", json={"commit_sha": "8282828282828282"})
    third = client.post(f"/api/projects/{project_id}/deployments", json={"commit_sha": "8383838383838383"})

    response = client.get(f"/health/activity?latest_limit=2&before_deployment_id={third.get_json()['id']}")
    payload = response.get_json()

    assert response.status_code == 200
    assert [item["deployment_id"] for item in payload["latest_deployments"]] == [
        second.get_json()["id"],
        first.get_json()["id"],
    ]
    assert payload["pagination"]["next_before_deployment_id"] == first.get_json()["id"]


def test_platform_activity_health_check_rejects_invalid_project_id_filter(client):
    response = client.get("/health/activity?project_id=abc")

    assert response.status_code == 400
    assert response.get_json() == {"error": "project_id must be an integer greater than or equal to 1"}


def test_platform_activity_health_check_rejects_invalid_limit_filters(client):
    response = client.get("/health/activity?latest_limit=0")

    assert response.status_code == 400
    assert response.get_json() == {"error": "latest_limit must be an integer between 1 and 100"}


def test_platform_activity_health_check_rejects_invalid_status_filters(client):
    response = client.get("/health/activity?deployment_status=running,wat")

    assert response.status_code == 400
    assert response.get_json() == {
        "error": (
            "deployment_status contains invalid values: wat. Allowed: "
            "building, cloning, deploying, failed, pending, pushing_image, running, stopped, testing"
        )
    }


def test_platform_activity_health_check_rejects_invalid_webhook_status_filters(client):
    response = client.get("/health/activity?webhook_status=queued")

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "webhook_status contains invalid values: queued. Allowed: accepted, ignored"
    }


def test_platform_activity_health_check_returns_empty_lists_without_deployments(client):
    response = client.get("/health/activity")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok",
        "project": None,
        "latest_deployment_at": None,
        "active_deployment_count": 0,
        "failed_deployment_count": 0,
        "recent_webhook_delivery_count": 0,
        "next_before_deployment_id": None,
        "next_before_webhook_delivery_id": None,
        "pagination": {
            "latest_limit": 10,
            "active_limit": 10,
            "failed_limit": 10,
            "ignored_webhook_limit": 10,
            "accepted_webhook_limit": 10,
            "next_before_deployment_id": None,
            "next_before_webhook_delivery_id": None,
        },
        "latest_deployments": [],
        "active_deployments": [],
        "failed_deployments": [],
        "ignored_webhook_deliveries": [],
        "accepted_webhook_deliveries": [],
    }
