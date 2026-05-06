from datetime import datetime, timezone
from collections import deque
from pathlib import Path
import subprocess

from sqlalchemy.exc import IntegrityError
from flask import Blueprint, abort, current_app, jsonify, request

from backend.extensions import db
from backend.models import Build, DeploymentEvent, PlatformDeployment, Project
from worker.executor import WorkerExecutionError, create_executor_for_deployment


projects_bp = Blueprint("projects", __name__, url_prefix="/api/projects")


def get_project_or_404(project_id):
    project = db.session.get(Project, project_id)
    if project is None:
        abort(404)
    return project


def get_project_deployment_or_404(project_id, deployment_id):
    deployment = PlatformDeployment.query.filter_by(id=deployment_id, project_id=project_id).first()
    if deployment is None:
        abort(404)
    return deployment


def validate_project_payload(payload):
    required_fields = ("name", "repo_url", "branch", "port", "healthcheck_path")
    missing_fields = [field for field in required_fields if payload.get(field) in (None, "")]
    if missing_fields:
        return f"Missing required fields: {', '.join(missing_fields)}"

    trigger = payload.get("trigger", "manual")
    if trigger not in Project.VALID_TRIGGERS:
        return "Invalid trigger. Expected one of: " + ", ".join(Project.VALID_TRIGGERS)

    runtime = payload.get("runtime", "dockerfile")
    if runtime not in Project.VALID_RUNTIMES:
        return "Invalid runtime. Expected one of: " + ", ".join(Project.VALID_RUNTIMES)

    git_auth_type = payload.get("git_auth_type", "none")
    if git_auth_type not in Project.VALID_GIT_AUTH_TYPES:
        return "Invalid git_auth_type. Expected one of: " + ", ".join(Project.VALID_GIT_AUTH_TYPES)

    git_secret_ref = payload.get("git_secret_ref")
    if git_auth_type == "token" and not git_secret_ref:
        return "git_secret_ref is required when git_auth_type is 'token'"
    if git_secret_ref is not None and not isinstance(git_secret_ref, str):
        return "Invalid git_secret_ref. Expected a string"

    port = payload.get("port")
    if not isinstance(port, int) or port < 1 or port > 65535:
        return "Invalid port. Expected an integer between 1 and 65535"

    env_vars = payload.get("env_vars", [])
    if not isinstance(env_vars, list):
        return "Invalid env_vars. Expected a list of environment variable definitions"

    return None


def validate_project_patch_payload(payload, project):
    allowed_fields = {
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
        "migration_command",
        "cpu",
        "memory",
        "trigger",
        "runtime",
    }
    update_data = {key: value for key, value in payload.items() if key in allowed_fields}
    if not update_data:
        return None, "Provide at least one updatable field"

    if "trigger" in update_data and update_data["trigger"] not in Project.VALID_TRIGGERS:
        return None, "Invalid trigger. Expected one of: " + ", ".join(Project.VALID_TRIGGERS)

    if "runtime" in update_data and update_data["runtime"] not in Project.VALID_RUNTIMES:
        return None, "Invalid runtime. Expected one of: " + ", ".join(Project.VALID_RUNTIMES)

    if "git_auth_type" in update_data and update_data["git_auth_type"] not in Project.VALID_GIT_AUTH_TYPES:
        return None, "Invalid git_auth_type. Expected one of: " + ", ".join(Project.VALID_GIT_AUTH_TYPES)

    if "git_secret_ref" in update_data and update_data["git_secret_ref"] is not None:
        if not isinstance(update_data["git_secret_ref"], str):
            return None, "Invalid git_secret_ref. Expected a string"

    effective_git_auth_type = update_data.get("git_auth_type", project.git_auth_type)
    effective_git_secret_ref = update_data.get("git_secret_ref", project.git_secret_ref)
    if effective_git_auth_type == "token" and not effective_git_secret_ref:
        return None, "git_secret_ref is required when git_auth_type is 'token'"

    if "port" in update_data:
        port = update_data["port"]
        if not isinstance(port, int) or port < 1 or port > 65535:
            return None, "Invalid port. Expected an integer between 1 and 65535"

    if "env_vars" in update_data and not isinstance(update_data["env_vars"], list):
        return None, "Invalid env_vars. Expected a list of environment variable definitions"

    return update_data, None


