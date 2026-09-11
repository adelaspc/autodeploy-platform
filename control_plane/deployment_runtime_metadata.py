"""Resolve and persist the immutable runtime identity of a deployment attempt."""


VALID_KUBERNETES_DEPLOYMENT_MODES = {"manifest", "helm"}


def _nonempty_string(value):
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _historical_metadata_value(deployment, key):
    """Recover legacy identity from the earliest event that recorded it."""
    preflight_metadata = getattr(deployment, "preflight_metadata_json", None)
    if isinstance(preflight_metadata, dict):
        value = _nonempty_string(preflight_metadata.get(key))
        if value:
            return value

    events = sorted(
        getattr(deployment, "events", ()) or (),
        key=lambda event: getattr(event, "id", 0) or 0,
    )
    for event in events:
        metadata = getattr(event, "metadata_json", None)
        if isinstance(metadata, dict):
            value = _nonempty_string(metadata.get(key))
            if value:
                return value
    return None


def kubernetes_deployment_mode(deployment):
    """Return the original Kubernetes mode, including for legacy rows."""
    persisted = _nonempty_string(getattr(deployment, "kubernetes_deployment_mode", None))
    if persisted in VALID_KUBERNETES_DEPLOYMENT_MODES:
        return persisted

    if getattr(deployment, "helm_release_name", None):
        return "helm"
    historical = _historical_metadata_value(deployment, "deployment_mode")
    if historical in VALID_KUBERNETES_DEPLOYMENT_MODES:
        return historical
    if getattr(deployment, "deploy_target", None) == "kubernetes":
        # Before the mode was persisted, Kubernetes deployments without Helm
        # release metadata were manifest-managed. Never infer this from current
        # configuration, which may have changed since the deployment ran.
        return "manifest"
    return None


def kubernetes_namespace(deployment):
    """Return the original namespace when it can be recovered safely."""
    return (
        _nonempty_string(getattr(deployment, "kubernetes_namespace", None))
        or _nonempty_string(getattr(deployment, "helm_namespace", None))
        or _historical_metadata_value(deployment, "namespace")
    )


def kubernetes_deployment_name(deployment):
    return _nonempty_string(getattr(deployment, "kubernetes_deployment_name", None)) or _historical_metadata_value(
        deployment, "deployment_name"
    )


def kubernetes_service_name(deployment):
    return _nonempty_string(getattr(deployment, "kubernetes_service_name", None)) or _historical_metadata_value(
        deployment, "service_name"
    )


def kubernetes_ingress_name(deployment):
    return _nonempty_string(getattr(deployment, "kubernetes_ingress_name", None)) or _historical_metadata_value(
        deployment, "ingress_name"
    )


def persist_kubernetes_runtime_identity(
    deployment,
    *,
    deployment_mode,
    namespace,
    deployment_name=None,
    service_name=None,
    ingress_name=None,
):
    """Freeze executor settings before the first Kubernetes side effect."""
    normalized_mode = _nonempty_string(deployment_mode)
    normalized_namespace = _nonempty_string(namespace)
    if normalized_mode not in VALID_KUBERNETES_DEPLOYMENT_MODES:
        raise ValueError("Kubernetes deployment mode must be one of: manifest, helm")
    if not normalized_namespace:
        raise ValueError("Kubernetes namespace must not be empty")
    if not _nonempty_string(getattr(deployment, "kubernetes_deployment_mode", None)):
        deployment.kubernetes_deployment_mode = normalized_mode
    if not _nonempty_string(getattr(deployment, "kubernetes_namespace", None)):
        deployment.kubernetes_namespace = normalized_namespace
    for attribute, value in (
        ("kubernetes_deployment_name", deployment_name),
        ("kubernetes_service_name", service_name),
        ("kubernetes_ingress_name", ingress_name),
    ):
        if _nonempty_string(value) and not _nonempty_string(getattr(deployment, attribute, None)):
            setattr(deployment, attribute, str(value).strip())


def persist_helm_runtime_metadata(deployment, metadata):
    if not isinstance(metadata, dict):
        return
    if metadata.get("deployment_mode") != "helm" and not metadata.get("helm_release_name"):
        return

    release_name = metadata.get("helm_release_name")
    namespace = metadata.get("namespace")
    chart_path = metadata.get("chart_path")

    if release_name:
        deployment.helm_release_name = str(release_name)
    if namespace:
        deployment.helm_namespace = str(namespace)
        if not _nonempty_string(getattr(deployment, "kubernetes_namespace", None)):
            deployment.kubernetes_namespace = str(namespace)
    if chart_path:
        deployment.helm_chart_path = str(chart_path)
    if not _nonempty_string(getattr(deployment, "kubernetes_deployment_mode", None)):
        deployment.kubernetes_deployment_mode = "helm"
