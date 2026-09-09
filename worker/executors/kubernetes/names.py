"""Derive stable, Kubernetes-safe names for project runtime resources."""

import re


MAX_DNS_LABEL_LENGTH = 63
MAX_HELM_RELEASE_LENGTH = 53
MANIFEST_SERVICE_SUFFIX = "-svc"


def dns_slug(value, *, fallback="app", max_length=MAX_DNS_LABEL_LENGTH):
    slug = re.sub(r"[^a-z0-9-]+", "-", str(value or "").lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug:
        slug = fallback
    return slug[:max_length].rstrip("-") or fallback[:max_length]


def workload_name(project, *, max_length=MAX_DNS_LABEL_LENGTH):
    return dns_slug(getattr(project, "name", None), max_length=max_length)


def manifest_deployment_name(project, deployment, *, max_length=MAX_DNS_LABEL_LENGTH):
    """Return an attempt-specific name with room for the Service suffix."""
    deployment_suffix = project_id_suffix(getattr(deployment, "id", None), max_length=20)
    base_max_length = max_length - len(MANIFEST_SERVICE_SUFFIX)
    reserved = len("paas") + len(deployment_suffix) + 2
    project_max_length = max(1, base_max_length - reserved)
    project_slug = workload_name(project, max_length=project_max_length)
    return f"paas-{project_slug}-{deployment_suffix}"


def manifest_service_name(project, deployment, *, max_length=MAX_DNS_LABEL_LENGTH):
    deployment_name = manifest_deployment_name(project, deployment, max_length=max_length)
    return f"{deployment_name}{MANIFEST_SERVICE_SUFFIX}"


def helm_release_name(project, deployment=None, *, max_length=MAX_HELM_RELEASE_LENGTH):
    project_slug = workload_name(project)
    environment_slug = dns_slug(getattr(deployment, "environment", None), fallback="production")
    suffix = project_id_suffix(getattr(project, "id", None))
    reserved = len("paas") + len(environment_slug) + len(suffix) + 3
    project_max_length = max(1, max_length - reserved)
    project_slug = dns_slug(project_slug, max_length=project_max_length)
    return f"paas-{project_slug}-{environment_slug}-{suffix}"[:max_length].rstrip("-")


def workload_labels(project, deployment=None):
    release_name = helm_release_name(project, deployment)
    return {
        "app.kubernetes.io/managed-by": "autodeploy-control-plane",
        "app.kubernetes.io/instance": release_name,
        "app.kubernetes.io/name": workload_name(project),
        "paas.dev/project-id": project_id_suffix(getattr(project, "id", None), max_length=MAX_DNS_LABEL_LENGTH),
        "paas.dev/workload": release_name,
    }


def project_id_suffix(project_id, *, max_length=12):
    raw = re.sub(r"[^a-z0-9]+", "", str(project_id or "").lower())
    return (raw[:max_length] or "unknown").rstrip("-")
