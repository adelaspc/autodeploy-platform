import hashlib
import hmac
import json

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
        "env_vars": [{"name": "APP_ENV", "value_source": "literal", "value": "test", "is_secret": False}],
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


def event_by_type(events, event_type):
    return next(event for event in events if event["event_type"] == event_type)
