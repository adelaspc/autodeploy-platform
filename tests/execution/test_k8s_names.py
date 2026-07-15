import re
from types import SimpleNamespace

from worker.executors.kubernetes.names import MAX_HELM_RELEASE_LENGTH, dns_slug, helm_release_name, workload_labels, workload_name


DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def make_project(project_id=42, name="demo-app"):
    return SimpleNamespace(id=project_id, name=name)


def make_deployment(deployment_id=7, environment="production"):
    return SimpleNamespace(id=deployment_id, environment=environment)


def assert_dns_safe(value, *, max_length=63):
    assert DNS_LABEL_RE.fullmatch(value)
    assert len(value) <= max_length


def test_release_name_for_normal_project_name():
    release = helm_release_name(make_project(42, "demo-app"), make_deployment(environment="production"))

    assert release == "paas-demo-app-production-42"
    assert_dns_safe(release, max_length=MAX_HELM_RELEASE_LENGTH)


def test_release_name_lowercases_uppercase_project_name():
    release = helm_release_name(make_project(42, "Demo API"), make_deployment())

    assert release == "paas-demo-api-production-42"
    assert_dns_safe(release, max_length=MAX_HELM_RELEASE_LENGTH)


def test_release_name_replaces_spaces_and_underscores():
    release = helm_release_name(make_project(42, "my_demo app"), make_deployment())

    assert release == "paas-my-demo-app-production-42"
    assert_dns_safe(release, max_length=MAX_HELM_RELEASE_LENGTH)


def test_release_name_replaces_special_characters():
    release = helm_release_name(make_project(42, "my/app@prod!"), make_deployment(environment="staging_env"))

    assert release == "paas-my-app-prod-staging-env-42"
    assert_dns_safe(release, max_length=MAX_HELM_RELEASE_LENGTH)


def test_release_name_truncates_very_long_project_name():
    release = helm_release_name(make_project(42, "a" * 120), make_deployment(environment="production"))

    assert release.startswith("paas-")
    assert release.endswith("-production-42")
    assert_dns_safe(release, max_length=MAX_HELM_RELEASE_LENGTH)


def test_empty_or_invalid_project_name_uses_fallback():
    assert workload_name(make_project(42, "!!!")) == "app"
    release = helm_release_name(make_project(42, "!!!"), make_deployment())

    assert release == "paas-app-production-42"
    assert_dns_safe(release, max_length=MAX_HELM_RELEASE_LENGTH)


def test_same_name_projects_with_different_ids_generate_different_release_names():
    first = helm_release_name(make_project(42, "demo-app"), make_deployment())
    second = helm_release_name(make_project(43, "demo-app"), make_deployment())

    assert first != second
    assert first == "paas-demo-app-production-42"
    assert second == "paas-demo-app-production-43"


def test_release_name_is_stable_for_same_project_and_environment():
    project = make_project(42, "demo-app")
    deployment = make_deployment(deployment_id=7, environment="production")

    assert helm_release_name(project, deployment) == helm_release_name(project, deployment)


def test_release_name_does_not_include_deployment_id():
    project = make_project(42, "demo-app")

    first = helm_release_name(project, make_deployment(deployment_id=7, environment="production"))
    second = helm_release_name(project, make_deployment(deployment_id=8, environment="production"))

    assert first == second
    assert "7" not in first
    assert "8" not in second


def test_all_generated_names_are_dns_safe():
    project = make_project("550e8400-e29b-41d4-a716-446655440000", "A Very_Odd Project!!!")
    deployment = make_deployment(environment="QA Env")

    values = [
        dns_slug(project.name),
        workload_name(project),
        helm_release_name(project, deployment),
    ]

    for value in values:
        assert_dns_safe(value)


def test_labels_include_expected_stable_selectors():
    labels = workload_labels(make_project(42, "demo-app"), make_deployment(environment="production"))

    assert labels == {
        "app.kubernetes.io/managed-by": "autodeploy-control-plane",
        "app.kubernetes.io/instance": "paas-demo-app-production-42",
        "app.kubernetes.io/name": "demo-app",
        "paas.dev/project-id": "42",
        "paas.dev/workload": "paas-demo-app-production-42",
    }


def test_labels_do_not_include_unsafe_long_free_form_project_name():
    long_name = "Very Long Project Name With Spaces " * 5
    labels = workload_labels(make_project(42, long_name), make_deployment())

    assert long_name not in labels.values()
    for value in labels.values():
        assert len(value) <= 63
        assert_dns_safe(value)


def test_environment_is_part_of_release_name():
    project = make_project(42, "demo-app")

    production = helm_release_name(project, make_deployment(environment="production"))
    staging = helm_release_name(project, make_deployment(environment="staging"))

    assert production == "paas-demo-app-production-42"
    assert staging == "paas-demo-app-staging-42"
