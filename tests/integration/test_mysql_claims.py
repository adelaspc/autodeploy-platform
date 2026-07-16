import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from control_plane import create_app
from control_plane.application.command_requests import request_deployment_command
from control_plane.extensions import db
from control_plane.models import Build, DeploymentCommand, PlatformDeployment, Project
from worker.processing.claims import claim_next_pending_deployment


pytestmark = pytest.mark.mysql


def mysql_test_app():
    database_url = os.getenv("CONTROL_PLANE_TEST_MYSQL_URL")
    if not database_url:
        pytest.skip("CONTROL_PLANE_TEST_MYSQL_URL is not configured")

    class MySQLTestConfig:
        TESTING = True
        SQLALCHEMY_DATABASE_URI = database_url
        SQLALCHEMY_TRACK_MODIFICATIONS = False
        CONTROL_PLANE_ENV = "test"
        CONTROL_PLANE_ALLOW_AUTH_DISABLED = True
        CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS = 0

    return create_app(MySQLTestConfig)


def create_pending_deployment(app):
    with app.app_context():
        db.drop_all()
        db.create_all()
        project = Project(
            name="concurrent-claims",
            repo_url="https://example.invalid/repo.git",
            branch="main",
            dockerfile_path="Dockerfile",
            build_context=".",
            port=8080,
            healthcheck_path="/health",
            env_vars=[],
            trigger="manual",
            runtime="dockerfile",
        )
        build = Build(project=project, commit_sha="abc123", status="pending")
        deployment = PlatformDeployment(project=project, build=build, status="pending", environment="production")
        db.session.add_all([project, build, deployment])
        db.session.commit()
        return deployment.id


def test_two_mysql_workers_cannot_claim_the_same_deployment():
    app = mysql_test_app()
    deployment_id = create_pending_deployment(app)

    def claim(worker_name):
        with app.app_context():
            claimed = claim_next_pending_deployment(worker_id=worker_name)
            return claimed.id if claimed else None

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, ("worker-a", "worker-b")))

        assert results.count(deployment_id) == 1
        assert results.count(None) == 1
    finally:
        with app.app_context():
            db.session.remove()
            db.drop_all()


def test_concurrent_mysql_stop_requests_are_deduplicated():
    app = mysql_test_app()
    deployment_id = create_pending_deployment(app)

    def request_stop(_request_number):
        with app.app_context():
            deployment = db.session.get(PlatformDeployment, deployment_id)
            command, created = request_deployment_command(deployment, "stop", message="Stop requested")
            db.session.commit()
            return command.id, created

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(request_stop, (1, 2)))

        assert len({command_id for command_id, _created in results}) == 1
        assert sum(created for _command_id, created in results) == 1
        with app.app_context():
            command_count = db.session.scalar(select(func.count()).select_from(DeploymentCommand))
            assert command_count == 1
    finally:
        with app.app_context():
            db.session.remove()
            db.drop_all()