def validate_deployment_request_payload(payload):
    if not payload.get("commit_sha"):
        return "Missing required field: commit_sha"

    status = payload.get("status", "pending")
    if status not in PlatformDeployment.VALID_STATUSES:
        return "Invalid deployment status. Expected one of: " + ", ".join(PlatformDeployment.VALID_STATUSES)

    build_status = payload.get("build_status", "pending")
    if build_status not in Build.VALID_STATUSES:
        return "Invalid build status. Expected one of: " + ", ".join(Build.VALID_STATUSES)

    return None


def validate_project_deploy_payload(payload):
    branch = payload.get("branch")
    if branch is not None and not isinstance(branch, str):
        return "Invalid branch. Expected a string"
    if isinstance(branch, str) and not branch.strip():
        return "Invalid branch. Expected a non-empty string"

    test_command = payload.get("test_command")
    if test_command is not None and not isinstance(test_command, str):
        return "Invalid test_command. Expected a string"

    return None


def validate_deployment_patch_payload(payload, deployment):
    allowed_fields = {"status", "service_url", "build_status", "message"}
    update_data = {key: value for key, value in payload.items() if key in allowed_fields}
    if not update_data:
        return None, "Provide at least one updatable field: status, service_url, build_status"

    next_status = update_data.get("status")
    if next_status:
        if next_status not in PlatformDeployment.VALID_STATUSES:
            return None, "Invalid deployment status. Expected one of: " + ", ".join(PlatformDeployment.VALID_STATUSES)
        if not deployment.can_transition_to(next_status):
            return None, (
                f"Invalid deployment transition from {deployment.status} to {next_status}. "
                f"Allowed transitions: {', '.join(deployment.allowed_transitions) or 'none'}"
            )

    build_status = update_data.get("build_status")
    if build_status and build_status not in Build.VALID_STATUSES:
        return None, "Invalid build status. Expected one of: " + ", ".join(Build.VALID_STATUSES)

    return update_data, None


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


def resolve_project_commit_sha(project, branch):
    repo_path = Path(project.repo_url)
    if repo_path.exists():
        command = ["git", "-C", str(repo_path), "rev-parse", branch]
    else:
        command = ["git", "ls-remote", project.repo_url, f"refs/heads/{branch}"]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
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
    for event in deployment.events:
        if event.event_type != "deployment.created":
            continue
        metadata = event.metadata_json or {}
        branch = metadata.get("branch")
        if branch:
            return branch
    return deployment.project.branch


def create_user_facing_deployment(project, *, branch, test_command, message_prefix):
    commit_sha = resolve_project_commit_sha(project, branch)
    build, deployment = create_build_and_deployment_records(
        project,
        commit_sha=commit_sha,
        test_command=test_command,
        deployment_message=f"{message_prefix} for branch '{branch}' at commit '{commit_sha[:12]}'",
        deployment_metadata={"branch": branch, "commit_sha": commit_sha},
    )
    db.session.commit()
    return build, deployment, commit_sha


def get_latest_project_deployment_record(project_id):
    return (
        PlatformDeployment.query.filter_by(project_id=project_id)
        .order_by(PlatformDeployment.created_at.desc(), PlatformDeployment.id.desc())
        .first()
    )


def get_runtime_log_path(deployment):
    for event in sorted(deployment.events, key=lambda item: item.created_at, reverse=True):
        metadata = event.metadata_json or {}
        runtime_log_path = metadata.get("runtime_log_path")
        if runtime_log_path:
            return Path(runtime_log_path)
    return None


def get_build_log_path(deployment):
    log_path = deployment.build.log_path
    if not log_path:
        return None
    return Path(log_path)


def recent_deployment_events(deployment, *, limit=5):
    events = sorted(deployment.events, key=lambda item: item.created_at, reverse=True)
    return events[:limit]


def latest_meaningful_event(deployment):
    ignored_prefixes = ("claim_", "reconcile.")
    for event in sorted(deployment.events, key=lambda item: item.created_at, reverse=True):
        if event.event_type.startswith(ignored_prefixes):
            continue
        return event
    return None


def build_log_available(deployment):
    log_path = get_build_log_path(deployment)
    return bool(log_path and is_safe_log_path(log_path) and log_path.is_file())


