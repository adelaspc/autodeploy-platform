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
