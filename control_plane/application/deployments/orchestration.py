from datetime import datetime, timezone
from pathlib import Path
import base64
import os
import subprocess

from flask import current_app
from sqlalchemy.orm import selectinload

from control_plane.extensions import db
from control_plane.deployment_spec import create_deployment_spec_snapshot, project_for_deployment
from control_plane.models import Build, DeploymentEvent, PlatformDeployment


def create_deployment_event(
    deployment_id,
    event_type,
    status,
    message=None,
    *,
    step=None,
    level="info",
    metadata_json=None,
):
    event = DeploymentEvent(
        deployment_id=deployment_id,
        event_type=event_type,
        step=step,
        level=level,
        status=status,
        message=message,
        metadata_json=metadata_json,
    )
    db.session.add(event)


def now_utc():
    return datetime.now(timezone.utc)


def sanitize_image_component(value):
    sanitized = "".join(char.lower() if char.isalnum() or char in {"-", "_", "."} else "-" for char in value)
    return sanitized.strip("-") or "app"


def registry_prefix_from_config():
    if not current_app.config.get("CONTROL_PLANE_REGISTRY_ENABLED"):
        return None

    registry_url = (current_app.config.get("CONTROL_PLANE_REGISTRY_URL") or "").strip().rstrip("/")
    if not registry_url:
        return None

    registry_namespace = (current_app.config.get("CONTROL_PLANE_REGISTRY_NAMESPACE") or "").strip().strip("/")
    return f"{registry_url}/{registry_namespace}" if registry_namespace else registry_url


def git_auth_environment(project):
    git_auth_type = (getattr(project, "git_auth_type", None) or "none").strip().lower()
    if git_auth_type == "none":
        return None
    if git_auth_type != "token":
        raise ValueError(f"Unsupported git auth type '{git_auth_type}'")

    secret_ref = (getattr(project, "git_secret_ref", None) or "").strip()
    if not secret_ref:
        raise ValueError("git_secret_ref is required when git_auth_type is 'token'")

    token_env_name = f"CONTROL_PLANE_GIT_TOKEN_{secret_ref}"
    token = os.getenv(token_env_name)
    if not token:
        raise ValueError(f"Git token environment variable '{token_env_name}' is not set")

    repo_url = project.repo_url or ""
    if not repo_url.startswith("https://github.com/"):
        raise ValueError("Token-based git auth currently supports only https://github.com/ repository URLs")

    auth_header = "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    env = os.environ.copy()
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": auth_header,
        }
    )
    return env


def resolve_project_commit_sha(project, branch):
    repo_path = Path(project.repo_url)
    if repo_path.exists():
        command = ["git", "-C", str(repo_path), "rev-parse", branch]
        env = None
    else:
        command = ["git", "ls-remote", project.repo_url, f"refs/heads/{branch}"]
        try:
            env = git_auth_environment(project)
        except ValueError as exc:
            raise ValueError(f"Unable to resolve commit for branch '{branch}': {exc}") from exc

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        error_output = (result.stderr or result.stdout).strip() or "Unknown git error"
        raise ValueError(f"Unable to resolve commit for branch '{branch}': {error_output}")

    output = result.stdout.strip()
    if not output:
        raise ValueError(f"Unable to resolve commit for branch '{branch}': no matching ref found")

    commit_sha = output.split()[0].strip()
    if not commit_sha:
        raise ValueError(f"Unable to resolve commit for branch '{branch}': invalid git output")

    return commit_sha


def create_build_and_deployment_records(
    project,
    *,
    commit_sha,
    registry=None,
    image_name=None,
    image_tag=None,
    image_ref=None,
    build_status="pending",
    test_command=None,
    environment="production",
    deployment_status="pending",
    service_url=None,
    deployment_message="Deployment record created",
    deployment_metadata=None,
    deployment_branch=None,
):
    resolved_image_name = image_name or sanitize_image_component(project.name)
    resolved_image_tag = image_tag or sanitize_image_component(commit_sha[:12])
    resolved_registry = registry if registry is not None else registry_prefix_from_config()
    resolved_image_ref = image_ref or (
        f"{resolved_registry}/{resolved_image_name}:{resolved_image_tag}"
        if resolved_registry
        else f"{resolved_image_name}:{resolved_image_tag}"
    )

    build = Build(
        project_id=project.id,
        commit_sha=commit_sha,
        registry=resolved_registry,
        image_name=resolved_image_name,
        image_tag=resolved_image_tag,
        image_ref=resolved_image_ref,
        status=build_status,
        test_command=test_command,
    )
    db.session.add(build)
    db.session.flush()

    deployment = PlatformDeployment(
        project_id=project.id,
        build_id=build.id,
        environment=environment,
        status=deployment_status,
        service_url=service_url,
        spec_snapshot_json=create_deployment_spec_snapshot(project, branch=deployment_branch),
    )
    db.session.add(deployment)
    db.session.flush()

    create_deployment_event(
        deployment.id,
        "deployment.created",
        deployment.status,
        deployment_message,
        step="deployment",
        metadata_json=deployment_metadata,
    )
    return build, deployment


def get_deployment_branch(deployment):
    if deployment.spec_snapshot_json:
        return project_for_deployment(deployment).branch
    for event in deployment.events:
        if event.event_type != "deployment.created":
            continue
        metadata = event.metadata_json or {}
        branch = metadata.get("branch")
        if branch:
            return branch
    return deployment.project.branch


def create_requested_deployment(
    project,
    *,
    branch,
    test_command,
    commit_sha,
    message_prefix,
    deployment_metadata=None,
    extra_events=None,
    commit=True,
):
    build, deployment = create_build_and_deployment_records(
        project,
        commit_sha=commit_sha,
        test_command=test_command,
        deployment_message=f"{message_prefix} for branch '{branch}' at commit '{commit_sha[:12]}'",
        deployment_metadata={"branch": branch, "commit_sha": commit_sha} | (deployment_metadata or {}),
        deployment_branch=branch,
    )
    for event in extra_events or ():
        create_deployment_event(
            deployment.id,
            event["event_type"],
            event.get("status", deployment.status),
            event.get("message"),
            step=event.get("step"),
            level=event.get("level", "info"),
            metadata_json=event.get("metadata_json"),
        )
    if commit:
        db.session.commit()
    return build, deployment, commit_sha


def create_user_facing_deployment(project, *, branch, test_command, message_prefix):
    commit_sha = resolve_project_commit_sha(project, branch)
    return create_requested_deployment(
        project,
        branch=branch,
        test_command=test_command,
        commit_sha=commit_sha,
        message_prefix=message_prefix,
    )


def get_latest_project_deployment_record(project_id):
    return (
        PlatformDeployment.query.options(
            selectinload(PlatformDeployment.project),
            selectinload(PlatformDeployment.build),
            selectinload(PlatformDeployment.events),
        )
        .populate_existing()
        .filter_by(project_id=project_id)
        .order_by(PlatformDeployment.created_at.desc(), PlatformDeployment.id.desc())
        .first()
    )
