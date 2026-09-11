"""Verify artifact pinning independently of mutable registry tags."""

import subprocess
from types import SimpleNamespace

import pytest
import yaml

from worker.execution.contracts import WorkerExecutionError
from worker.executors.local_docker import LocalDockerExecutor
from worker.helm.values import generic_web_app_values
from tests.execution.test_helm_values import make_deployment
from tests.execution.test_helm_values_render import helm_template, rendered_docs


DIGEST = "sha256:" + "a" * 64
TAGGED = "registry.example.com:5000/team/app:build-1"
PINNED = "registry.example.com:5000/team/app@" + DIGEST


def setup_registry(tmp_path, push_output):
    commands = []

    def runner(args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, push_output if args[:2] == ["docker", "push"] else "verified", "")

    executor = LocalDockerExecutor(workspace_root=tmp_path, command_timeout=30, runner=runner, registry_enabled=True)
    deployment = SimpleNamespace(
        id=1, project_id=1, project=SimpleNamespace(env_vars=[]),
        build=SimpleNamespace(image_tag="app:build-1", image_ref=TAGGED, commit_sha="a" * 40),
    )
    return executor, deployment, commands


def test_push_pins_reported_artifact_and_verification_never_resolves_tag(tmp_path):
    executor, deployment, commands = setup_registry(tmp_path, f"build-1: digest: {DIGEST} size: 1234\n")
    pushed = executor.push_image(deployment)
    assert pushed.image_ref == PINNED
    assert pushed.metadata["tagged_image_ref"] == TAGGED
    deployment.build.image_ref = pushed.image_ref
    # A tag may now point elsewhere; verification must use the push receipt.
    verified = executor.verify_image(deployment)
    assert verified.image_ref == PINNED
    assert commands[-1] == ["docker", "buildx", "imagetools", "inspect", PINNED]


@pytest.mark.parametrize("output", ["push ok", "digest: sha256:bad", f"digest: {DIGEST}\ndigest: sha256:" + "b" * 64])
def test_missing_invalid_or_ambiguous_push_digest_blocks_deployment(tmp_path, output):
    executor, deployment, _ = setup_registry(tmp_path, output)
    with pytest.raises(WorkerExecutionError, match="one valid image digest") as error:
        executor.push_image(deployment)
    assert error.value.step == "image.push"


def test_verification_rejects_unpinned_registry_reference(tmp_path):
    executor, deployment, commands = setup_registry(tmp_path, "")
    with pytest.raises(WorkerExecutionError, match="requires a digest"):
        executor.verify_image(deployment)
    assert commands == []


def test_repeated_builds_of_same_commit_receive_distinct_tags(tmp_path):
    executor, deployment, _ = setup_registry(tmp_path, "")
    first = executor._tag_suffix(deployment)
    second = executor._tag_suffix(deployment)
    assert first.startswith("aaaaaaaaaaaa-")
    assert first != second


def test_helm_renders_digest_even_when_tag_changes(tmp_path):
    deployment = make_deployment(image_ref=PINNED, image_tag="app:original")
    for tag in ("app:original", "app:replacement"):
        deployment.build.image_tag = tag
        values = generic_web_app_values(deployment)
        path = tmp_path / "values.yaml"
        path.write_text(yaml.safe_dump(values))
        docs = rendered_docs(helm_template("digest-test", "deploy/helm/generic-web-app", "-f", str(path)))
        workload = next(doc for doc in docs if doc["kind"] == "Deployment")
        assert workload["spec"]["template"]["spec"]["containers"][0]["image"] == PINNED
