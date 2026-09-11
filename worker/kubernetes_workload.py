"""Translate project settings into the shared Kubernetes workload contract."""

from __future__ import annotations


def resource_requests(project):
    """Return optional Kubernetes resource requests from a project snapshot."""
    requests = {}
    cpu = _clean_string(getattr(project, "cpu", None))
    memory = _clean_string(getattr(project, "memory", None))
    if cpu:
        requests["cpu"] = cpu
    if memory:
        requests["memory"] = memory
    return {"requests": requests} if requests else {}


def health_probes(project):
    """Return the readiness and liveness probes shared by Kubernetes modes."""
    path = _clean_string(getattr(project, "healthcheck_path", None)) or "/"
    return {
        "readiness": {
            "enabled": True,
            "httpGet": {"path": path, "port": "http"},
            "initialDelaySeconds": 5,
            "periodSeconds": 10,
        },
        "liveness": {
            "enabled": True,
            "httpGet": {"path": path, "port": "http"},
            "initialDelaySeconds": 15,
            "periodSeconds": 20,
        },
        "startup": {"enabled": False},
    }


def manifest_health_probes(project):
    """Return enabled probes in the shape expected by a Kubernetes container."""
    probes = health_probes(project)
    return {
        "readinessProbe": _enabled_probe(probes["readiness"]),
        "livenessProbe": _enabled_probe(probes["liveness"]),
    }


def _enabled_probe(probe):
    return {key: value for key, value in probe.items() if key != "enabled"}


def _clean_string(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None
