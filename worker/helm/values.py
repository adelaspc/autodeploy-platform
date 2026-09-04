"""Translate a deployment snapshot into values for the generic web-app chart."""

from __future__ import annotations

from dataclasses import dataclass

from control_plane.deployment_spec import project_for_deployment
from control_plane.security import env_var_is_secret


@dataclass(frozen=True)
class GenericWebAppValuesConfig:
    image_pull_policy: str = "IfNotPresent"
    image_pull_secret: str | None = None
    ingress_enabled: bool = False
    ingress_host: str = ""
    ingress_class_name: str = ""


def generic_web_app_values(deployment, config: GenericWebAppValuesConfig | None = None):
    config = config or GenericWebAppValuesConfig()
    project = project_for_deployment(deployment)
    build = deployment.build
    repository, tag = _image_repository_and_tag(build)

    values = {
        "image": {
            "repository": repository,
            "tag": tag,
            "pullPolicy": config.image_pull_policy,
            "pullSecrets": _image_pull_secrets(config.image_pull_secret),
        },
        "replicaCount": 1,
        "container": {
            "port": project.port,
        },
        "env": _env_values(project.env_vars),
        "envFrom": {
            "configMaps": [],
            "secrets": [],
        },
        "service": {
            "type": "ClusterIP",
            "port": project.port,
            "targetPort": "",
        },
        "resources": _resource_values(project),
        "probes": _probe_values(project.healthcheck_path),
        "ingress": (
            {
                "enabled": True,
                "className": config.ingress_class_name,
                "host": config.ingress_host,
                "path": "/",
                "tls": [],
            }
            if config.ingress_enabled
            else {"enabled": False}
        ),
    }

    return values


def _image_repository_and_tag(build):
    image_ref = _clean_string(getattr(build, "image_ref", None))
    image_tag = _clean_string(getattr(build, "image_tag", None))
    if image_ref:
        parsed = _split_tagged_image_ref(image_ref)
        if parsed:
            return parsed

    registry = _clean_string(getattr(build, "registry", None))
    image_name = _clean_string(getattr(build, "image_name", None))
    if registry and image_name:
        return f"{registry.rstrip('/')}/{image_name.lstrip('/')}", image_tag or "latest"
    if image_name:
        return image_name, image_tag or "latest"
    if image_ref:
        return image_ref, image_tag or "latest"
    return "", image_tag or "latest"


def _split_tagged_image_ref(image_ref):
    last_slash = image_ref.rfind("/")
    last_colon = image_ref.rfind(":")
    if last_colon <= last_slash or last_colon == len(image_ref) - 1:
        return None
    return image_ref[:last_colon], image_ref[last_colon + 1 :]


def _image_pull_secrets(image_pull_secret):
    secret = _clean_string(image_pull_secret)
    if not secret:
        return []
    return [{"name": secret}]


def _env_values(env_vars):
    rendered = []
    for item in env_vars or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        value_source = item.get("value_source")
        if value_source is None:
            value_source = "literal" if item.get("value") is not None else None

        if value_source == "literal" and item.get("value") is not None:
            if env_var_is_secret(item):
                raise ValueError("generic-web-app values require secret_key_ref for secret env vars")
            rendered.append({"name": str(name), "value": str(item["value"])})
            continue

        if value_source == "configmap_key_ref" and item.get("source_name") and item.get("source_key"):
            rendered.append(
                {
                    "name": str(name),
                    "valueFrom": {
                        "configMapKeyRef": {
                            "name": str(item["source_name"]),
                            "key": str(item["source_key"]),
                        }
                    },
                }
            )
            continue

        if value_source == "secret_key_ref" and item.get("source_name") and item.get("source_key"):
            rendered.append(
                {
                    "name": str(name),
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": str(item["source_name"]),
                            "key": str(item["source_key"]),
                        }
                    },
                }
            )

    return rendered


def _resource_values(project):
    requests = {}
    cpu = _clean_string(getattr(project, "cpu", None))
    memory = _clean_string(getattr(project, "memory", None))
    if cpu:
        requests["cpu"] = cpu
    if memory:
        requests["memory"] = memory
    if not requests:
        return {}
    return {"requests": requests}


def _probe_values(healthcheck_path):
    path = _clean_string(healthcheck_path) or "/"
    return {
        "readiness": {
            "enabled": True,
            "httpGet": {
                "path": path,
                "port": "http",
            },
            "initialDelaySeconds": 5,
            "periodSeconds": 10,
        },
        "liveness": {
            "enabled": True,
            "httpGet": {
                "path": path,
                "port": "http",
            },
            "initialDelaySeconds": 15,
            "periodSeconds": 20,
        },
        "startup": {
            "enabled": False,
        },
    }


def _clean_string(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None
