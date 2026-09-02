import pytest

from control_plane import create_app
from control_plane.config import Config, env_bool, resolve_database_url


class ValidConfig:
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    CONTROL_PLANE_ENV = "test"
    CONTROL_PLANE_ALLOW_AUTH_DISABLED = True
    CONTROL_PLANE_CLAIM_TTL_SECONDS = 30
    CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS = 5


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MAX_CONTENT_LENGTH", 0),
        ("CONTROL_PLANE_COMMAND_TIMEOUT_SECONDS", 0),
        ("CONTROL_PLANE_COMMAND_RETRY_COUNT", -1),
        ("CONTROL_PLANE_HEALTHCHECK_TIMEOUT_SECONDS", -1),
        ("CONTROL_PLANE_K8S_ROLLOUT_TIMEOUT_SECONDS", 0),
        ("CONTROL_PLANE_HEALTHCHECK_INTERVAL_SECONDS", 0),
        ("CONTROL_PLANE_WORKER_POLL_INTERVAL_SECONDS", 0),
        ("CONTROL_PLANE_RECONCILER_POLL_INTERVAL_SECONDS", -1),
        ("CONTROL_PLANE_CLAIM_TTL_SECONDS", 0),
        ("CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS", -1),
        ("CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS", 30),
    ],
)
def test_invalid_numeric_config_fails_fast(name, value):
    invalid_config = type("InvalidConfig", (ValidConfig,), {name: value})

    with pytest.raises(RuntimeError):
        create_app(invalid_config)


def test_zero_claim_refresh_interval_is_allowed_for_synchronous_tests():
    config = type("ZeroRefreshConfig", (ValidConfig,), {"CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS": 0})

    app = create_app(config)

    assert app.config["CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS"] == 0


def test_negative_trusted_proxy_count_fails_fast():
    invalid_config = type("InvalidProxyConfig", (ValidConfig,), {"CONTROL_PLANE_TRUSTED_PROXY_COUNT": -1})

    with pytest.raises(RuntimeError, match="CONTROL_PLANE_TRUSTED_PROXY_COUNT"):
        create_app(invalid_config)


@pytest.mark.parametrize(("raw_value", "expected"), [("true", True), (" YES ", True), ("0", False), ("invalid", False)])
def test_env_bool_accepts_only_explicit_true_values(monkeypatch, raw_value, expected):
    monkeypatch.setenv("CONTROL_PLANE_TEST_BOOLEAN", raw_value)

    assert env_bool("CONTROL_PLANE_TEST_BOOLEAN") is expected


def test_database_url_is_required_outside_local_environments(monkeypatch):
    monkeypatch.delenv("CONTROL_PLANE_DATABASE_URL", raising=False)
    monkeypatch.setenv("CONTROL_PLANE_ENV", "production")

    with pytest.raises(RuntimeError, match="CONTROL_PLANE_DATABASE_URL"):
        resolve_database_url()


def test_local_database_url_uses_the_requested_instance_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("CONTROL_PLANE_DATABASE_URL", raising=False)
    monkeypatch.setenv("CONTROL_PLANE_ENV", "development")

    assert resolve_database_url(tmp_path) == f"sqlite:///{tmp_path / 'control_plane.db'}"


def test_environment_config_is_built_from_the_supplied_mapping_not_import_state():
    first = Config.from_env(
        {
            "CONTROL_PLANE_ENV": "development",
            "CONTROL_PLANE_EXECUTOR": "fake",
            "CONTROL_PLANE_ALLOW_AUTH_DISABLED": "true",
            "CONTROL_PLANE_WORKER_POLL_INTERVAL_SECONDS": "2.5",
        }
    )
    second = Config.from_env(
        {
            "CONTROL_PLANE_ENV": "test",
            "CONTROL_PLANE_EXECUTOR": "kubernetes",
            "CONTROL_PLANE_ALLOW_AUTH_DISABLED": "yes",
            "CONTROL_PLANE_WORKER_POLL_INTERVAL_SECONDS": "7",
        }
    )

    assert first.CONTROL_PLANE_ENV == "development"
    assert first.CONTROL_PLANE_EXECUTOR == "fake"
    assert first.CONTROL_PLANE_WORKER_POLL_INTERVAL_SECONDS == 2.5
    assert second.CONTROL_PLANE_ENV == "test"
    assert second.CONTROL_PLANE_EXECUTOR == "kubernetes"
    assert second.CONTROL_PLANE_WORKER_POLL_INTERVAL_SECONDS == 7.0


def test_environment_config_rejects_non_numeric_values_at_app_construction_time():
    with pytest.raises(ValueError, match="invalid literal"):
        Config.from_env({"CONTROL_PLANE_COMMAND_TIMEOUT_SECONDS": "ten"})