def runtime_log_available(deployment):
    log_path = get_runtime_log_path(deployment)
    return bool(log_path and is_safe_log_path(log_path) and log_path.is_file())


def latest_push_event(deployment):
    for event in sorted(deployment.events, key=lambda item: item.created_at, reverse=True):
        if event.event_type in {"image.push_succeeded", "image.push.failed"}:
            return event
    return None


def serialize_deployment_summary(deployment):
    build = deployment.build
    branch = get_deployment_branch(deployment)
    meaningful_event = latest_meaningful_event(deployment)
    push_event = latest_push_event(deployment)
    current_step = meaningful_event.step if meaningful_event and meaningful_event.step else deployment.status
    last_meaningful_event = None
    if meaningful_event is not None:
        last_meaningful_event = {
            "event_type": meaningful_event.event_type,
            "step": meaningful_event.step,
            "status": meaningful_event.status,
            "message": meaningful_event.message,
            "created_at": meaningful_event.created_at.isoformat(),
        }

    push_metadata = push_event.metadata_json if push_event else {}

    return {
        "deployment_id": deployment.id,
        "deployment_status": deployment.status,
        "build_id": build.id,
        "build_status": build.status,
        "current_step": current_step,
        "last_meaningful_event": last_meaningful_event,
        "last_error": deployment.last_error or build.last_error,
        "branch": branch,
        "commit_sha": build.commit_sha,
        "image_tag": build.image_tag,
        "image_ref": build.image_ref,
        "registry_push_status": build.registry_push_status,
        "push_log_available": bool(push_metadata.get("push_log_available")),
        "last_push_error_summary": push_metadata.get("error_message") or push_metadata.get("push_summary"),
        "service_url": deployment.service_url,
        "deploy_target": deployment.deploy_target,
        "build_log_available": build_log_available(deployment),
        "runtime_log_available": runtime_log_available(deployment),
        "started_at": deployment.started_at.isoformat() if deployment.started_at else None,
        "finished_at": deployment.finished_at.isoformat() if deployment.finished_at else None,
        "created_at": deployment.created_at.isoformat() if deployment.created_at else None,
        "updated_at": deployment.updated_at.isoformat() if deployment.updated_at else None,
        "events": [event.to_dict() for event in reversed(recent_deployment_events(deployment))],
    }


def is_safe_log_path(path):
    allowed_roots = [
        Path(current).resolve()
        for current in {
            current_app.config.get("CONTROL_PLANE_WORKSPACE_ROOT", "/tmp/paas-workspaces"),
            current_app.instance_path,
        }
    ]
    resolved = path.resolve()
    return any(root == resolved or root in resolved.parents for root in allowed_roots)


def read_log_tail(path, *, tail_lines):
    lines = deque(maxlen=tail_lines)
    total_lines = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            total_lines += 1
            lines.append(line.rstrip("\n"))
    return {
        "content": "\n".join(lines),
        "line_count": total_lines,
        "truncated": total_lines > tail_lines,
    }


def stop_deployment_runtime(deployment, *, message=None):
    executor = create_executor_for_deployment(deployment)
    create_deployment_event(
        deployment.id,
        "deployment.stop_started",
        "stopped",
        message or "Stopping deployment runtime",
        step="deploy.stop",
    )
    stop_result = executor.stop(deployment)
    deployment.status = "stopped"
    deployment.service_url = None
    deployment.finished_at = now_utc()
    if stop_result.deploy_target:
        deployment.deploy_target = stop_result.deploy_target
    if stop_result.container_name:
        deployment.container_name = stop_result.container_name
    if stop_result.container_id:
        deployment.container_id = stop_result.container_id
    if stop_result.host_port is not None:
        deployment.host_port = stop_result.host_port
    if stop_result.healthcheck_url:
        deployment.healthcheck_url = stop_result.healthcheck_url
    create_deployment_event(
        deployment.id,
        "deployment.stopped",
        "stopped",
        stop_result.message,
        step="deploy.stop",
        metadata_json=stop_result.metadata
        | {
            "log_path": stop_result.log_path,
            "summary": stop_result.message,
            "container_name": deployment.container_name,
            "container_id": deployment.container_id,
        },
    )


