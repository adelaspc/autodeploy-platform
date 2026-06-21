def test_create_and_list_deployments(client):
    create_response = client.post(
        "/api/deployments",
        json={
            "application_name": "billing-api",
            "version": "2026.04.27-1",
            "environment": "staging",
            "status": "pending",
        },
    )

    assert create_response.status_code == 201
    created = create_response.get_json()
    assert created["application_name"] == "billing-api"
    assert created["status"] == "pending"
    assert created["allowed_transitions"] == ["building", "failed"]

    list_response = client.get("/api/deployments")

    assert list_response.status_code == 200
    payload = list_response.get_json()
    assert payload["total"] == 1
    assert payload["page"] == 1
    assert len(payload["items"]) == 1
    assert payload["items"][0]["version"] == "2026.04.27-1"


def test_list_deployments_supports_filters(client):
    client.post(
        "/api/deployments",
        json={
            "application_name": "billing-api",
            "version": "2026.04.27-1",
            "environment": "staging",
            "status": "pending",
        },
    )
    client.post(
        "/api/deployments",
        json={
            "application_name": "auth-worker",
            "version": "2026.04.27-2",
            "environment": "production",
            "status": "failed",
        },
    )

    by_environment = client.get("/api/deployments?environment=production")
    assert by_environment.status_code == 200
    environment_payload = by_environment.get_json()
    assert len(environment_payload["items"]) == 1
    assert environment_payload["items"][0]["application_name"] == "auth-worker"

    by_status = client.get("/api/deployments?status=pending")
    assert by_status.status_code == 200
    status_payload = by_status.get_json()
    assert len(status_payload["items"]) == 1
    assert status_payload["items"][0]["application_name"] == "billing-api"

    by_application = client.get("/api/deployments?application_name=auth")
    assert by_application.status_code == 200
    application_payload = by_application.get_json()
    assert len(application_payload["items"]) == 1
    assert application_payload["items"][0]["environment"] == "production"


def test_list_deployments_supports_pagination(client):
    for index in range(12):
        response = client.post(
            "/api/deployments",
            json={
                "application_name": f"service-{index}",
                "version": f"2026.04.27-{index}",
                "environment": "staging",
                "status": "pending",
            },
        )
        assert response.status_code == 201

    first_page = client.get("/api/deployments?page=1&per_page=5")
    assert first_page.status_code == 200
    first_payload = first_page.get_json()
    assert first_payload["page"] == 1
    assert first_payload["per_page"] == 5
    assert first_payload["total"] == 12
    assert first_payload["pages"] == 3
    assert first_payload["has_next"] is True
    assert first_payload["has_prev"] is False
    assert len(first_payload["items"]) == 5

    third_page = client.get("/api/deployments?page=3&per_page=5")
    assert third_page.status_code == 200
    third_payload = third_page.get_json()
    assert third_payload["page"] == 3
    assert third_payload["has_next"] is False
    assert third_payload["has_prev"] is True
    assert len(third_payload["items"]) == 2


def test_create_deployment_rejects_invalid_status(client):
    response = client.post(
        "/api/deployments",
        json={
            "application_name": "billing-api",
            "version": "2026.04.27-1",
            "environment": "staging",
            "status": "done",
        },
    )

    assert response.status_code == 400
    assert "Invalid status" in response.get_json()["error"]


def test_patch_deployment_status(client):
    create_response = client.post(
        "/api/deployments",
        json={
            "application_name": "billing-api",
            "version": "2026.04.27-1",
            "environment": "staging",
            "status": "pending",
        },
    )
    deployment_id = create_response.get_json()["id"]

    patch_response = client.patch(
        f"/api/deployments/{deployment_id}",
        json={"status": "building"},
    )

    assert patch_response.status_code == 200
    updated = patch_response.get_json()
    assert updated["status"] == "building"
    assert updated["allowed_transitions"] == ["deployed", "failed"]


def test_patch_deployment_rejects_invalid_transition(client):
    create_response = client.post(
        "/api/deployments",
        json={
            "application_name": "billing-api",
            "version": "2026.04.27-1",
            "environment": "staging",
            "status": "pending",
        },
    )
    deployment_id = create_response.get_json()["id"]

    patch_response = client.patch(
        f"/api/deployments/{deployment_id}",
        json={"status": "deployed"},
    )

    assert patch_response.status_code == 409
    assert "Invalid status transition" in patch_response.get_json()["error"]


def test_delete_deployment(client):
    create_response = client.post(
        "/api/deployments",
        json={
            "application_name": "billing-api",
            "version": "2026.04.27-1",
            "environment": "staging",
            "status": "pending",
        },
    )
    deployment_id = create_response.get_json()["id"]

    delete_response = client.delete(f"/api/deployments/{deployment_id}")

    assert delete_response.status_code == 200
    assert delete_response.get_json() == {"message": "Deployment deleted"}

    list_response = client.get("/api/deployments")
    payload = list_response.get_json()
    assert payload["total"] == 0
    assert payload["items"] == []
