import pytest

from control_plane import create_app


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
