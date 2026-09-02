import hashlib
import hmac
import json

from control_plane.models import PlatformDeployment, WebhookDelivery


def create_project(client, **overrides):
    payload = {
        "name": "webhook-app",
        "repo_url": "https://github.com/example/webhook-app",
        "branch": "main",
        "dockerfile_path": "Dockerfile",
        "build_context": ".",
        "port": 5000,
        "healthcheck_path": "/health",
        "env_vars": [],
        "trigger": "github_push",
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


def test_github_webhook_rejects_invalid_signature(client):
    payload = {"zen": "keep it logically awesome"}

    response = client.post(
        "/api/webhooks/github",
        data=json.dumps(payload),
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "ping",
            "X-Hub-Signature-256": "sha256=invalid",
        },
    )

    assert response.status_code == 401
    payload = response.get_json()
    assert payload["error"] == "Invalid GitHub webhook signature"
    assert payload["request_id"] == response.headers["X-Request-ID"]


def test_github_webhook_handles_ping_event(client, app):
    payload = {"zen": "keep it logically awesome"}
    body, headers = github_headers(app, payload, event="ping", delivery_id="ping-1")

    response = client.post("/api/webhooks/github", data=body, headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {
        "message": "GitHub webhook ping received",
        "event": "ping",
        "delivery_id": "ping-1",
    }


def test_github_webhook_triggers_deployment_for_matching_push_event(client, app):
    project_response = create_project(client, name="push-triggered-app")
    project_id = project_response.get_json()["id"]
    payload = {
        "ref": "refs/heads/main",
        "after": "0123456789abcdef0123456789abcdef01234567",
        "repository": {
            "clone_url": "https://github.com/example/webhook-app.git",
            "html_url": "https://github.com/example/webhook-app",
        },
    }
    body, headers = github_headers(app, payload, delivery_id="push-1")

    response = client.post("/api/webhooks/github", data=body, headers=headers)

    assert response.status_code == 202
    webhook_payload = response.get_json()
    assert webhook_payload["status"] == "accepted"
    assert webhook_payload["event"] == "push"
    assert webhook_payload["delivery_id"] == "push-1"
    assert webhook_payload["repository_url"] == "https://github.com/example/webhook-app.git"
    assert len(webhook_payload["deployments"]) == 1
    assert webhook_payload["deployments"][0]["project_id"] == project_id
    assert webhook_payload["deployments"][0]["preflight_status"] is None
    assert webhook_payload["deployments"][0]["preflight_summary"] is None
    assert webhook_payload["deployments"][0]["preflight_completed_at"] is None

    deployment_id = webhook_payload["deployments"][0]["deployment_id"]
    deployment_response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}")
    deployment = deployment_response.get_json()

    assert deployment["status"] == "pending"
    assert deployment["build"]["commit_sha"] == "0123456789abcdef0123456789abcdef01234567"
    assert deployment["build"]["test_command"] is None
    assert deployment["events"][0]["event_type"] == "deployment.created"
    assert deployment["events"][0]["metadata_json"]["trigger"] == "github_push"
    assert deployment["events"][0]["metadata_json"]["source"] == "github_webhook"
    assert deployment["events"][0]["metadata_json"]["github_delivery_id"] == "push-1"
    assert deployment["events"][1]["event_type"] == "webhook.github_push_received"
    assert deployment["events"][1]["metadata_json"] == {
        "event_type": "push",
        "branch": "main",
        "commit_sha": "0123456789abcdef0123456789abcdef01234567",
        "github_delivery_id": "push-1",
        "repository_url": "https://github.com/example/webhook-app.git",
    }

    with app.app_context():
        delivery = WebhookDelivery.query.filter_by(delivery_id="push-1").first()
        assert delivery is not None
        assert delivery.status == "accepted"
        assert delivery.reason is None
        assert delivery.branch == "main"
        assert delivery.commit_sha == "0123456789abcdef0123456789abcdef01234567"
        assert delivery.deployment_id == deployment_id


def test_github_webhook_uses_project_default_test_command(client, app):
    project_response = create_project(client, name="push-triggered-default-tests-app", default_test_command="pytest -q")
    project_id = project_response.get_json()["id"]
    payload = {
        "ref": "refs/heads/main",
        "after": "0123456789abcdef0123456789abcdef01234567",
        "repository": {
            "clone_url": "https://github.com/example/webhook-app.git",
        },
    }
    body, headers = github_headers(app, payload, delivery_id="push-default-test-command")

    response = client.post("/api/webhooks/github", data=body, headers=headers)

    assert response.status_code == 202
    deployment_id = response.get_json()["deployments"][0]["deployment_id"]
    deployment = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}").get_json()
    assert deployment["build"]["test_command"] == "pytest -q"


