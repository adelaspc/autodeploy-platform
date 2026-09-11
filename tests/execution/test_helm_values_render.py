import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.execution.test_helm_values import make_deployment
from worker.helm.values import GenericWebAppValuesConfig, generic_web_app_values


REPO_ROOT = Path(__file__).resolve().parents[2]


def helm_binary():
    helm = shutil.which("helm")
    if not helm:
        pytest.skip("helm binary is not available")
    return helm


def test_control_plane_image_bundles_pinned_helm_cli():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert re.search(
        r"^FROM alpine/helm:[^\s@]+@sha256:[0-9a-f]{64} AS helm-cli$",
        dockerfile,
        flags=re.MULTILINE,
    )
    assert "COPY --from=helm-cli /usr/bin/helm /usr/local/bin/helm" in dockerfile
    assert "helm version --short" in dockerfile


def helm_template(*args):
    result = subprocess.run(
        [helm_binary(), "template", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def helm_template_failure(*args):
    result = subprocess.run(
        [helm_binary(), "template", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    return result.stderr


def rendered_docs(rendered):
    return [
        doc
        for doc in yaml.safe_load_all(rendered)
        if isinstance(doc, dict)
    ]


def rendered_role(rendered):
    return next(doc for doc in rendered_docs(rendered) if doc.get("kind") == "Role")


def runtime_containers(docs):
    deployments = {
        doc["metadata"]["name"].rsplit("-", 1)[-1]: doc["spec"]["template"]["spec"][
            "containers"
        ][0]
        for doc in docs
        if doc.get("kind") == "Deployment"
        and doc["metadata"]["name"].endswith(("-api", "-worker"))
    }
    reconciler = next(doc for doc in docs if doc.get("kind") == "CronJob")
    migration = next(doc for doc in docs if doc.get("kind") == "Job")
    return {
        **deployments,
        "reconciler": reconciler["spec"]["jobTemplate"]["spec"]["template"]["spec"][
            "containers"
        ][0],
        "migrate": migration["spec"]["template"]["spec"]["containers"][0],
    }


def secret_env_names(container):
    return {
        item["name"]
        for item in container.get("env", [])
        if "secretKeyRef" in item.get("valueFrom", {})
    }


def secret_ref_names(container):
    return {
        item["valueFrom"]["secretKeyRef"]["name"]
        for item in container.get("env", [])
        if "secretKeyRef" in item.get("valueFrom", {})
    }


def literal_env(container):
    return {
        item["name"]: item["value"]
        for item in container.get("env", [])
        if "value" in item
    }


def role_rules_for(role, *, api_group, resource):
    return [
        rule
        for rule in role.get("rules", [])
        if api_group in (rule.get("apiGroups") or [])
        and resource in (rule.get("resources") or [])
    ]


def test_generated_generic_web_app_values_render_with_helm(tmp_path):
    helm = helm_binary()

    deployment = make_deployment(
        image_ref="localhost:32000/my-app:dev",
        port=3000,
        healthcheck_path="/health",
        env_vars=[
            {"name": "APP_ENV", "value": "production"},
            {
                "name": "CONFIG_VALUE",
                "value_source": "configmap_key_ref",
                "source_name": "app-config",
                "source_key": "config-value",
            },
            {
                "name": "DATABASE_URL",
                "value_source": "secret_key_ref",
                "source_name": "app-secret",
                "source_key": "database-url",
            },
        ],
    )
    values = generic_web_app_values(
        deployment,
        GenericWebAppValuesConfig(image_pull_secret="registry-pull-secret"),
    )
    values_path = tmp_path / "generated-values.yaml"
    values_path.write_text(json.dumps(values), encoding="utf-8")

    result = subprocess.run(
        [
            helm,
            "template",
            "generic-render-test",
            "./deploy/helm/generic-web-app",
            "-f",
            str(values_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    rendered = result.stdout
    assert "kind: Deployment" in rendered
    assert "kind: Service" in rendered
    assert 'image: "localhost:32000/my-app:dev"' in rendered
    assert "containerPort: 3000" in rendered
    assert "port: 3000" in rendered
    assert "targetPort: 3000" in rendered
    assert "name: APP_ENV" in rendered
    assert "value: production" in rendered
    assert "configMapKeyRef:" in rendered
    assert "name: app-config" in rendered
    assert "key: config-value" in rendered
    assert "secretKeyRef:" in rendered
    assert "name: app-secret" in rendered
    assert "key: database-url" in rendered
    assert "readinessProbe:" in rendered
    assert "livenessProbe:" in rendered
    assert "path: /health" in rendered
    assert "imagePullSecrets:" in rendered
    assert "name: registry-pull-secret" in rendered
    assert "automountServiceAccountToken: false" in rendered
    assert "type: RuntimeDefault" in rendered
    assert "allowPrivilegeEscalation: false" in rendered


def test_control_plane_chart_does_not_mount_docker_socket_by_default():
    rendered = helm_template(
        "control-plane-default",
        "./deploy/helm/autodeploy-control-plane",
    )

    assert "/var/run/docker.sock" not in rendered
    assert "name: docker-socket" not in rendered


def test_control_plane_chart_uses_dependency_aware_readiness_probes():
    docs = rendered_docs(
        helm_template(
            "control-plane-readiness",
            "./deploy/helm/autodeploy-control-plane",
        )
    )
    api = next(
        doc
        for doc in docs
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"].endswith("-api")
    )
    worker = next(
        doc
        for doc in docs
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"].endswith("-worker")
    )
    api_container = api["spec"]["template"]["spec"]["containers"][0]
    worker_container = worker["spec"]["template"]["spec"]["containers"][0]

    assert api_container["livenessProbe"]["httpGet"]["path"] == "/health"
    assert api_container["readinessProbe"]["httpGet"]["path"] == "/health/ready"
    assert worker_container["livenessProbe"]["exec"]["command"][-1] == "import os; os.kill(1, 0)"
    assert worker_container["readinessProbe"]["exec"]["command"][-1] == "check-worker-readiness"


def test_control_plane_chart_creates_and_references_runtime_secret_by_default():
    rendered = helm_template(
        "control-plane-secret",
        "./deploy/helm/autodeploy-control-plane",
        "--set-string",
        "secrets.values.CONTROL_PLANE_DATABASE_URL=sqlite:////tmp/control-plane.db",
    )
    docs = rendered_docs(rendered)
    secret = next(doc for doc in docs if doc.get("kind") == "Secret")
    config_map = next(doc for doc in docs if doc.get("kind") == "ConfigMap")
    containers = runtime_containers(docs)

    assert secret["metadata"]["name"] == "control-plane-secret-autodeploy-control-plane-secret"
    assert secret["stringData"]["CONTROL_PLANE_DATABASE_URL"] == "sqlite:////tmp/control-plane.db"
    assert "helm.sh/hook" not in secret["metadata"].get("annotations", {})
    assert "helm.sh/hook" not in config_map["metadata"].get("annotations", {})
    assert all(
        "CONTROL_PLANE_DATABASE_URL" in secret_env_names(container)
        for container in containers.values()
    )
    assert all(
        secret_ref_names(container)
        == {"control-plane-secret-autodeploy-control-plane-secret"}
        for container in containers.values()
    )
    assert "envFrom" not in containers["migrate"]


def test_control_plane_chart_references_external_runtime_secret_without_creating_it():
    rendered = helm_template(
        "control-plane-external-secret",
        "./deploy/helm/autodeploy-control-plane",
        "--set",
        "secrets.create=false",
        "--set",
        "secrets.existingSecret=externally-managed-runtime",
    )
    docs = rendered_docs(rendered)
    containers = runtime_containers(docs)

    assert not any(doc.get("kind") == "Secret" for doc in docs)
    assert all(
        "CONTROL_PLANE_DATABASE_URL" in secret_env_names(container)
        for container in containers.values()
    )
    assert all(
        secret_ref_names(container) == {"externally-managed-runtime"}
        for container in containers.values()
    )
    assert all(
        not any("secretRef" in source for source in container.get("envFrom", []))
        for container in containers.values()
    )


def test_control_plane_chart_scopes_secret_keys_by_component():
    settings = []
    secret_keys = {
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
    for key in sorted(secret_keys):
        settings.extend(("--set-string", f"secrets.values.{key}=test-value"))

    containers = runtime_containers(
        rendered_docs(
            helm_template(
                "control-plane-scoped-secrets",
                "./deploy/helm/autodeploy-control-plane",
                *settings,
            )
        )
    )

    assert secret_env_names(containers["api"]) == {
        "CONTROL_PLANE_DATABASE_URL",
        "CONTROL_PLANE_GITHUB_WEBHOOK_SECRET",
        "CONTROL_PLANE_API_TOKEN_READ_ONLY",
        "CONTROL_PLANE_API_TOKEN_DEPLOYER",
        "CONTROL_PLANE_API_TOKEN_ADMIN",
        "CONTROL_PLANE_API_TOKENS_JSON",
        "CONTROL_PLANE_METRICS_TOKEN",
        "CONTROL_PLANE_GIT_TOKEN_GITHUB",
    }
    assert secret_env_names(containers["worker"]) == {
        "CONTROL_PLANE_DATABASE_URL",
        "CONTROL_PLANE_REGISTRY_USERNAME",
        "CONTROL_PLANE_REGISTRY_PASSWORD",
        "CONTROL_PLANE_GIT_TOKEN_GITHUB",
    }
    assert secret_env_names(containers["reconciler"]) == {
        "CONTROL_PLANE_DATABASE_URL"
    }
    assert secret_env_names(containers["migrate"]) == {"CONTROL_PLANE_DATABASE_URL"}
    assert {
        name: literal_env(container)["CONTROL_PLANE_COMPONENT"]
        for name, container in containers.items()
    } == {name: name for name in containers}


def test_control_plane_chart_selects_external_git_token_for_api_and_worker():
    containers = runtime_containers(
        rendered_docs(
            helm_template(
                "control-plane-external-git-token",
                "./deploy/helm/autodeploy-control-plane",
                "--set",
                "secrets.create=false",
                "--set",
                "secrets.existingSecret=externally-managed-runtime",
                "--set",
                "secrets.gitTokenKeys[0]=CONTROL_PLANE_GIT_TOKEN_CUSTOM",
            )
        )
    )

    assert "CONTROL_PLANE_GIT_TOKEN_CUSTOM" in secret_env_names(containers["api"])
    assert "CONTROL_PLANE_GIT_TOKEN_CUSTOM" in secret_env_names(containers["worker"])
    assert "CONTROL_PLANE_GIT_TOKEN_CUSTOM" not in secret_env_names(
        containers["reconciler"]
    )
    assert "CONTROL_PLANE_GIT_TOKEN_CUSTOM" not in secret_env_names(containers["migrate"])


def test_control_plane_chart_rejects_invalid_git_token_key():
    stderr = helm_template_failure(
        "control-plane-invalid-git-token",
        "./deploy/helm/autodeploy-control-plane",
        "--set",
        "secrets.gitTokenKeys[0]=GITHUB_TOKEN",
    )

    assert 'invalid secrets.gitTokenKeys entry "GITHUB_TOKEN"' in stderr


@pytest.mark.parametrize(
    ("settings", "expected_error"),
    [
        (
            ("--set", "secrets.existingSecret=externally-managed-runtime"),
            "secrets.create=true and secrets.existingSecret cannot be used together",
        ),
        (
            ("--set", "secrets.create=false"),
            "secrets.existingSecret is required when secrets.create=false",
        ),
    ],
)
def test_control_plane_chart_rejects_ambiguous_runtime_secret_modes(settings, expected_error):
    stderr = helm_template_failure(
        "control-plane-invalid-secret",
        "./deploy/helm/autodeploy-control-plane",
        *settings,
    )

    assert expected_error in stderr


def test_local_microk8s_fake_values_do_not_mount_docker_socket():
    rendered = helm_template(
        "control-plane-local",
        "./deploy/helm/autodeploy-control-plane",
        "-f",
        "./deploy/helm/autodeploy-control-plane/values.local-microk8s.yaml",
    )

    assert "/var/run/docker.sock" not in rendered
    assert "name: docker-socket" not in rendered


def test_control_plane_chart_adds_docker_socket_group_to_runtime_pods():
    rendered = helm_template(
        "control-plane-docker-socket",
        "./deploy/helm/autodeploy-control-plane",
        "--set",
        "dockerSocket.enabled=true",
        "--set-string",
        "dockerSocket.groupId=998",
    )
    docs = rendered_docs(rendered)
    worker = next(
        doc
        for doc in docs
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"].endswith("-worker")
    )
    reconciler = next(doc for doc in docs if doc.get("kind") == "CronJob")

    assert worker["spec"]["template"]["spec"]["securityContext"]["supplementalGroups"] == [998]
    assert reconciler["spec"]["jobTemplate"]["spec"]["template"]["spec"]["securityContext"][
        "supplementalGroups"
    ] == [998]


def test_control_plane_chart_requires_group_id_when_docker_socket_is_enabled():
    stderr = helm_template_failure(
        "control-plane-docker-socket",
        "./deploy/helm/autodeploy-control-plane",
        "--set",
        "dockerSocket.enabled=true",
    )

    assert "dockerSocket.groupId is required when dockerSocket.enabled=true" in stderr


def test_control_plane_chart_rejects_cross_namespace_in_cluster_executor():
    stderr = helm_template_failure(
        "control-plane-namespace",
        "./deploy/helm/autodeploy-control-plane",
        "--namespace",
        "paas-local",
        "--set",
        "config.CONTROL_PLANE_EXECUTOR=kubernetes",
        "--set",
        "config.CONTROL_PLANE_K8S_NAMESPACE=default",
    )

    assert "must match the Helm release namespace paas-local" in stderr


def test_control_plane_chart_allows_external_kubeconfig_for_another_namespace():
    rendered = helm_template(
        "control-plane-namespace",
        "./deploy/helm/autodeploy-control-plane",
        "--namespace",
        "paas-local",
        "--set",
        "config.CONTROL_PLANE_EXECUTOR=kubernetes",
        "--set",
        "config.CONTROL_PLANE_K8S_NAMESPACE=default",
        "--set",
        "kubeconfig.existingSecret=external-kubeconfig",
    )

    assert "secretName: external-kubeconfig" in rendered


def test_control_plane_rbac_limits_secret_and_configmap_permissions_by_default():
    role = rendered_role(
        helm_template(
            "control-plane-default",
            "./deploy/helm/autodeploy-control-plane",
        )
    )

    for resource in ("secrets", "configmaps"):
        rules = role_rules_for(role, api_group="", resource=resource)
        assert rules
        assert all(set(rule["verbs"]) <= {"get"} for rule in rules)


def test_control_plane_rbac_uses_narrow_workload_permissions_by_default():
    role = rendered_role(
        helm_template(
            "control-plane-default",
            "./deploy/helm/autodeploy-control-plane",
        )
    )

    pods_rules = role_rules_for(role, api_group="", resource="pods")
    assert pods_rules
    assert all(set(rule["verbs"]) <= {"get", "list"} for rule in pods_rules)

    pod_logs_rules = role_rules_for(role, api_group="", resource="pods/log")
    assert pod_logs_rules
    assert all(set(rule["verbs"]) <= {"get"} for rule in pod_logs_rules)

    port_forward_rules = role_rules_for(role, api_group="", resource="pods/portforward")
    assert port_forward_rules
    assert all(set(rule["verbs"]) <= {"create"} for rule in port_forward_rules)

    for api_group, resource in (
        ("", "services"),
        ("apps", "deployments"),
        ("networking.k8s.io", "ingresses"),
    ):
        rules = role_rules_for(role, api_group=api_group, resource=resource)
        assert rules
        assert all("watch" not in rule["verbs"] for rule in rules)


def test_control_plane_rbac_adds_secret_mutation_only_for_helm_release_storage():
    role = rendered_role(
        helm_template(
            "control-plane-helm-storage",
            "./deploy/helm/autodeploy-control-plane",
            "--set",
            "rbac.helmReleaseStorage=true",
        )
    )

    secret_rules = role_rules_for(role, api_group="", resource="secrets")
    assert any({"create", "update", "patch", "delete"} <= set(rule["verbs"]) for rule in secret_rules)
