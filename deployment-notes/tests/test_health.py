def test_health_check(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_database_health_check(client):
    response = client.get("/health/db")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "database": "reachable"}
