import json
import os
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.kubernetes


def kubectl(*args):
    kubeconfig = os.getenv("CONTROL_PLANE_TEST_KUBECONFIG")
    if not kubeconfig:
        pytest.skip("CONTROL_PLANE_TEST_KUBECONFIG is not configured")
    binary = shutil.which("kubectl")
    if not binary:
        pytest.skip("kubectl is not available")
    namespace = os.getenv("CONTROL_PLANE_TEST_K8S_NAMESPACE", "default")
    return subprocess.run(
        [binary, "--kubeconfig", kubeconfig, "--namespace", namespace, *args],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_kubernetes_api_contract_and_read_permissions():
    version = kubectl("version", "-o", "json")
    assert version.returncode == 0, version.stderr
    version_payload = json.loads(version.stdout)
    assert version_payload["serverVersion"]["major"]
    assert version_payload["serverVersion"]["minor"]

    readiness = kubectl("get", "--raw=/readyz")
    assert readiness.returncode == 0, readiness.stderr
    assert readiness.stdout.strip() == "ok"

    api_resources = kubectl("api-resources", "--api-group=apps", "-o", "name")
    assert api_resources.returncode == 0, api_resources.stderr
    assert "deployments.apps" in api_resources.stdout.splitlines()

    required_permissions = (
        ("get", "configmaps"),
        ("get", "secrets"),
        ("list", "pods"),
        ("get", "pods/log"),
        ("create", "pods/portforward"),
        ("create", "deployments.apps"),
        ("patch", "deployments.apps"),
        ("delete", "deployments.apps"),
        ("create", "services"),
        ("patch", "services"),
        ("delete", "services"),
    )
    for verb, resource in required_permissions:
        permission = kubectl("auth", "can-i", verb, resource)
        assert permission.returncode == 0, permission.stderr
        assert permission.stdout.strip() == "yes", f"missing permission: {verb} {resource}"
