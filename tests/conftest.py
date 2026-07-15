from pathlib import Path
import sys

import pytest
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control_plane import create_app
from control_plane.extensions import db


class TestConfig:
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "connect_args": {"check_same_thread": False},
        "poolclass": StaticPool,
    }
    CONTROL_PLANE_ENV = "development"
    CONTROL_PLANE_ALLOW_AUTH_DISABLED = True
    CONTROL_PLANE_REGISTRY_ENABLED = False
    CONTROL_PLANE_GITHUB_WEBHOOK_SECRET = "test-github-webhook-secret"
    CONTROL_PLANE_REGISTRY_URL = "registry.example.com"
    CONTROL_PLANE_REGISTRY_NAMESPACE = "paas"
    CONTROL_PLANE_REGISTRY_USERNAME = None
    CONTROL_PLANE_REGISTRY_PASSWORD = None
    CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS = 0


@pytest.fixture
def app():
    app = create_app(TestConfig)

    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()
