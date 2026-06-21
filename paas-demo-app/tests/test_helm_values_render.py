import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_helm_values import make_deployment
from worker.helm_values import GenericWebAppValuesConfig, generic_web_app_values


def test_generated_generic_web_app_values_render_with_helm(tmp_path):
    helm = shutil.which("helm")
    if not helm:
        pytest.skip("helm binary is not available")

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
        cwd=Path(__file__).resolve().parents[1],
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
