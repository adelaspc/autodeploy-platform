import re
from pathlib import Path

import pytest
import yaml
from dotenv import dotenv_values

from control_plane.config import Config


REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PROFILES = (
    REPO_ROOT / ".env.example",
    REPO_ROOT / ".env.local-docker.example",
    REPO_ROOT / ".env.local-kubernetes.example",
)
ENV_CONFIG_EXCEPTIONS = {
    "CONTROL_PLANE_COMPONENT",
    "CONTROL_PLANE_DATABASE_URI",
}
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def test_environment_profiles_expose_the_same_keys():
    profile_keys = [{key for key in dotenv_values(path)} for path in ENV_PROFILES]

    assert profile_keys[1:] == profile_keys[:-1]


def test_compose_scopes_secret_environment_by_component():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    platform_secret_keys = {
        "CONTROL_PLANE_DATABASE_URL",
        "CONTROL_PLANE_GITHUB_WEBHOOK_SECRET",
        "CONTROL_PLANE_API_TOKEN_READ_ONLY",
        "CONTROL_PLANE_API_TOKEN_DEPLOYER",
        "CONTROL_PLANE_API_TOKEN_ADMIN",
        "CONTROL_PLANE_API_TOKENS_JSON",
        "CONTROL_PLANE_REGISTRY_USERNAME",
        "CONTROL_PLANE_REGISTRY_PASSWORD",
        "CONTROL_PLANE_METRICS_TOKEN",
        "CONTROL_PLANE_GIT_TOKEN_GITHUB",
    }

    def scoped_keys(service_name):
        return set(services[service_name]["environment"]) & platform_secret_keys

    assert scoped_keys("control-plane-api") == {
        "CONTROL_PLANE_DATABASE_URL",
        "CONTROL_PLANE_GITHUB_WEBHOOK_SECRET",
        "CONTROL_PLANE_API_TOKEN_READ_ONLY",
        "CONTROL_PLANE_API_TOKEN_DEPLOYER",
        "CONTROL_PLANE_API_TOKEN_ADMIN",
        "CONTROL_PLANE_API_TOKENS_JSON",
        "CONTROL_PLANE_METRICS_TOKEN",
        "CONTROL_PLANE_GIT_TOKEN_GITHUB",
    }
    assert scoped_keys("control-plane-worker") == {
        "CONTROL_PLANE_DATABASE_URL",
        "CONTROL_PLANE_REGISTRY_USERNAME",
        "CONTROL_PLANE_REGISTRY_PASSWORD",
        "CONTROL_PLANE_GIT_TOKEN_GITHUB",
    }
    assert scoped_keys("control-plane-reconciler") == {"CONTROL_PLANE_DATABASE_URL"}
    assert scoped_keys("migrate") == {"CONTROL_PLANE_DATABASE_URL"}
    assert {
        service_name: services[service_name]["environment"]["CONTROL_PLANE_COMPONENT"]
        for service_name in (
            "control-plane-api",
            "control-plane-worker",
            "control-plane-reconciler",
            "migrate",
        )
    } == {
        "control-plane-api": "api",
        "control-plane-worker": "worker",
        "control-plane-reconciler": "reconciler",
        "migrate": "migrate",
    }
    assert all(
        ".env.secrets" not in str(source)
        for service in services.values()
        for source in service.get("env_file", [])
    )


def test_application_spec_example_uses_supported_env_var_shapes():
    specs = (REPO_ROOT / "docs/specs.md").read_text(encoding="utf-8")

    assert "required: true" not in specs
    assert "value_source: secret_key_ref" in specs
    assert "source_name: my-app-secret" in specs
    assert "source_key: database-url" in specs


def test_api_reference_documents_runtime_contract_semantics():
    api_reference = (REPO_ROOT / "docs/api-reference.md").read_text(encoding="utf-8")

    assert "`GET /health` and `GET /health/ready` are public" in api_reference
    assert "an explicit `null` disables tests" in api_reference
    assert "an empty string is invalid and returns `400`" in api_reference
    assert "`tail_lines` parameter from `1` through `2000`, defaulting to `200`" in api_reference
    assert '`{"error": "Deployment does not use the Kubernetes target"}`' in api_reference
    assert "invalid lifecycle transitions return `400`" in api_reference


def test_every_runtime_config_key_is_represented_in_an_environment_example():
    documented_keys = set().union(*(dotenv_values(path).keys() for path in ENV_PROFILES))
    documented_keys.update(dotenv_values(REPO_ROOT / ".env.secrets.example").keys())
    runtime_keys = {
        name
        for name in vars(Config)
        if name.startswith("CONTROL_PLANE_")
    }

    assert runtime_keys - documented_keys <= ENV_CONFIG_EXCEPTIONS


@pytest.mark.parametrize(
    "path",
    sorted(
        path
        for path in (REPO_ROOT / "deploy").rglob("*.yaml")
        if "templates" not in path.parts
    ),
    ids=lambda path: str(path.relative_to(REPO_ROOT)),
)
def test_deployment_yaml_examples_are_parseable(path):
    documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))

    assert documents
    assert all(document is not None for document in documents)


@pytest.mark.parametrize(
    "path",
    [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").glob("*.md"))],
    ids=lambda path: str(path.relative_to(REPO_ROOT)),
)
def test_local_markdown_links_point_to_existing_paths(path):
    missing = []
    for target in MARKDOWN_LINK.findall(path.read_text(encoding="utf-8")):
        target = target.strip().split("#", 1)[0]
        if not target or "://" in target or target.startswith(("mailto:", "#")):
            continue
        resolved = (path.parent / target).resolve()
        if not resolved.exists():
            missing.append(target)

    assert missing == []