@projects_bp.get("")
def list_projects():
    projects = Project.query.order_by(Project.created_at.desc()).all()
    return jsonify([project.to_dict() for project in projects])


@projects_bp.post("")
def create_project():
    payload = request.get_json(silent=True) or {}
    validation_error = validate_project_payload(payload)
    if validation_error:
        return jsonify({"error": validation_error}), 400

    project = Project(
        name=payload["name"],
        repo_url=payload["repo_url"],
        branch=payload["branch"],
        git_auth_type=payload.get("git_auth_type", "none"),
        git_secret_ref=payload.get("git_secret_ref"),
        dockerfile_path=payload.get("dockerfile_path", "Dockerfile"),
        build_context=payload.get("build_context", "."),
        port=payload["port"],
        healthcheck_path=payload["healthcheck_path"],
        env_vars=payload.get("env_vars", []),
        migration_command=payload.get("migration_command"),
        cpu=payload.get("cpu"),
        memory=payload.get("memory"),
        trigger=payload.get("trigger", "manual"),
        runtime=payload.get("runtime", "dockerfile"),
    )
    try:
        db.session.add(project)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Project name must be unique"}), 409

    return jsonify(project.to_dict()), 201


@projects_bp.get("/<int:project_id>")
def get_project(project_id):
    return jsonify(get_project_or_404(project_id).to_dict())


@projects_bp.patch("/<int:project_id>")
def update_project(project_id):
    project = get_project_or_404(project_id)
    payload = request.get_json(silent=True) or {}
    update_data, validation_error = validate_project_patch_payload(payload, project)
    if validation_error:
        return jsonify({"error": validation_error}), 400

    for key, value in update_data.items():
        setattr(project, key, value)

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Project name must be unique"}), 409

    return jsonify(project.to_dict())


@projects_bp.delete("/<int:project_id>")
def delete_project(project_id):
    project = get_project_or_404(project_id)
    db.session.delete(project)
    db.session.commit()
    return jsonify({"message": "Project deleted"}), 200


@projects_bp.get("/<int:project_id>/builds")
def list_project_builds(project_id):
    project = get_project_or_404(project_id)
    builds = Build.query.filter_by(project_id=project.id).order_by(Build.created_at.desc()).all()
    return jsonify([build.to_dict() for build in builds])


@projects_bp.get("/<int:project_id>/deployments")
def list_project_deployments(project_id):
    project = get_project_or_404(project_id)
    deployments = PlatformDeployment.query.filter_by(project_id=project.id).order_by(
        PlatformDeployment.created_at.desc()
    ).all()
    return jsonify(
        [
            {
                **deployment.to_dict(),
                "build": deployment.build.to_dict(),
            }
            for deployment in deployments
        ]
    )


@projects_bp.get("/<int:project_id>/deployments/latest")
def get_latest_project_deployment(project_id):
    project = get_project_or_404(project_id)
    deployment = get_latest_project_deployment_record(project.id)
    if deployment is None:
        return jsonify({"error": "Project has no deployments"}), 404

    build = deployment.build
    return jsonify(
        {
            "deployment_id": deployment.id,
            "status": deployment.status,
            "build_id": build.id,
            "build_status": build.status,
            "branch": get_deployment_branch(deployment),
            "commit_sha": build.commit_sha,
            "image_tag": build.image_tag,
            "image_ref": build.image_ref,
            "service_url": deployment.service_url,
            "created_at": deployment.created_at.isoformat() if deployment.created_at else None,
            "updated_at": deployment.updated_at.isoformat() if deployment.updated_at else None,
        }
    )


