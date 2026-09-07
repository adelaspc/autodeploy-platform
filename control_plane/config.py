"""Load control-plane settings and validate combinations required at startup."""

import os
import socket
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


def resolve_database_url(instance_path=None):
    database_url = os.getenv("CONTROL_PLANE_DATABASE_URL")
    if database_url:
        return database_url

    app_env = os.getenv("CONTROL_PLANE_ENV", "").strip().lower()
    if app_env in {"local", "development", "test"}:
        if instance_path is None:
            instance_dir = Path.cwd() / "instance"
        else:
            instance_dir = Path(instance_path)
        instance_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(instance_dir / 'control_plane.db').resolve()}"

    raise RuntimeError(
        f"CONTROL_PLANE_DATABASE_URL must be set when CONTROL_PLANE_ENV is '{app_env or 'unset'}'"
    )


def env_bool(name, default=False, *, environ=None):
    source = os.environ if environ is None else environ
    value = source.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def validate_numeric_config(app):
    positive_settings = {
        "MAX_CONTENT_LENGTH": 2 * 1024 * 1024,
        "CONTROL_PLANE_COMMAND_TIMEOUT_SECONDS": 600,
        "CONTROL_PLANE_HEALTHCHECK_TIMEOUT_SECONDS": 30,
        "CONTROL_PLANE_K8S_ROLLOUT_TIMEOUT_SECONDS": 120,
        "CONTROL_PLANE_HEALTHCHECK_INTERVAL_SECONDS": 1,
        "CONTROL_PLANE_WORKER_POLL_INTERVAL_SECONDS": 5,
        "CONTROL_PLANE_RECONCILER_POLL_INTERVAL_SECONDS": 60,
        "CONTROL_PLANE_CLAIM_TTL_SECONDS": 300,
    }
    for name, default in positive_settings.items():
        value = app.config.get(name)
        if value is None:
            value = default
        if value <= 0:
            raise RuntimeError(f"{name} must be greater than zero")
    if app.config.get("CONTROL_PLANE_COMMAND_RETRY_COUNT", 0) < 0:
        raise RuntimeError("CONTROL_PLANE_COMMAND_RETRY_COUNT cannot be negative")
    refresh_interval = app.config.get("CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS", 30)
    claim_ttl = app.config.get("CONTROL_PLANE_CLAIM_TTL_SECONDS", 300)
    if refresh_interval < 0:
        raise RuntimeError("CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS cannot be negative")
    if refresh_interval >= claim_ttl:
        raise RuntimeError(
            "CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS must be less than CONTROL_PLANE_CLAIM_TTL_SECONDS"
        )


