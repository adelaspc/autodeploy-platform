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
    """Freeze the project settings that a queued deployment must use."""
    project_spec = {field: deepcopy(getattr(project, field, None)) for field in PROJECT_SPEC_FIELDS}
    if branch is not None:
        project_spec["branch"] = branch
    return {"version": DEPLOYMENT_SPEC_VERSION, "project": project_spec}


def project_for_deployment(deployment):
    # Project edits apply to future deployments only. Older records without a
    # snapshot keep the fallback for compatibility with pre-migration data.
    snapshot = getattr(deployment, "spec_snapshot_json", None)
    if isinstance(snapshot, dict) and snapshot.get("version") == DEPLOYMENT_SPEC_VERSION:
        project_spec = snapshot.get("project")
        if isinstance(project_spec, dict):
            return SimpleNamespace(**deepcopy(project_spec))
    return deployment.project
