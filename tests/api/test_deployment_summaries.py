from pathlib import Path

from control_plane.application.deployments import orchestration as deployment_orchestration_api
from control_plane.extensions import db
from control_plane.models import DeploymentEvent, PlatformDeployment
from tests.api.project_test_helpers import create_project


def test_get_deployment_summary_returns_successful_view(client, app, monkeypatch, tmp_path):
    project_response = create_project(client, name="summary-app")
    project_id = project_response.get_json()["id"]
    monkeypatch.setattr(
        deployment_orchestration_api, "resolve_project_commit_sha", lambda project, branch: "0123456789abcdef"
    )
    deploy_response = client.post(f"/api/projects/{project_id}/deploy", json={"branch": "release"})
    deployment_id = deploy_response.get_json()["deployment_id"]

    runtime_log_path = tmp_path / "project-1" / "deployment-1" / "logs" / "runtime.log"
    runtime_log_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_log_path.write_text("ready\n", encoding="utf-8")

    with app.app_context():
        app.config["CONTROL_PLANE_WORKSPACE_ROOT"] = str(tmp_path)
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.status = "running"
        deployment.deploy_target = "local-docker"
        deployment.service_url = "http://127.0.0.1:18080"
        deployment.started_at = deployment.created_at
        deployment.build.status = "succeeded"
        deployment.build.registry_push_status = "succeeded"
        deployment.build.log_path = str(tmp_path / "project-1" / "deployment-1" / "logs" / "build.log")
        Path(deployment.build.log_path).write_text("build ok\n", encoding="utf-8")
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="deployment.apply_succeeded",
                step="deploy",
                level="info",
                status="deploying",
                message="Container started and passed healthcheck.",
                metadata_json={"runtime_log_path": str(runtime_log_path)},
            )
        )
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="image.push_succeeded",
                step="image.push",
                level="info",
                status="succeeded",
                message="Image pushed successfully",
                metadata_json={
                    "step": "image.push",
                    "success": True,
                    "push_log_available": True,
                    "push_summary": "Image pushed successfully",
                },
            )
        )
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="deployment.running",
                step="deployment",
                level="info",
                status="running",
                message="Deployment is now running",
                metadata_json={"service_url": "http://127.0.0.1:18080"},
            )
        )
        db.session.commit()

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/summary")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["deployment_id"] == deployment_id
    assert payload["deployment_status"] == "running"
    assert payload["build_status"] == "succeeded"
    assert payload["current_step"] == "deployment"
    assert payload["last_meaningful_event"]["event_type"] == "deployment.running"
    assert payload["last_error"] is None
    assert payload["branch"] == "release"
    assert payload["commit_sha"] == "0123456789abcdef"
    assert payload["image_tag"] == "0123456789ab"
    assert payload["image_ref"] == "summary-app:0123456789ab"
    assert payload["registry_push_status"] == "succeeded"
    assert payload["push_log_available"] is True
    assert payload["last_push_error_summary"] == "Image pushed successfully"
    assert payload["service_url"] == "http://127.0.0.1:18080"
    assert payload["deploy_target"] == "local-docker"
    assert payload["build_log_available"] is True
    assert payload["runtime_log_available"] is True
    assert payload["started_at"] is not None
    assert payload["created_at"] is not None
    assert payload["updated_at"] is not None
    assert len(payload["events"]) >= 3