class Config:
    SQLALCHEMY_DATABASE_URI = None
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAX_CONTENT_LENGTH = 2 * 1024 * 1024
    CONTROL_PLANE_ENV = ""
    CONTROL_PLANE_EXECUTOR = "fake"
    CONTROL_PLANE_WORKSPACE_ROOT = "/tmp/paas-workspaces"
    CONTROL_PLANE_LOCAL_REPO_ROOT = "/tmp/paas-local-repos"
    CONTROL_PLANE_COMMAND_TIMEOUT_SECONDS = 600
    CONTROL_PLANE_COMMAND_RETRY_COUNT = 1
    CONTROL_PLANE_REGISTRY_ENABLED = False
    CONTROL_PLANE_REGISTRY_URL = None
    CONTROL_PLANE_REGISTRY_NAMESPACE = None
    CONTROL_PLANE_REGISTRY_USERNAME = None
    CONTROL_PLANE_REGISTRY_PASSWORD = None
    CONTROL_PLANE_API_TOKEN_READ_ONLY = None
    CONTROL_PLANE_API_TOKEN_DEPLOYER = None
    CONTROL_PLANE_API_TOKEN_ADMIN = None
    CONTROL_PLANE_API_TOKENS_JSON = None
    CONTROL_PLANE_ALLOW_AUTH_DISABLED = False
    CONTROL_PLANE_GITHUB_WEBHOOK_SECRET = None
    CONTROL_PLANE_METRICS_ENABLED = False
    CONTROL_PLANE_METRICS_TOKEN = None
    CONTROL_PLANE_LOG_FORMAT = "json"
    CONTROL_PLANE_COMPONENT = "api"
    CONTROL_PLANE_TRUSTED_PROXY_COUNT = 0
    CONTROL_PLANE_KUBECONFIG = None
    CONTROL_PLANE_K8S_NAMESPACE = "default"
    CONTROL_PLANE_K8S_IMAGE_PULL_SECRET = None
    CONTROL_PLANE_K8S_DEPLOYMENT_MODE = "manifest"
    CONTROL_PLANE_K8S_HELM_CHART_PATH = "deploy/helm/generic-web-app"
    CONTROL_PLANE_K8S_HELM_BINARY = "helm"
    CONTROL_PLANE_K8S_HELM_TIMEOUT = "180s"
    CONTROL_PLANE_K8S_INGRESS_ENABLED = False
    CONTROL_PLANE_K8S_INGRESS_CLASS_NAME = ""
    CONTROL_PLANE_K8S_INGRESS_BASE_DOMAIN = "127.0.0.1.nip.io"
    CONTROL_PLANE_DEPLOY_HOST = "127.0.0.1"
    CONTROL_PLANE_HEALTHCHECK_TIMEOUT_SECONDS = 30
    CONTROL_PLANE_K8S_ROLLOUT_TIMEOUT_SECONDS = 120
    CONTROL_PLANE_HEALTHCHECK_INTERVAL_SECONDS = 1.0
    CONTROL_PLANE_WORKER_POLL_INTERVAL_SECONDS = 5.0
    CONTROL_PLANE_RECONCILER_POLL_INTERVAL_SECONDS = 60.0
    CONTROL_PLANE_WORKER_ID = None
    CONTROL_PLANE_CLAIM_TTL_SECONDS = 300
    CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS = 30.0

    @classmethod
    def from_env(cls, environ=None):
        source = os.environ if environ is None else environ
        value = source.get
        settings = {
            "SQLALCHEMY_DATABASE_URI": value("CONTROL_PLANE_DATABASE_URL"),
            "MAX_CONTENT_LENGTH": int(value("CONTROL_PLANE_MAX_CONTENT_LENGTH", str(2 * 1024 * 1024))),
            "CONTROL_PLANE_ENV": value("CONTROL_PLANE_ENV", "").strip().lower(),
            "CONTROL_PLANE_EXECUTOR": value("CONTROL_PLANE_EXECUTOR", "fake"),
            "CONTROL_PLANE_WORKSPACE_ROOT": value("CONTROL_PLANE_WORKSPACE_ROOT", "/tmp/paas-workspaces"),
            "CONTROL_PLANE_LOCAL_REPO_ROOT": value("CONTROL_PLANE_LOCAL_REPO_ROOT", "/tmp/paas-local-repos"),
            "CONTROL_PLANE_COMMAND_TIMEOUT_SECONDS": int(value("CONTROL_PLANE_COMMAND_TIMEOUT_SECONDS", "600")),
            "CONTROL_PLANE_COMMAND_RETRY_COUNT": int(value("CONTROL_PLANE_COMMAND_RETRY_COUNT", "1")),
            "CONTROL_PLANE_REGISTRY_ENABLED": env_bool("CONTROL_PLANE_REGISTRY_ENABLED", environ=source),
            "CONTROL_PLANE_REGISTRY_URL": value("CONTROL_PLANE_REGISTRY_URL"),
            "CONTROL_PLANE_REGISTRY_NAMESPACE": value("CONTROL_PLANE_REGISTRY_NAMESPACE"),
            "CONTROL_PLANE_REGISTRY_USERNAME": value("CONTROL_PLANE_REGISTRY_USERNAME"),
            "CONTROL_PLANE_REGISTRY_PASSWORD": value("CONTROL_PLANE_REGISTRY_PASSWORD"),
            "CONTROL_PLANE_API_TOKEN_READ_ONLY": value("CONTROL_PLANE_API_TOKEN_READ_ONLY"),
            "CONTROL_PLANE_API_TOKEN_DEPLOYER": value("CONTROL_PLANE_API_TOKEN_DEPLOYER"),
            "CONTROL_PLANE_API_TOKEN_ADMIN": value("CONTROL_PLANE_API_TOKEN_ADMIN"),
            "CONTROL_PLANE_API_TOKENS_JSON": value("CONTROL_PLANE_API_TOKENS_JSON"),
            "CONTROL_PLANE_ALLOW_AUTH_DISABLED": env_bool("CONTROL_PLANE_ALLOW_AUTH_DISABLED", environ=source),
            "CONTROL_PLANE_GITHUB_WEBHOOK_SECRET": value("CONTROL_PLANE_GITHUB_WEBHOOK_SECRET"),
            "CONTROL_PLANE_METRICS_ENABLED": env_bool("CONTROL_PLANE_METRICS_ENABLED", environ=source),
            "CONTROL_PLANE_METRICS_TOKEN": value("CONTROL_PLANE_METRICS_TOKEN"),
            "CONTROL_PLANE_LOG_FORMAT": value("CONTROL_PLANE_LOG_FORMAT", "json"),
            "CONTROL_PLANE_COMPONENT": value("CONTROL_PLANE_COMPONENT", "api"),
            "CONTROL_PLANE_TRUSTED_PROXY_COUNT": int(value("CONTROL_PLANE_TRUSTED_PROXY_COUNT", "0")),
            "CONTROL_PLANE_KUBECONFIG": value("CONTROL_PLANE_KUBECONFIG"),
            "CONTROL_PLANE_K8S_NAMESPACE": value("CONTROL_PLANE_K8S_NAMESPACE", "default"),
            "CONTROL_PLANE_K8S_IMAGE_PULL_SECRET": value("CONTROL_PLANE_K8S_IMAGE_PULL_SECRET"),
            "CONTROL_PLANE_K8S_DEPLOYMENT_MODE": value("CONTROL_PLANE_K8S_DEPLOYMENT_MODE", "manifest"),
            "CONTROL_PLANE_K8S_HELM_CHART_PATH": value("CONTROL_PLANE_K8S_HELM_CHART_PATH", "deploy/helm/generic-web-app"),
            "CONTROL_PLANE_K8S_HELM_BINARY": value("CONTROL_PLANE_K8S_HELM_BINARY", "helm"),
            "CONTROL_PLANE_K8S_HELM_TIMEOUT": value("CONTROL_PLANE_K8S_HELM_TIMEOUT", "180s"),
            "CONTROL_PLANE_K8S_INGRESS_ENABLED": env_bool("CONTROL_PLANE_K8S_INGRESS_ENABLED", environ=source),
            "CONTROL_PLANE_K8S_INGRESS_CLASS_NAME": value("CONTROL_PLANE_K8S_INGRESS_CLASS_NAME", ""),
            "CONTROL_PLANE_K8S_INGRESS_BASE_DOMAIN": value("CONTROL_PLANE_K8S_INGRESS_BASE_DOMAIN", "127.0.0.1.nip.io"),
            "CONTROL_PLANE_DEPLOY_HOST": value("CONTROL_PLANE_DEPLOY_HOST", "127.0.0.1"),
            "CONTROL_PLANE_HEALTHCHECK_TIMEOUT_SECONDS": int(value("CONTROL_PLANE_HEALTHCHECK_TIMEOUT_SECONDS", "30")),
            "CONTROL_PLANE_K8S_ROLLOUT_TIMEOUT_SECONDS": int(value("CONTROL_PLANE_K8S_ROLLOUT_TIMEOUT_SECONDS", "120")),
            "CONTROL_PLANE_HEALTHCHECK_INTERVAL_SECONDS": float(value("CONTROL_PLANE_HEALTHCHECK_INTERVAL_SECONDS", "1")),
            "CONTROL_PLANE_WORKER_POLL_INTERVAL_SECONDS": float(value("CONTROL_PLANE_WORKER_POLL_INTERVAL_SECONDS", "5")),
            "CONTROL_PLANE_RECONCILER_POLL_INTERVAL_SECONDS": float(value("CONTROL_PLANE_RECONCILER_POLL_INTERVAL_SECONDS", "60")),
            "CONTROL_PLANE_WORKER_ID": value("CONTROL_PLANE_WORKER_ID", f"{socket.gethostname()}:{os.getpid()}"),
            "CONTROL_PLANE_CLAIM_TTL_SECONDS": int(value("CONTROL_PLANE_CLAIM_TTL_SECONDS", "300")),
            "CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS": float(value("CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS", "30")),
        }
        return type("EnvironmentConfig", (cls,), settings)

    @staticmethod
    def init_app(app):
        from control_plane.api.auth import api_auth_disabled_allowed_for_config, api_token_configs_from_config

        if not app.config.get("SQLALCHEMY_DATABASE_URI"):
            app.config["SQLALCHEMY_DATABASE_URI"] = resolve_database_url(app.instance_path)
        validate_numeric_config(app)
        if app.config.get("CONTROL_PLANE_TRUSTED_PROXY_COUNT", 0) < 0:
            raise RuntimeError("CONTROL_PLANE_TRUSTED_PROXY_COUNT cannot be negative")
        if app.config.get("CONTROL_PLANE_METRICS_ENABLED"):
            token = app.config.get("CONTROL_PLANE_METRICS_TOKEN")
            if not isinstance(token, str) or not token.strip():
                raise RuntimeError(
                    "CONTROL_PLANE_METRICS_TOKEN must be set when CONTROL_PLANE_METRICS_ENABLED is true"
                )
            app.config["CONTROL_PLANE_METRICS_TOKEN"] = token.strip()
        if not api_token_configs_from_config(app.config) and not api_auth_disabled_allowed_for_config(app.config):
            raise RuntimeError(
                "API bearer tokens must be configured unless CONTROL_PLANE_ALLOW_AUTH_DISABLED=true "
                "is explicitly set for a local development or test environment"
            )
