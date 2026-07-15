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


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    SQLALCHEMY_DATABASE_URI = os.getenv("CONTROL_PLANE_DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    CONTROL_PLANE_ENV = os.getenv("CONTROL_PLANE_ENV", "").strip().lower()
    CONTROL_PLANE_EXECUTOR = os.getenv("CONTROL_PLANE_EXECUTOR", "fake")
    CONTROL_PLANE_WORKSPACE_ROOT = os.getenv("CONTROL_PLANE_WORKSPACE_ROOT", "/tmp/paas-workspaces")
    CONTROL_PLANE_COMMAND_TIMEOUT_SECONDS = int(os.getenv("CONTROL_PLANE_COMMAND_TIMEOUT_SECONDS", "600"))
    CONTROL_PLANE_COMMAND_RETRY_COUNT = int(os.getenv("CONTROL_PLANE_COMMAND_RETRY_COUNT", "1"))
    CONTROL_PLANE_REGISTRY_ENABLED = env_bool("CONTROL_PLANE_REGISTRY_ENABLED", False)
    CONTROL_PLANE_REGISTRY_URL = os.getenv("CONTROL_PLANE_REGISTRY_URL")
    CONTROL_PLANE_REGISTRY_NAMESPACE = os.getenv("CONTROL_PLANE_REGISTRY_NAMESPACE")
    CONTROL_PLANE_REGISTRY_USERNAME = os.getenv("CONTROL_PLANE_REGISTRY_USERNAME")
    CONTROL_PLANE_REGISTRY_PASSWORD = os.getenv("CONTROL_PLANE_REGISTRY_PASSWORD")
    CONTROL_PLANE_API_TOKEN_READ_ONLY = os.getenv("CONTROL_PLANE_API_TOKEN_READ_ONLY")
    CONTROL_PLANE_API_TOKEN_DEPLOYER = os.getenv("CONTROL_PLANE_API_TOKEN_DEPLOYER")
    CONTROL_PLANE_API_TOKEN_ADMIN = os.getenv("CONTROL_PLANE_API_TOKEN_ADMIN")
    CONTROL_PLANE_API_TOKENS_JSON = os.getenv("CONTROL_PLANE_API_TOKENS_JSON")
    CONTROL_PLANE_ALLOW_AUTH_DISABLED = env_bool("CONTROL_PLANE_ALLOW_AUTH_DISABLED", False)
    CONTROL_PLANE_GITHUB_WEBHOOK_SECRET = os.getenv("CONTROL_PLANE_GITHUB_WEBHOOK_SECRET")
    CONTROL_PLANE_METRICS_ENABLED = env_bool("CONTROL_PLANE_METRICS_ENABLED", False)
    CONTROL_PLANE_METRICS_TOKEN = os.getenv("CONTROL_PLANE_METRICS_TOKEN")
    CONTROL_PLANE_LOG_FORMAT = os.getenv("CONTROL_PLANE_LOG_FORMAT", "json")
    CONTROL_PLANE_COMPONENT = os.getenv("CONTROL_PLANE_COMPONENT", "api")
    CONTROL_PLANE_KUBECONFIG = os.getenv("CONTROL_PLANE_KUBECONFIG")
    CONTROL_PLANE_K8S_NAMESPACE = os.getenv("CONTROL_PLANE_K8S_NAMESPACE", "default")
    CONTROL_PLANE_K8S_IMAGE_PULL_SECRET = os.getenv("CONTROL_PLANE_K8S_IMAGE_PULL_SECRET")
    CONTROL_PLANE_K8S_DEPLOYMENT_MODE = os.getenv("CONTROL_PLANE_K8S_DEPLOYMENT_MODE", "manifest")
    CONTROL_PLANE_K8S_HELM_CHART_PATH = os.getenv(
        "CONTROL_PLANE_K8S_HELM_CHART_PATH", "deploy/helm/generic-web-app"
    )
    CONTROL_PLANE_K8S_HELM_BINARY = os.getenv("CONTROL_PLANE_K8S_HELM_BINARY", "helm")
    CONTROL_PLANE_K8S_HELM_TIMEOUT = os.getenv("CONTROL_PLANE_K8S_HELM_TIMEOUT", "180s")
    CONTROL_PLANE_K8S_INGRESS_ENABLED = env_bool("CONTROL_PLANE_K8S_INGRESS_ENABLED", False)
    CONTROL_PLANE_K8S_INGRESS_CLASS_NAME = os.getenv("CONTROL_PLANE_K8S_INGRESS_CLASS_NAME", "")
    CONTROL_PLANE_K8S_INGRESS_BASE_DOMAIN = os.getenv(
        "CONTROL_PLANE_K8S_INGRESS_BASE_DOMAIN", "127.0.0.1.nip.io"
    )
    CONTROL_PLANE_DEPLOY_HOST = os.getenv("CONTROL_PLANE_DEPLOY_HOST", "127.0.0.1")
    CONTROL_PLANE_HEALTHCHECK_TIMEOUT_SECONDS = int(
        os.getenv("CONTROL_PLANE_HEALTHCHECK_TIMEOUT_SECONDS", "30")
    )
    CONTROL_PLANE_HEALTHCHECK_INTERVAL_SECONDS = float(
        os.getenv("CONTROL_PLANE_HEALTHCHECK_INTERVAL_SECONDS", "1")
    )
    CONTROL_PLANE_WORKER_POLL_INTERVAL_SECONDS = float(
        os.getenv("CONTROL_PLANE_WORKER_POLL_INTERVAL_SECONDS", "5")
    )
    CONTROL_PLANE_RECONCILER_POLL_INTERVAL_SECONDS = float(
        os.getenv("CONTROL_PLANE_RECONCILER_POLL_INTERVAL_SECONDS", "60")
    )
    CONTROL_PLANE_WORKER_ID = os.getenv("CONTROL_PLANE_WORKER_ID", f"{socket.gethostname()}:{os.getpid()}")
    CONTROL_PLANE_CLAIM_TTL_SECONDS = int(os.getenv("CONTROL_PLANE_CLAIM_TTL_SECONDS", "300"))
    CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS = float(
        os.getenv("CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS", "30")
    )

    @staticmethod
    def init_app(app):
        from control_plane.api.auth import api_auth_disabled_allowed_for_config, api_token_configs_from_config

        if not app.config.get("SQLALCHEMY_DATABASE_URI"):
            app.config["SQLALCHEMY_DATABASE_URI"] = resolve_database_url(app.instance_path)
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