def test_get_deployment_summary_for_failed_deployment_includes_error_and_step(client, app):
    project_response = create_project(client, name="failed-summary-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": "abc123def456",
            "status": "failed",
            "build_status": "failed",
            "message": "Deployment failed",
        },
    )
    deployment_id = deployment_response.get_json()["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.last_error = "Healthcheck did not succeed within 30 seconds"
        deployment.build.last_error = "Healthcheck did not succeed within 30 seconds"
        deployment.build.registry_push_status = "failed"
        deployment.build.log_path = "/tmp/paas-workspaces/project-1/deployment-1/logs/deploy.log"
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="image.push.failed",
                step="image.push",
                level="error",
                status="failed",
                message="Registry push failed",
                metadata_json={
                    "log_path": deployment.build.log_path,
                    "step": "image.push",
                    "success": False,
                    "push_log_available": True,
                    "push_summary": "Registry push failed",
                    "error_message": "Registry push failed",
                },
            )
        )
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="deployment.failed",
                step="deployment",
                level="error",
                status="failed",
                message="Healthcheck did not succeed within 30 seconds",
                metadata_json={"log_path": deployment.build.log_path},
            )
        )
        db.session.commit()

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/summary")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["deployment_status"] == "failed"
    assert payload["build_status"] == "failed"
    assert payload["current_step"] == "deployment"
    assert payload["last_meaningful_event"]["event_type"] == "deployment.failed"
    assert payload["last_error"] == "Healthcheck did not succeed within 30 seconds"
    assert payload["registry_push_status"] == "failed"
    assert payload["push_log_available"] is True
    assert payload["last_push_error_summary"] == "Registry push failed"


def test_get_deployment_summary_returns_404_for_missing_deployment(client):
    project_response = create_project(client, name="missing-summary-app")
    project_id = project_response.get_json()["id"]

    response = client.get(f"/api/projects/{project_id}/deployments/999/summary")

    assert response.status_code == 404


def test_get_deployment_summary_does_not_expose_other_project_deployment(client):
    first_project_response = create_project(client, name="summary-owner-app")
    first_project_id = first_project_response.get_json()["id"]
    second_project_response = create_project(client, name="summary-other-app")
    second_project_id = second_project_response.get_json()["id"]

    deployment_response = client.post(
        f"/api/projects/{first_project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "pending"},
    )
    deployment_id = deployment_response.get_json()["id"]

    response = client.get(f"/api/projects/{second_project_id}/deployments/{deployment_id}/summary")

    assert response.status_code == 404


def test_get_deployment_summary_includes_kubernetes_runtime_metadata(client, app):
    project_response = create_project(client, name="k8s-summary-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "running", "build_status": "succeeded"},
    )
    deployment_id = deployment_response.get_json()["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        deployment.service_url = "http://paas-k8s-summary-app-1-svc.default.svc.cluster.local:5000"
        deployment.build.registry_push_status = "succeeded"
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="kubernetes.healthcheck_succeeded",
                step="deploy.kubernetes.healthcheck",
                level="info",
                status="deploying",
                message="Kubernetes Service passed healthcheck",
                metadata_json={
                    "namespace": "default",
                    "deployment_name": "paas-k8s-summary-app-1",
                    "service_name": "paas-k8s-summary-app-1-svc",
                    "service_url": deployment.service_url,
                    "healthcheck_url": f"{deployment.service_url}/health",
                },
            )
        )
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="deployment.running",
                step="deployment",
                level="info",
                status="running",
                message="Deployment is now running",
                metadata_json={"service_url": deployment.service_url, "deploy_target": "kubernetes"},
            )
        )
        db.session.commit()

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/summary")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["deploy_target"] == "kubernetes"
    assert payload["helm_release_name"] is None
    assert payload["helm_namespace"] is None
    assert payload["helm_chart_path"] is None
    assert payload["kubernetes_namespace"] == "default"
    assert payload["kubernetes_deployment_name"] == "paas-k8s-summary-app-1"
    assert payload["kubernetes_service_name"] == "paas-k8s-summary-app-1-svc"
    assert payload["last_kubernetes_failure_stage"] is None
    assert payload["last_kubernetes_failure_summary"] is None


