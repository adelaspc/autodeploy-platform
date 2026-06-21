import os
import json
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv


load_dotenv()


def truthy_env(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def current_environment():
    return os.getenv("DEPLOYMENT_NOTES_ENV", "").strip().lower()


def should_serve_frontend():
    configured_value = os.getenv("DEPLOYMENT_NOTES_SERVE_FRONTEND")
    if configured_value is not None:
        return truthy_env(configured_value)

    return current_environment() in {"local", "development", "test"}


def get_ssm_parameter(name):
    import boto3

    region_name = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    client = boto3.client("ssm", region_name=region_name)
    response = client.get_parameter(Name=name, WithDecryption=True)
    return response["Parameter"]["Value"]


def get_secret_value(secret_arn):
    import boto3

    region_name = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    client = boto3.client("secretsmanager", region_name=region_name)
    response = client.get_secret_value(SecretId=secret_arn)
    return response["SecretString"]


def build_mysql_url(username, password, host, port, database):
    return (
        f"mysql+pymysql://{quote_plus(username)}:{quote_plus(password)}"
        f"@{host}:{port}/{database}"
    )


def resolve_database_url_from_config_parameter(parameter_name):
    config = json.loads(get_ssm_parameter(parameter_name))
    secret = json.loads(get_secret_value(config["secret_arn"]))

    username = config.get("username") or secret["username"]
    password = secret["password"]

    return build_mysql_url(
        username=username,
        password=password,
        host=config["host"],
        port=config.get("port", 3306),
        database=config["database"],
    )


def resolve_database_url(instance_path=None):
    database_url = os.getenv("DEPLOYMENT_NOTES_DATABASE_URL")
    if database_url:
        return database_url

    database_config_param = os.getenv("DEPLOYMENT_NOTES_DATABASE_CONFIG_SSM_PARAM")
    if database_config_param:
        return resolve_database_url_from_config_parameter(database_config_param)

    database_url_param = os.getenv("DEPLOYMENT_NOTES_DATABASE_URL_SSM_PARAM")
    if database_url_param:
        return get_ssm_parameter(database_url_param)

    app_env = current_environment()
    if app_env in {"local", "development", "test"}:
        database_path = Path(instance_path or "instance") / "deployment_notes.db"
        return f"sqlite:///{database_path.resolve()}"

    raise RuntimeError(
        "DEPLOYMENT_NOTES_DATABASE_URL, DEPLOYMENT_NOTES_DATABASE_CONFIG_SSM_PARAM, "
        "or DEPLOYMENT_NOTES_DATABASE_URL_SSM_PARAM must be set when "
        f"DEPLOYMENT_NOTES_ENV is '{app_env or 'unset'}'"
    )


class Config:
    SQLALCHEMY_DATABASE_URI = os.getenv("DEPLOYMENT_NOTES_DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SERVE_FRONTEND = should_serve_frontend()
    LOG_LEVEL = os.getenv("DEPLOYMENT_NOTES_LOG_LEVEL", "INFO")

    @staticmethod
    def init_app(app):
        if not app.config.get("SQLALCHEMY_DATABASE_URI"):
            app.config["SQLALCHEMY_DATABASE_URI"] = resolve_database_url(app.instance_path)
