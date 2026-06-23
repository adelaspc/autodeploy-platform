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
    if chart_path:
        deployment.helm_chart_path = str(chart_path)