def test_get_deployment_summary_includes_persisted_helm_runtime_metadata(client, app):
    project_response = create_project(client, name="helm-summary-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "running", "build_status": "succeeded"},
    )
    deployment_id = deployment_response.get_json()["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        deployment.service_url = "http://paas-helm-summary-app-production-1-generic-web-app.apps.svc.cluster.local:5000"
        deployment.helm_release_name = "paas-helm-summary-app-production-1"
        deployment.helm_namespace = "apps"
        deployment.helm_chart_path = "deploy/helm/generic-web-app"
        db.session.commit()

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/summary")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["helm_release_name"] == "paas-helm-summary-app-production-1"
    assert payload["helm_namespace"] == "apps"
    assert payload["helm_chart_path"] == "deploy/helm/generic-web-app"
    assert payload["kubernetes_namespace"] == "apps"


def test_get_deployment_summary_includes_last_kubernetes_failure_context(client, app):
    project_response = create_project(client, name="k8s-summary-failure-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "failed", "build_status": "failed"},
    )
    deployment_id = deployment_response.get_json()["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        deployment.last_error = "Healthcheck did not succeed within 30 seconds"
        deployment.build.last_error = deployment.last_error
        deployment.build.registry_push_status = "succeeded"
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="kubernetes.healthcheck_failed",
                step="deploy.kubernetes.healthcheck",
                level="error",
                status="failed",
                message="Healthcheck did not succeed within 30 seconds",
                metadata_json={
                    "namespace": "default",
                    "deployment_name": "paas-k8s-summary-failure-app-1",
                    "service_name": "paas-k8s-summary-failure-app-1-svc",
                    "healthcheck_service_summary": "Endpoints: <none> | Session Affinity: None",
                    "healthcheck_deployment_summary": "Conditions: | Available  True",
                },
            )
        )
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="deployment.failed",
                step="deployment",
                level="error",
                status="failed",
                message=deployment.last_error,
                metadata_json={"deploy_target": "kubernetes"},
            )
        )
        db.session.commit()

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/summary")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["deploy_target"] == "kubernetes"
    assert payload["kubernetes_namespace"] == "default"
    assert payload["kubernetes_deployment_name"] == "paas-k8s-summary-failure-app-1"
    assert payload["kubernetes_service_name"] == "paas-k8s-summary-failure-app-1-svc"
    assert payload["last_kubernetes_failure_stage"] == "healthcheck"
    assert payload["last_kubernetes_failure_summary"] == "Endpoints: <none> | Session Affinity: None"


def test_get_deployment_summary_includes_kubernetes_preflight_failure_context(client, app):
    project_response = create_project(client, name="k8s-summary-preflight-app")
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={"commit_sha": "abc123def456", "status": "failed", "build_status": "failed"},
    )
    deployment_id = deployment_response.get_json()["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.deploy_target = "kubernetes"
        deployment.last_error = "Missing Kubernetes referenced resources"
        deployment.build.last_error = deployment.last_error
        deployment.build.registry_push_status = "succeeded"
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="kubernetes.preflight_failed",
                step="deploy.kubernetes.preflight",
                level="error",
                status="failed",
                message="Missing Kubernetes referenced resources: ConfigMap/my-app-config, Secret/dockerhub-pull-secret",
                metadata_json={
                    "namespace": "default",
                    "missing_resources": [
                        {"kind": "ConfigMap", "name": "my-app-config"},
                        {"kind": "Secret", "name": "dockerhub-pull-secret", "usage": "image_pull_secret"},
                    ],
                },
            )
        )
        db.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type="deployment.failed",
                step="deployment",
                level="error",
                status="failed",
                message=deployment.last_error,
                metadata_json={"deploy_target": "kubernetes"},
            )
        )
        db.session.commit()

    response = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}/summary")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["deploy_target"] == "kubernetes"
    assert payload["kubernetes_namespace"] == "default"
    assert payload["last_kubernetes_failure_stage"] == "preflight"
    assert payload["last_kubernetes_failure_summary"] == "ConfigMap/my-app-config, Secret/dockerhub-pull-secret"
    assert payload["last_kubernetes_failure_missing_resources"] == [
        {"kind": "ConfigMap", "name": "my-app-config"},
        {"kind": "Secret", "name": "dockerhub-pull-secret", "usage": "image_pull_secret"},
    ]
