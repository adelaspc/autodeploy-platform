"""Snapshot project settings so queued deployments do not change underneath the worker."""

from copy import deepcopy
from types import SimpleNamespace


DEPLOYMENT_SPEC_VERSION = 1
PROJECT_SPEC_FIELDS = (
    "id",
    "name",
    "repo_url",
    "branch",
    "git_auth_type",
    "git_secret_ref",
    "dockerfile_path",
    "build_context",
    "port",
    "healthcheck_path",
    "env_vars",
    "cpu",
    "memory",
    "runtime",
)


def create_deployment_spec_snapshot(project, *, branch=None):
    """Freeze the project settings that this deployment must use."""
    project_spec = {field: deepcopy(getattr(project, field, None)) for field in PROJECT_SPEC_FIELDS}
    if branch is not None:
        project_spec["branch"] = branch
    return {"version": DEPLOYMENT_SPEC_VERSION, "project": project_spec}


def copy_deployment_spec_snapshot(deployment, *, project_id):
    """Return a validated copy suitable for an exact historical retry."""
    snapshot = getattr(deployment, "spec_snapshot_json", None)
    if not isinstance(snapshot, dict) or snapshot.get("version") != DEPLOYMENT_SPEC_VERSION:
        raise ValueError("Historical deployment specification is unavailable")
    project_spec = snapshot.get("project")
    if not isinstance(project_spec, dict) or any(field not in project_spec for field in PROJECT_SPEC_FIELDS):
        raise ValueError("Historical deployment specification is incomplete")
    if project_spec.get("id") != project_id or getattr(deployment, "project_id", None) != project_id:
        raise ValueError("Historical deployment specification belongs to another project")
    return deepcopy(snapshot)


def project_for_deployment(deployment):
    # Project edits apply to future deployments only. The fallback keeps records
    # created before deployment snapshots usable.
    snapshot = getattr(deployment, "spec_snapshot_json", None)
    if isinstance(snapshot, dict) and snapshot.get("version") == DEPLOYMENT_SPEC_VERSION:
        project_spec = snapshot.get("project")
        if isinstance(project_spec, dict):
            return SimpleNamespace(**deepcopy(project_spec))
    return deployment.project