@projects_bp.post("/<int:project_id>/deployments")
def create_project_deployment(project_id):
    project = get_project_or_404(project_id)
    payload = request.get_json(silent=True) or {}
    validation_error = validate_deployment_request_payload(payload)
    if validation_error:
        return jsonify({"error": validation_error}), 400

    registry = payload.get("registry")
    image_name = payload.get("image_name")
    image_tag = payload.get("image_tag")
    image_ref = payload.get("image_ref")
    if not image_ref and registry and image_name and image_tag:
        image_ref = f"{registry.rstrip('/')}/{image_name}:{image_tag}"

    build, deployment = create_build_and_deployment_records(
        project,
        commit_sha=payload["commit_sha"],
        registry=registry,
        image_name=image_name,
        image_tag=image_tag,
        image_ref=image_ref,
        build_status=payload.get("build_status", "pending"),
        test_command=payload.get("test_command"),
        environment=payload.get("environment", "production"),
        deployment_status=payload.get("status", "pending"),
        service_url=payload.get("service_url"),
        deployment_message=payload.get("message", "Deployment record created"),
        deployment_metadata={"branch": project.branch, "commit_sha": payload["commit_sha"]},
    )

    if build.status != "pending":
        create_deployment_event(
            deployment.id,
            "build.status_reported",
            build.status,
            f"Build recorded in status '{build.status}'",
            step="build",
        )

    db.session.commit()

    return (
        jsonify(
            {
                **deployment.to_dict(),
                "build": build.to_dict(),
                "events": [event.to_dict() for event in deployment.events],
            }
        ),
        201,
    )


