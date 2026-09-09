"""Resolve the environment passed to a deployment workload."""

from __future__ import annotations

from copy import deepcopy

from control_plane.deployment_spec import project_for_deployment


RESERVED_WORKLOAD_ENVIRONMENT_NAMES = frozenset({"APP_COMMIT_SHA", "APP_VERSION"})


def resolved_workload_environment(deployment):
    """Return snapshot environment values with platform identity values applied.

    The two identity variables describe the build being deployed. They are owned
    by the platform so a project cannot replace them with stale configuration.
    """
    project = project_for_deployment(deployment)
    environment = [
        deepcopy(item)
        for item in project.env_vars or []
        if not (
            isinstance(item, dict)
            and item.get("name") in RESERVED_WORKLOAD_ENVIRONMENT_NAMES
        )
    ]
    build = deployment.build
    environment.extend(
        [
            {
                "name": "APP_COMMIT_SHA",
                "value_source": "literal",
                "value": str(getattr(build, "commit_sha", "") or ""),
            },
            {
                "name": "APP_VERSION",
                "value_source": "literal",
                "value": str(getattr(build, "image_tag", "") or ""),
            },
        ]
    )
    return environment
