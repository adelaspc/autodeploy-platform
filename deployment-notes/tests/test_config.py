from backend import config


def test_resolve_database_url_prefers_direct_env(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_NOTES_DATABASE_URL", "mysql+pymysql://direct")
    monkeypatch.setenv("DEPLOYMENT_NOTES_DATABASE_CONFIG_SSM_PARAM", "/app/db/config")
    monkeypatch.setenv("DEPLOYMENT_NOTES_DATABASE_URL_SSM_PARAM", "/app/db/url")
    monkeypatch.setattr(
        config,
        "get_ssm_parameter",
        lambda name: "mysql+pymysql://from-ssm",
    )

    assert config.resolve_database_url() == "mysql+pymysql://direct"


def test_resolve_database_url_uses_database_config_parameter(monkeypatch):
    requested_params = []
    requested_secrets = []

    def fake_get_ssm_parameter(name):
        requested_params.append(name)
        return (
            '{"host":"db.example.internal","port":3306,'
            '"database":"deployment_notes","username":"app_user",'
            '"secret_arn":"arn:aws:secretsmanager:region:acct:secret:rds"}'
        )

    def fake_get_secret_value(secret_arn):
        requested_secrets.append(secret_arn)
        return '{"username":"ignored","password":"p@ss word"}'

    monkeypatch.delenv("DEPLOYMENT_NOTES_DATABASE_URL", raising=False)
    monkeypatch.setenv("DEPLOYMENT_NOTES_DATABASE_CONFIG_SSM_PARAM", "/app/db/config")
    monkeypatch.setenv("DEPLOYMENT_NOTES_DATABASE_URL_SSM_PARAM", "/app/db/url")
    monkeypatch.setattr(config, "get_ssm_parameter", fake_get_ssm_parameter)
    monkeypatch.setattr(config, "get_secret_value", fake_get_secret_value)

    assert (
        config.resolve_database_url()
        == "mysql+pymysql://app_user:p%40ss+word@db.example.internal:3306/deployment_notes"
    )
    assert requested_params == ["/app/db/config"]
    assert requested_secrets == ["arn:aws:secretsmanager:region:acct:secret:rds"]


def test_resolve_database_url_uses_ssm_parameter_name(monkeypatch):
    requested_params = []

    def fake_get_ssm_parameter(name):
        requested_params.append(name)
        return "mysql+pymysql://from-ssm"

    monkeypatch.delenv("DEPLOYMENT_NOTES_DATABASE_URL", raising=False)
    monkeypatch.delenv("DEPLOYMENT_NOTES_DATABASE_CONFIG_SSM_PARAM", raising=False)
    monkeypatch.setenv("DEPLOYMENT_NOTES_DATABASE_URL_SSM_PARAM", "/app/db/url")
    monkeypatch.setattr(config, "get_ssm_parameter", fake_get_ssm_parameter)

    assert config.resolve_database_url() == "mysql+pymysql://from-ssm"
    assert requested_params == ["/app/db/url"]


def test_resolve_database_url_keeps_local_sqlite_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("DEPLOYMENT_NOTES_DATABASE_URL", raising=False)
    monkeypatch.delenv("DEPLOYMENT_NOTES_DATABASE_CONFIG_SSM_PARAM", raising=False)
    monkeypatch.delenv("DEPLOYMENT_NOTES_DATABASE_URL_SSM_PARAM", raising=False)
    monkeypatch.setenv("DEPLOYMENT_NOTES_ENV", "development")

    assert config.resolve_database_url(tmp_path).startswith("sqlite:///")
