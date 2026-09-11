import hashlib
import hmac
import json

import pytest

from tests.api.project_test_helpers import create_project


EXPECTED_ERROR = {"error": "Request body must be a JSON object"}
NON_OBJECT_JSON_VALUES = ([], ["item"], "text", 42, True, False, None)


def post_json_value(client, path, value, *, method="post", headers=None):
    return client.open(
        path,
        method=method.upper(),
        data=json.dumps(value),
        content_type="application/json",
        headers=headers,
    )


@pytest.mark.parametrize("value", NON_OBJECT_JSON_VALUES)
def test_project_create_rejects_every_valid_non_object_json_body(client, value):
    response = post_json_value(client, "/api/projects", value)

    assert response.status_code == 400
    assert response.get_json() == EXPECTED_ERROR


def test_all_project_write_routes_reject_non_object_json_bodies(client):
    project = create_project(client, name="json-object-contract").get_json()
    deployment = client.post(
        f"/api/projects/{project['id']}/deployments",
        json={"commit_sha": "0123456789abcdef"},
    ).get_json()
    routes = (
        ("patch", f"/api/projects/{project['id']}"),
        ("post", f"/api/projects/{project['id']}/deployments"),
        ("post", f"/api/projects/{project['id']}/deploy"),
        ("post", f"/api/projects/{project['id']}/deployments/{deployment['id']}/stop"),
        ("post", f"/api/projects/{project['id']}/deployments/{deployment['id']}/cleanup"),
        ("patch", f"/api/projects/{project['id']}/deployments/{deployment['id']}"),
    )

    for method, path in routes:
        response = post_json_value(client, path, [], method=method)
        assert response.status_code == 400, path
        assert response.get_json() == EXPECTED_ERROR, path


def test_github_webhook_rejects_signed_non_object_json_body(client, app):
    body = json.dumps([]).encode("utf-8")
    secret = app.config["CONTROL_PLANE_GITHUB_WEBHOOK_SECRET"].encode("utf-8")
    signature = hmac.new(secret, body, hashlib.sha256).hexdigest()

    response = client.post(
        "/api/webhooks/github",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": "non-object-json",
            "X-Hub-Signature-256": f"sha256={signature}",
        },
    )

    assert response.status_code == 400
    assert response.get_json() == EXPECTED_ERROR
