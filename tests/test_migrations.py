from flask_migrate import upgrade
from sqlalchemy import inspect, text

from control_plane import create_app
from control_plane.extensions import db


PREVIOUS_REVISION = "8a3d6e1f5c9b"


def test_origin_request_id_migration_upgrades_existing_rows(tmp_path):
    database_path = tmp_path / "migration.db"

    class MigrationConfig:
        TESTING = True
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path}"
        SQLALCHEMY_TRACK_MODIFICATIONS = False
        CONTROL_PLANE_ENV = "test"
        CONTROL_PLANE_ALLOW_AUTH_DISABLED = True
        CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS = 0

    app = create_app(MigrationConfig)
    now = "2026-01-01 00:00:00"

    with app.app_context():
        upgrade(directory="migrations", revision=PREVIOUS_REVISION)
        db.session.execute(
            text(
                "INSERT INTO projects "
                "(id, name, repo_url, branch, git_auth_type, dockerfile_path, build_context, port, "
                "healthcheck_path, env_vars, trigger, runtime, created_at, updated_at) "
                "VALUES (1, 'migration_project.withdot', 'https://example.invalid/repo.git', 'main', 'none', "
                "'Dockerfile', '.', 8080, '/health', '[]', 'manual', 'dockerfile', :now, :now)"
            ),
            {"now": now},
        )
        db.session.execute(
            text(
                "INSERT INTO builds "
                "(id, project_id, commit_sha, status, created_at, updated_at) "
                "VALUES (1, 1, 'abc123', 'pending', :now, :now)"
            ),
            {"now": now},
        )
        db.session.execute(
            text(
                "INSERT INTO builds "
                "(id, project_id, commit_sha, status, created_at, updated_at) "
                "VALUES (2, 1, 'def456', 'pending', :now, :now)"
            ),
            {"now": now},
        )
        db.session.execute(
            text(
                "INSERT INTO platform_deployments "
                "(id, project_id, build_id, environment, status, deploy_target, helm_release_name, "
                "helm_namespace, created_at, updated_at) "
                "VALUES (1, 1, 1, 'production', 'pending', 'kubernetes', 'migration-release', "
                "'original-apps', :now, :now)"
            ),
            {"now": now},
        )
        db.session.execute(
            text(
                "INSERT INTO platform_deployments "
                "(id, project_id, build_id, environment, status, deploy_target, created_at, updated_at) "
                "VALUES (2, 1, 2, 'production', 'failed', 'kubernetes', :now, :now)"
            ),
            {"now": now},
        )
        db.session.execute(
            text(
                "INSERT INTO deployment_events "
                "(id, deployment_id, event_type, status, level, metadata_json, created_at) "
                "VALUES (1, 2, 'kubernetes.manifest_apply_failed', 'failed', 'error', "
                "'{\"deployment_mode\": \"manifest\", \"namespace\": \"legacy-apps\"}', :now)"
            ),
            {"now": now},
        )
        db.session.execute(
            text(
                "INSERT INTO deployment_commands "
                "(id, deployment_id, command_type, status, active_key, requested_at) "
                "VALUES (1, 1, 'stop', 'pending', 'active', :now)"
            ),
            {"now": now},
        )
        db.session.commit()

        upgrade(directory="migrations")

        inspector = inspect(db.engine)
        deployment_columns = {column["name"] for column in inspector.get_columns("platform_deployments")}
        command_columns = {column["name"] for column in inspector.get_columns("deployment_commands")}
        deployment_indexes = {index["name"] for index in inspector.get_indexes("platform_deployments")}
        command_indexes = {index["name"] for index in inspector.get_indexes("deployment_commands")}

        assert "origin_request_id" in deployment_columns
        assert "kubernetes_deployment_mode" in deployment_columns
        assert "kubernetes_namespace" in deployment_columns
        assert "kubernetes_deployment_name" in deployment_columns
        assert "kubernetes_service_name" in deployment_columns
        assert "kubernetes_ingress_name" in deployment_columns
        assert "origin_request_id" in command_columns
        assert "ix_platform_deployments_origin_request_id" in deployment_indexes
        assert "ix_deployment_commands_origin_request_id" in command_indexes
        assert db.session.execute(text("SELECT id FROM platform_deployments WHERE id = 1")).scalar_one() == 1
        runtime_identity = db.session.execute(
            text(
                "SELECT kubernetes_deployment_mode, kubernetes_namespace "
                "FROM platform_deployments WHERE id = 1"
            )
        ).one()
        assert runtime_identity == ("helm", "original-apps")
        legacy_runtime_identity = db.session.execute(
            text(
                "SELECT kubernetes_deployment_mode, kubernetes_namespace "
                "FROM platform_deployments WHERE id = 2"
            )
        ).one()
        assert legacy_runtime_identity == ("manifest", "legacy-apps")
        legacy_resource_identity = db.session.execute(
            text(
                "SELECT kubernetes_deployment_name, kubernetes_service_name, kubernetes_ingress_name "
                "FROM platform_deployments WHERE id = 2"
            )
        ).one()
        assert legacy_resource_identity == (
            "paas-migration_project.withdot-2",
            "paas-migration_project.withdot-2-svc",
            "paas-migration_project.withdot-2",
        )
        assert db.session.execute(text("SELECT id FROM deployment_commands WHERE id = 1")).scalar_one() == 1