def test_github_webhook_ignores_unmatched_repository_safely(client, app):
    create_project(client, name="other-webhook-app", repo_url="https://github.com/example/other-webhook-app")
    payload = {
        "ref": "refs/heads/main",
        "after": "fedcba9876543210fedcba9876543210fedcba98",
        "repository": {
            "clone_url": "https://github.com/example/webhook-app.git",
        },
    }
    body, headers = github_headers(app, payload, delivery_id="push-2")

    response = client.post("/api/webhooks/github", data=body, headers=headers)

    assert response.status_code == 202
    assert response.get_json() == {
        "status": "ignored",
        "reason": "unmatched_repository",
        "event": "push",
        "delivery_id": "push-2",
        "repository_url": "https://github.com/example/webhook-app.git",
        "branch": "main",
        "commit_sha": "fedcba9876543210fedcba9876543210fedcba98",
    }

    with app.app_context():
        delivery = WebhookDelivery.query.filter_by(delivery_id="push-2").first()
        assert delivery is not None
        assert delivery.status == "ignored"
        assert delivery.reason == "unmatched_repository"
        assert delivery.deployment_id is None


def test_github_webhook_ignores_branch_mismatch(client, app):
    create_project(client, name="branch-app", branch="main")
    payload = {
        "ref": "refs/heads/release",
        "after": "1111111111111111111111111111111111111111",
        "repository": {
            "clone_url": "https://github.com/example/webhook-app.git",
        },
    }
    body, headers = github_headers(app, payload, delivery_id="push-branch")

    response = client.post("/api/webhooks/github", data=body, headers=headers)

    assert response.status_code == 202
    assert response.get_json() == {
        "status": "ignored",
        "reason": "branch_mismatch",
        "event": "push",
        "delivery_id": "push-branch",
        "repository_url": "https://github.com/example/webhook-app.git",
        "branch": "release",
        "commit_sha": "1111111111111111111111111111111111111111",
    }

    with app.app_context():
        delivery = WebhookDelivery.query.filter_by(delivery_id="push-branch").first()
        assert delivery is not None
        assert delivery.status == "ignored"
        assert delivery.reason == "branch_mismatch"


def test_github_webhook_ignores_deleted_branch(client, app):
    project_response = create_project(client, name="deleted-branch-app")
    project_id = project_response.get_json()["id"]
    zero_sha = "0" * 40
    payload = {
        "ref": "refs/heads/main",
        "after": zero_sha,
        "deleted": True,
        "repository": {
            "clone_url": "https://github.com/example/webhook-app.git",
        },
    }
    body, headers = github_headers(app, payload, delivery_id="push-branch-deleted")

    response = client.post("/api/webhooks/github", data=body, headers=headers)

    assert response.status_code == 202
    assert response.get_json() == {
        "status": "ignored",
        "reason": "branch_deleted",
        "event": "push",
        "delivery_id": "push-branch-deleted",
        "repository_url": "https://github.com/example/webhook-app.git",
        "branch": "main",
        "commit_sha": zero_sha,
    }
    assert client.get(f"/api/projects/{project_id}/deployments").get_json()["items"] == []

    with app.app_context():
        delivery = WebhookDelivery.query.filter_by(delivery_id="push-branch-deleted").first()
        assert delivery is not None
        assert delivery.status == "ignored"
        assert delivery.reason == "branch_deleted"
        assert delivery.deployment_id is None


def test_github_webhook_treats_zero_after_sha_as_deleted_branch(client, app):
    create_project(client, name="zero-sha-deleted-branch-app")
    payload = {
        "ref": "refs/heads/main",
        "after": "0" * 40,
        "repository": {
            "clone_url": "https://github.com/example/webhook-app.git",
        },
    }
    body, headers = github_headers(app, payload, delivery_id="push-zero-sha")

    response = client.post("/api/webhooks/github", data=body, headers=headers)

    assert response.status_code == 202
    assert response.get_json()["reason"] == "branch_deleted"


def test_github_webhook_ignores_push_when_kubernetes_executor_is_not_ready(client, app):
    create_project(client, name="k8s-webhook-prereq-app", default_test_command="pytest -q")
    with app.app_context():
        app.config["CONTROL_PLANE_EXECUTOR"] = "kubernetes"
        app.config["CONTROL_PLANE_REGISTRY_ENABLED"] = True
        app.config["CONTROL_PLANE_REGISTRY_URL"] = "docker.io"
        app.config["CONTROL_PLANE_REGISTRY_NAMESPACE"] = None
        app.config["CONTROL_PLANE_KUBECONFIG"] = "/tmp/kubeconfig"

    payload = {
        "ref": "refs/heads/main",
        "after": "1111111111111111111111111111111111111111",
        "repository": {
            "clone_url": "https://github.com/example/webhook-app.git",
        },
    }
    body, headers = github_headers(app, payload, delivery_id="push-k8s-not-ready")

    response = client.post("/api/webhooks/github", data=body, headers=headers)

    assert response.status_code == 202
    assert response.get_json() == {
        "status": "ignored",
        "reason": "platform_not_ready",
        "event": "push",
        "delivery_id": "push-k8s-not-ready",
        "repository_url": "https://github.com/example/webhook-app.git",
        "branch": "main",
        "commit_sha": "1111111111111111111111111111111111111111",
    }

    with app.app_context():
        delivery = WebhookDelivery.query.filter_by(delivery_id="push-k8s-not-ready").first()
        assert delivery is not None
        assert delivery.status == "ignored"
        assert delivery.reason == "platform_not_ready"