@projects_bp.post("/<int:project_id>/deploy")
def deploy_project(project_id):
    project = get_project_or_404(project_id)
    payload = request.get_json(silent=True) or {}
    validation_error = validate_project_deploy_payload(payload)
    if validation_error:
        return jsonify({"error": validation_error}), 400

    branch = (payload.get("branch") or project.branch).strip()
    test_command = payload.get("test_command")

    try:
        build, deployment, commit_sha = create_user_facing_deployment(
            project,
            branch=branch,
            test_command=test_command,
            message_prefix="Deployment requested",
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409

    return (
        jsonify(
            {
                "deployment_id": deployment.id,
                "project_id": project.id,
                "status": deployment.status,
                "branch": branch,
                "commit_sha": build.commit_sha,
                "image_tag": build.image_tag,
                "image_ref": build.image_ref,
            }
        ),
        201,
    )


@projects_bp.post("/<int:project_id>/deployments/<int:deployment_id>/retry")
def retry_project_deployment(project_id, deployment_id):
    project = get_project_or_404(project_id)
    original = get_project_deployment_or_404(project_id, deployment_id)
    branch = get_deployment_branch(original)
    test_command = original.build.test_command

    try:
        build, deployment, commit_sha = create_user_facing_deployment(
            project,
            branch=branch,
            test_command=test_command,
            message_prefix="Deployment retry requested",
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409

    return (
        jsonify(
            {
                "deployment_id": deployment.id,
                "retried_from_deployment_id": original.id,
                "project_id": project.id,
                "status": deployment.status,
                "branch": branch,
                "commit_sha": build.commit_sha,
                "image_tag": build.image_tag,
                "image_ref": build.image_ref,
            }
        ),
        201,
    )


@projects_bp.post("/<int:project_id>/redeploy")
def redeploy_project(project_id):
    project = get_project_or_404(project_id)
    original = get_latest_project_deployment_record(project.id)
    if original is None:
        return jsonify({"error": "Project has no deployments"}), 404

    branch = get_deployment_branch(original)
    test_command = original.build.test_command

    try:
        build, deployment, _commit_sha = create_user_facing_deployment(
            project,
            branch=branch,
            test_command=test_command,
            message_prefix="Project redeploy requested",
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409

    return (
        jsonify(
            {
                "deployment_id": deployment.id,
                "redeployed_from_deployment_id": original.id,
                "project_id": project.id,
                "status": deployment.status,
                "branch": branch,
                "commit_sha": build.commit_sha,
                "image_tag": build.image_tag,
                "image_ref": build.image_ref,
            }
        ),
        201,
    )


@projects_bp.get("/<int:project_id>/deployments/<int:deployment_id>")
def get_project_deployment(project_id, deployment_id):
    deployment = get_project_deployment_or_404(project_id, deployment_id)
    return jsonify(
        {
            **deployment.to_dict(),
            "build": deployment.build.to_dict(),
            "events": [event.to_dict() for event in deployment.events],
        }
    )


@projects_bp.get("/<int:project_id>/deployments/<int:deployment_id>/summary")
def get_project_deployment_summary(project_id, deployment_id):
    deployment = get_project_deployment_or_404(project_id, deployment_id)
    return jsonify(serialize_deployment_summary(deployment))


@projects_bp.get("/<int:project_id>/deployments/<int:deployment_id>/runtime-log")
def get_project_deployment_runtime_log(project_id, deployment_id):
    deployment = get_project_deployment_or_404(project_id, deployment_id)
    runtime_log_path = get_runtime_log_path(deployment)
    if runtime_log_path is None:
        return jsonify({"error": "No runtime log is available for this deployment"}), 404
    if not is_safe_log_path(runtime_log_path):
        return jsonify({"error": "Runtime log path is outside allowed log roots"}), 409
    if not runtime_log_path.is_file():
        return jsonify({"error": "Runtime log file does not exist", "path": str(runtime_log_path)}), 404

    tail_lines = request.args.get("tail_lines", default=200, type=int)
    if tail_lines is None or tail_lines < 1 or tail_lines > 2000:
        return jsonify({"error": "tail_lines must be an integer between 1 and 2000"}), 400

    payload = read_log_tail(runtime_log_path, tail_lines=tail_lines)
    return jsonify(
        {
            "deployment_id": deployment.id,
            "path": str(runtime_log_path),
            "tail_lines": tail_lines,
            **payload,
        }
    )


@projects_bp.get("/<int:project_id>/deployments/<int:deployment_id>/build-log")
def get_project_deployment_build_log(project_id, deployment_id):
    deployment = get_project_deployment_or_404(project_id, deployment_id)
    build_log_path = get_build_log_path(deployment)
    if build_log_path is None:
        return jsonify({"error": "No build log is available for this deployment"}), 404
    if not is_safe_log_path(build_log_path):
        return jsonify({"error": "Build log path is outside allowed log roots"}), 409
    if not build_log_path.is_file():
        return jsonify({"error": "Build log file does not exist", "path": str(build_log_path)}), 404

    tail_lines = request.args.get("tail_lines", default=200, type=int)
    if tail_lines is None or tail_lines < 1 or tail_lines > 2000:
        return jsonify({"error": "tail_lines must be an integer between 1 and 2000"}), 400

    payload = read_log_tail(build_log_path, tail_lines=tail_lines)
    return jsonify(
        {
            "deployment_id": deployment.id,
            "build_id": deployment.build.id,
            "path": str(build_log_path),
            "tail_lines": tail_lines,
            **payload,
        }
    )


@projects_bp.patch("/<int:project_id>/deployments/<int:deployment_id>")
def update_project_deployment(project_id, deployment_id):
    deployment = get_project_deployment_or_404(project_id, deployment_id)
    payload = request.get_json(silent=True) or {}
    update_data, validation_error = validate_deployment_patch_payload(payload, deployment)
    if validation_error:
        return jsonify({"error": validation_error}), 400

    message = update_data.get("message")

    if update_data.get("status") == "stopped":
        try:
            stop_deployment_runtime(deployment, message=message)
        except WorkerExecutionError as exc:
            db.session.rollback()
            return (
                jsonify(
                    {
                        "error": exc.message,
                        "step": exc.step,
                        "metadata": exc.metadata,
                    }
                ),
                409,
            )
    elif "status" in update_data:
        deployment.status = update_data["status"]
        create_deployment_event(
            deployment.id,
            "deployment.status_updated",
            deployment.status,
            message or f"Deployment status updated to '{deployment.status}'",
            step="deployment",
        )

    if "service_url" in update_data:
        deployment.service_url = update_data["service_url"]
        create_deployment_event(
            deployment.id,
            "deployment.service_url_updated",
            deployment.status,
            message or f"Service URL updated to '{deployment.service_url}'",
            step="deploy",
        )

    if "build_status" in update_data:
        deployment.build.status = update_data["build_status"]
        create_deployment_event(
            deployment.id,
            "build.status_updated",
            deployment.build.status,
            message or f"Build status updated to '{deployment.build.status}'",
            step="build",
        )

    db.session.commit()

    return jsonify(
        {
            **deployment.to_dict(),
            "build": deployment.build.to_dict(),
            "events": [event.to_dict() for event in deployment.events],
        }
    )


@projects_bp.get("/<int:project_id>/deployments/<int:deployment_id>/events")
def list_project_deployment_events(project_id, deployment_id):
    deployment = get_project_deployment_or_404(project_id, deployment_id)
    events = DeploymentEvent.query.filter_by(deployment_id=deployment.id).order_by(DeploymentEvent.created_at.asc()).all()
    return jsonify([event.to_dict() for event in events])
