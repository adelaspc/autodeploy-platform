from flask import current_app


def _executor_types():
    from worker.executors.fake import FakeDeploymentExecutor
    from worker.executors.kubernetes.executor import KubernetesExecutor
    from worker.executors.local_docker import LocalDockerExecutor

    return {
        "fake": FakeDeploymentExecutor,
        "local-docker": LocalDockerExecutor,
        "kubernetes": KubernetesExecutor,
    }


def executor_contract_for_name(executor_name):
    normalized = (executor_name or "fake").strip().lower()
    executor_type = _executor_types().get(normalized)
    if executor_type is None:
        raise RuntimeError(f"Unsupported executor '{normalized}'")
    return executor_type.contract_spec()


def _common_settings():
    return {
        "workspace_root": current_app.config.get("CONTROL_PLANE_WORKSPACE_ROOT", "/tmp/paas-workspaces"),
        "command_timeout": current_app.config.get("CONTROL_PLANE_COMMAND_TIMEOUT_SECONDS", 600),
        "retry_count": current_app.config.get("CONTROL_PLANE_COMMAND_RETRY_COUNT", 1),
        "registry_enabled": current_app.config.get("CONTROL_PLANE_REGISTRY_ENABLED", False),
        "registry_url": current_app.config.get("CONTROL_PLANE_REGISTRY_URL"),
        "registry_namespace": current_app.config.get("CONTROL_PLANE_REGISTRY_NAMESPACE"),
        "registry_username": current_app.config.get("CONTROL_PLANE_REGISTRY_USERNAME"),
        "registry_password": current_app.config.get("CONTROL_PLANE_REGISTRY_PASSWORD"),
        "deploy_host": current_app.config.get("CONTROL_PLANE_DEPLOY_HOST", "127.0.0.1"),
        "healthcheck_timeout": current_app.config.get("CONTROL_PLANE_HEALTHCHECK_TIMEOUT_SECONDS", 30),
        "healthcheck_interval": current_app.config.get("CONTROL_PLANE_HEALTHCHECK_INTERVAL_SECONDS", 1),
        "heartbeat_interval": current_app.config.get("CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS", 30),
    }


def _build_local_docker_executor():
    from worker.executors.local_docker import LocalDockerExecutor

    return LocalDockerExecutor(**_common_settings())


def _build_kubernetes_executor():
    from worker.executors.kubernetes.executor import KubernetesExecutor

    return KubernetesExecutor(
        **_common_settings(),
        kubeconfig=current_app.config.get("CONTROL_PLANE_KUBECONFIG"),
        namespace=current_app.config.get("CONTROL_PLANE_K8S_NAMESPACE", "default"),
        image_pull_secret=current_app.config.get("CONTROL_PLANE_K8S_IMAGE_PULL_SECRET"),
        deployment_mode=current_app.config.get("CONTROL_PLANE_K8S_DEPLOYMENT_MODE", "manifest"),
        helm_chart_path=current_app.config.get("CONTROL_PLANE_K8S_HELM_CHART_PATH", "deploy/helm/generic-web-app"),
        helm_binary=current_app.config.get("CONTROL_PLANE_K8S_HELM_BINARY", "helm"),
        helm_timeout=current_app.config.get("CONTROL_PLANE_K8S_HELM_TIMEOUT", "180s"),
        ingress_enabled=current_app.config.get("CONTROL_PLANE_K8S_INGRESS_ENABLED", False),
        ingress_class_name=current_app.config.get("CONTROL_PLANE_K8S_INGRESS_CLASS_NAME", ""),
        ingress_base_domain=current_app.config.get("CONTROL_PLANE_K8S_INGRESS_BASE_DOMAIN", "127.0.0.1.nip.io"),
        rollout_timeout=current_app.config.get("CONTROL_PLANE_K8S_ROLLOUT_TIMEOUT_SECONDS", 120),
    )


def create_executor():
    return _create_executor_for_name(current_app.config.get("CONTROL_PLANE_EXECUTOR", "fake"))


def create_executor_for_deployment(deployment):
    # Prefer the recorded target when revisiting existing work. Configuration may
    # have changed since the deployment was originally created.
    return _create_executor_for_name(deployment.deploy_target or current_app.config.get("CONTROL_PLANE_EXECUTOR", "fake"))


def _create_executor_for_name(executor_name):
    normalized = executor_name.strip().lower()
    if normalized == "local-docker":
        return _build_local_docker_executor()
    if normalized == "kubernetes":
        return _build_kubernetes_executor()
    if normalized == "fake":
        return _executor_types()["fake"]()
    raise RuntimeError(f"Unsupported executor '{normalized}'")