def test_github_webhook_ignores_unsupported_event_type(client, app):
    payload = {
        "repository": {
            "clone_url": "https://github.com/example/webhook-app.git",
        },
    }
    body, headers = github_headers(app, payload, event="issues", delivery_id="issues-1")

    response = client.post("/api/webhooks/github", data=body, headers=headers)

    assert response.status_code == 202
    assert response.get_json() == {
        "status": "ignored",
        "reason": "unsupported_event_type",
        "event": "issues",
        "delivery_id": "issues-1",
        "repository_url": "https://github.com/example/webhook-app.git",
        "branch": None,
        "commit_sha": None,
    }

    with app.app_context():
        delivery = WebhookDelivery.query.filter_by(delivery_id="issues-1").first()
        assert delivery is not None
        assert delivery.status == "ignored"
        assert delivery.reason == "unsupported_event_type"


def test_github_webhook_ignores_duplicate_delivery(client, app):
    project_response = create_project(client, name="duplicate-app")
    project_id = project_response.get_json()["id"]
    payload = {
        "ref": "refs/heads/main",
        "after": "2222222222222222222222222222222222222222",
        "repository": {
            "clone_url": "https://github.com/example/webhook-app.git",
        },
    }
    body, headers = github_headers(app, payload, delivery_id="duplicate-1")

    first_response = client.post("/api/webhooks/github", data=body, headers=headers)
    second_response = client.post("/api/webhooks/github", data=body, headers=headers)

    assert first_response.status_code == 202
    assert first_response.get_json()["status"] == "accepted"
    assert second_response.status_code == 202
    assert second_response.get_json() == {
        "status": "ignored",
        "reason": "duplicate_delivery",
        "event": "push",
        "delivery_id": "duplicate-1",
        "repository_url": "https://github.com/example/webhook-app.git",
        "branch": "main",
        "commit_sha": "2222222222222222222222222222222222222222",
    }

    with app.app_context():
        deployments = PlatformDeployment.query.filter_by(project_id=project_id).all()
        assert len(deployments) == 1
        deliveries = WebhookDelivery.query.filter_by(delivery_id="duplicate-1").all()
        assert len(deliveries) == 1
        assert deliveries[0].status == "accepted"


def test_github_webhook_allows_same_commit_with_different_delivery_ids(client, app):
    project_response = create_project(client, name="same-commit-app")
    project_id = project_response.get_json()["id"]
    payload = {
        "ref": "refs/heads/main",
        "after": "3333333333333333333333333333333333333333",
        "repository": {
            "clone_url": "https://github.com/example/webhook-app.git",
        },
    }

    first_body, first_headers = github_headers(app, payload, delivery_id="same-commit-1")
    second_body, second_headers = github_headers(app, payload, delivery_id="same-commit-2")

    first_response = client.post("/api/webhooks/github", data=first_body, headers=first_headers)
    second_response = client.post("/api/webhooks/github", data=second_body, headers=second_headers)

    assert first_response.status_code == 202
    assert first_response.get_json()["status"] == "accepted"
    assert second_response.status_code == 202
    assert second_response.get_json()["status"] == "accepted"

    with app.app_context():
        deployments = (
            PlatformDeployment.query.filter_by(project_id=project_id)
            .order_by(PlatformDeployment.id.asc())
            .all()
        )
        assert len(deployments) == 2
        assert all(deployment.build.commit_sha == "3333333333333333333333333333333333333333" for deployment in deployments)
        assert WebhookDelivery.query.filter(WebhookDelivery.delivery_id.in_(["same-commit-1", "same-commit-2"])).count() == 2


def test_github_webhook_accepts_valid_signature(client, app):
    payload = {
        "ref": "refs/heads/main",
        "after": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "repository": {
            "clone_url": "https://github.com/example/unmatched.git",
        },
    }
    body, headers = github_headers(app, payload, delivery_id="push-3")

    response = client.post("/api/webhooks/github", data=body, headers=headers)

    assert response.status_code != 401
