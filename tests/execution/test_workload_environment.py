from types import SimpleNamespace

from worker.executors.fake import FakeDeploymentExecutor
from worker.workload_environment import resolved_workload_environment


def make_deployment():
    return SimpleNamespace(
        id=17,
        service_url=None,
        project=SimpleNamespace(
            name="identity-app",
            env_vars=[
                {"name": "APP_ENV", "value": "production"},
                {"name": "APP_COMMIT_SHA", "value": "project-commit"},
                {"name": "APP_VERSION", "value": "project-version"},
            ],
        ),
        build=SimpleNamespace(commit_sha="build-commit", image_tag="identity-app:build-17"),
    )


def test_resolved_workload_environment_replaces_reserved_project_values():
    environment = resolved_workload_environment(make_deployment())

    assert environment == [
        {"name": "APP_ENV", "value": "production"},
        {"name": "APP_COMMIT_SHA", "value_source": "literal", "value": "build-commit"},
        {"name": "APP_VERSION", "value_source": "literal", "value": "identity-app:build-17"},
    ]


def test_fake_executor_records_the_resolved_workload_environment_names():
    result = FakeDeploymentExecutor().deploy(make_deployment())

    assert result.metadata["platform_environment_names"] == ["APP_ENV", "APP_COMMIT_SHA", "APP_VERSION"]
