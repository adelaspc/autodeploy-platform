from datetime import datetime, timezone

import pytest

from control_plane import create_app
from control_plane.extensions import db
from control_plane.models import Build, DeploymentEvent, PlatformDeployment, Project, WebhookDelivery
from tests.conftest import TestConfig


class MetricsConfig(TestConfig):
    CONTROL_PLANE_METRICS_ENABLED = True
    CONTROL_PLANE_METRICS_TOKEN = "metrics-test-token"


@pytest.fixture
def metrics_app():
    app = create_app(MetricsConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def metrics_client(metrics_app):
    return metrics_app.test_client()


def test_metrics_is_hidden_when_disabled(client):
    response = client.get("/metrics")

    assert response.status_code == 404


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer wrong-token", "Basic metrics-test-token", "Bearer"],
)
def test_metrics_requires_dedicated_bearer_token(metrics_client, authorization):
    headers = {"Authorization": authorization} if authorization else {}
    response = metrics_client.get("/metrics", headers=headers)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_metrics_configuration_fails_fast_without_token():
    class InvalidMetricsConfig(TestConfig):
        CONTROL_PLANE_METRICS_ENABLED = True
        CONTROL_PLANE_METRICS_TOKEN = ""

        @staticmethod
        def init_app(app):
            from control_plane.config import Config

            Config.init_app(app)

    with pytest.raises(RuntimeError, match="CONTROL_PLANE_METRICS_TOKEN"):
        create_app(InvalidMetricsConfig)


def test_non_api_component_does_not_require_metrics_token():
    class WorkerMetricsConfig(TestConfig):
        CONTROL_PLANE_COMPONENT = "worker"
        CONTROL_PLANE_METRICS_ENABLED = True
        CONTROL_PLANE_METRICS_TOKEN = ""

        @staticmethod
        def init_app(app):
            from control_plane.config import Config

            Config.init_app(app)

    app = create_app(WorkerMetricsConfig)

    assert app.config["CONTROL_PLANE_COMPONENT"] == "worker"


def test_metrics_exports_only_bounded_aggregate_labels(metrics_app, metrics_client):
    with metrics_app.app_context():
        project = Project(
            name="private-project",
            repo_url="https://github.com/example/private-project.git",
            branch="secret-branch",
            port=5000,
            healthcheck_path="/health",
        )
        db.session.add(project)
        db.session.flush()
        build = Build(project_id=project.id, commit_sha="abc123", status="failed", image_tag="sensitive-tag")
        db.session.add(build)
        db.session.flush()
        deployment = PlatformDeployment(
            project_id=project.id,
            build_id=build.id,
            status="failed",
            last_error="sensitive failure detail",
            updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        db.session.add(deployment)
        db.session.flush()
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment.id,
                event_type="deployment.failed",
                level="error",
                status="failed",
                message="sensitive event detail",
            )
        )
        db.session.add(
            WebhookDelivery(
                delivery_id="private-delivery-id",
                event_type="push",
                repository_url=project.repo_url,
                branch=project.branch,
                status="ignored",
            )
        )
        db.session.commit()

    response = metrics_client.get(
        "/metrics",
        headers={"Authorization": "Bearer metrics-test-token"},
    )

    assert response.status_code == 200
    assert response.content_type == "text/plain; version=0.0.4; charset=utf-8"
    assert response.headers["Cache-Control"] == "no-store"
    body = response.get_data(as_text=True)
    assert 'control_plane_deployments{status="failed"} 1' in body
    assert 'control_plane_builds{status="failed"} 1' in body
    assert 'control_plane_webhook_deliveries{result="ignored"} 1' in body
    assert 'control_plane_deployment_events{level="error"} 1' in body
    for sensitive_value in (
        "private-project",
        "private-project.git",
        "secret-branch",
        "sensitive-tag",
        "sensitive failure detail",
        "sensitive event detail",
        "private-delivery-id",
    ):
        assert sensitive_value not in body
    for forbidden_label in (
        "request_id",
        "project_id",
        "deployment_id",
        "app_name",
        "repo_url",
        "branch_name",
        "user_id",
        "image_tag",
        "error_message",
    ):
        assert f'{forbidden_label}="' not in body
