import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.test_helm_values import make_deployment
from worker.helm_values import GenericWebAppValuesConfig, generic_web_app_values


REPO_ROOT = Path(__file__).resolve().parents[1]


def helm_binary():
    helm = shutil.which("helm")
    if not helm:
        pytest.skip("helm binary is not available")
    return helm


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


def rendered_docs(rendered):
    return [
        doc
        for doc in yaml.safe_load_all(rendered)
        if isinstance(doc, dict)
    ]


def rendered_role(rendered):
    return next(doc for doc in rendered_docs(rendered) if doc.get("kind") == "Role")


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


def test_control_plane_chart_does_not_mount_docker_socket_by_default():
    rendered = helm_template(
        "control-plane-default",
        "./deploy/helm/paas-control-plane",
    )

    assert "/var/run/docker.sock" not in rendered
    assert "name: docker-socket" not in rendered


def test_local_microk8s_values_explicitly_mount_docker_socket():
    rendered = helm_template(
        "control-plane-local",
        "./deploy/helm/paas-control-plane",
        "-f",
        "./deploy/helm/paas-control-plane/values.local-microk8s.yaml",
    )

    assert "/var/run/docker.sock" in rendered
    assert "name: docker-socket" in rendered


def test_control_plane_rbac_limits_secret_and_configmap_permissions_by_default():
    role = rendered_role(
        helm_template(
            "control-plane-default",
            "./deploy/helm/paas-control-plane",
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
            "./deploy/helm/paas-control-plane",
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
            "./deploy/helm/paas-control-plane",
            "--set",
            "rbac.helmReleaseStorage=true",
        )
    )

    secret_rules = role_rules_for(role, api_group="", resource="secrets")
    assert any({"create", "update", "patch", "delete"} <= set(rule["verbs"]) for rule in secret_rules)
