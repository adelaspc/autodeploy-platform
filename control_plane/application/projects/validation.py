from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlparse

from flask import current_app

from control_plane.command_validation import validate_optional_command
from control_plane.models import Build, PlatformDeployment, Project
from control_plane.security import normalize_project_env_vars
from worker.execution.factory import executor_contract_for_name


VALID_ENV_VALUE_SOURCES = {"literal", "configmap_key_ref", "secret_key_ref"}
KUBERNETES_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
KUBERNETES_RESOURCE_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")


def validate_non_empty_string(value, field_name):
    if not isinstance(value, str):
        return f"Invalid {field_name}. Expected a string"
    if not value.strip():
        return f"Invalid {field_name}. Expected a non-empty string"
    return None


def validate_healthcheck_path(value):
    string_error = validate_non_empty_string(value, "healthcheck_path")
    if string_error:
        return string_error
    if not value.startswith("/"):
        return "Invalid healthcheck_path. Expected an absolute path starting with '/'"
    return None


def normalize_github_repo_url(value):
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        return None
    if parsed.params or parsed.query or parsed.fragment:
        return None

    path = parsed.path.rstrip("/")
    parts = [part for part in path.split("/") if part]
    if len(parts) != 2:
        return None

    owner, repo = parts
    if not owner or not repo:
        return None

    if repo.endswith(".git"):
        repo = repo[:-4]
    if not repo:
        return None

    if "/" in owner or "/" in repo or repo.endswith(".git"):
        return None

    return f"https://github.com/{owner.lower()}/{repo.lower()}.git"


def validate_repo_url(value):
    string_error = validate_non_empty_string(value, "repo_url")
    if string_error:
        return string_error

    normalized_remote = normalize_github_repo_url(value)
    if normalized_remote is not None:
        return None

    app_env = (current_app.config.get("CONTROL_PLANE_ENV") or "").strip().lower()
    repo_path = Path(value)
    if app_env == "development" and repo_path.exists():
        return None

    if repo_path.exists():
        return "Invalid repo_url. Local repository paths are allowed only when CONTROL_PLANE_ENV=development"

    return (
        "Invalid repo_url. Expected a canonical GitHub HTTPS repository URL like "
        "https://github.com/<owner>/<repo> or https://github.com/<owner>/<repo>.git"
    )


def validate_repo_relative_path(value, field_name, *, allow_dot=False):
    string_error = validate_non_empty_string(value, field_name)
    if string_error:
        return string_error

    repo_path = PurePosixPath(value)
    if repo_path.is_absolute():
        return f"Invalid {field_name}. Expected a repository-relative path"
    if any(part == ".." for part in repo_path.parts):
        return f"Invalid {field_name}. Parent directory traversal is not allowed"
    if not allow_dot and value in {".", "./"}:
        return f"Invalid {field_name}. Expected a file path within the repository"

    return None


def validate_project_spec_fields(payload):
    for field_name in ("name", "branch"):
        if field_name in payload:
            string_error = validate_non_empty_string(payload[field_name], field_name)
            if string_error:
                return string_error

    if "repo_url" in payload:
        repo_url_error = validate_repo_url(payload["repo_url"])
        if repo_url_error:
            return repo_url_error

    if "dockerfile_path" in payload:
        dockerfile_error = validate_repo_relative_path(payload["dockerfile_path"], "dockerfile_path")
        if dockerfile_error:
            return dockerfile_error

    if "build_context" in payload:
        build_context_error = validate_repo_relative_path(
            payload["build_context"],
            "build_context",
            allow_dot=True,
        )
        if build_context_error:
            return build_context_error

    if "healthcheck_path" in payload:
        healthcheck_error = validate_healthcheck_path(payload["healthcheck_path"])
        if healthcheck_error:
            return healthcheck_error

    for command_field in ("default_test_command", "migration_command"):
        if command_field in payload:
            command_error = validate_optional_command(payload[command_field], command_field)
            if command_error:
                return command_error

    return None


def normalize_project_spec_fields(payload):
    normalized = dict(payload)
    repo_url = normalized.get("repo_url")
    if isinstance(repo_url, str):
        normalized_remote = normalize_github_repo_url(repo_url)
        if normalized_remote is not None:
            normalized["repo_url"] = normalized_remote
    if "env_vars" in normalized:
        normalized["env_vars"] = normalize_project_env_vars(normalized.get("env_vars"))
    return normalized


def validate_project_payload(payload):
    required_fields = ("name", "repo_url", "branch", "port", "healthcheck_path")
    missing_fields = [field for field in required_fields if payload.get(field) in (None, "")]
    if missing_fields:
        return f"Missing required fields: {', '.join(missing_fields)}"

    spec_payload = {
        "name": payload.get("name"),
        "repo_url": payload.get("repo_url"),
        "branch": payload.get("branch"),
        "dockerfile_path": payload.get("dockerfile_path", "Dockerfile"),
        "build_context": payload.get("build_context", "."),
        "healthcheck_path": payload.get("healthcheck_path"),
    }
    for command_field in ("default_test_command", "migration_command"):
        if command_field in payload:
            spec_payload[command_field] = payload.get(command_field)

    field_error = validate_project_spec_fields(spec_payload)
    if field_error:
        return field_error

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
    env_vars_error = validate_project_env_vars(env_vars)
    if env_vars_error:
        return env_vars_error

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
        "default_test_command",
        "migration_command",
        "cpu",
        "memory",
        "trigger",
        "runtime",
    }
    update_data = {key: value for key, value in payload.items() if key in allowed_fields}
    if not update_data:
        return None, "Provide at least one updatable field"

    field_error = validate_project_spec_fields(update_data)
    if field_error:
        return None, field_error

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

    if "env_vars" in update_data:
        env_vars_error = validate_project_env_vars(update_data["env_vars"])
        if env_vars_error:
            return None, env_vars_error

    return update_data, None


def validate_project_env_vars(env_vars):
    if not isinstance(env_vars, list):
        return "Invalid env_vars. Expected a list of environment variable definitions"

    kubernetes_mode = kubernetes_project_validation_enabled()
    seen_names = set()

    for index, item in enumerate(env_vars):
        if not isinstance(item, dict):
            return f"Invalid env_vars[{index}]. Expected an object"

        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            return f"Invalid env_vars[{index}]. 'name' is required"
        if kubernetes_mode and not KUBERNETES_ENV_VAR_NAME_RE.fullmatch(name):
            return f"Invalid env_vars[{index}]. 'name' must be a valid Kubernetes environment variable name"
        if kubernetes_mode and name in seen_names:
            return f"Invalid env_vars[{index}]. Duplicate env var name '{name}' is not allowed for Kubernetes deployments"
        seen_names.add(name)

        value_source = item.get("value_source")
        if value_source is None:
            value_source = "literal" if item.get("value") is not None else None

        source_name = item.get("source_name")
        source_key = item.get("source_key")
        value = item.get("value")
        is_secret = item.get("is_secret")
        configmap_ref = item.get("configmap_ref")
        secret_ref = item.get("secret_ref")

        if configmap_ref is not None or secret_ref is not None:
            return (
                f"Invalid env_vars[{index}]. Use 'source_name' and 'source_key' for Kubernetes references; "
                "'configmap_ref' and 'secret_ref' are not supported field names"
            )

        if value_source is None and value is None and source_name is None and source_key is None:
            continue

        if value_source is None:
            return (
                f"Invalid env_vars[{index}]. Provide either a literal 'value', a supported "
                "'value_source', or a metadata-only env var definition"
            )
        if value_source not in VALID_ENV_VALUE_SOURCES:
            return (
                f"Invalid env_vars[{index}]. 'value_source' must be one of: "
                + ", ".join(sorted(VALID_ENV_VALUE_SOURCES))
            )

        if value_source == "literal":
            if value is None:
                return f"Invalid env_vars[{index}]. 'value' is required when value_source is 'literal'"
            if source_name is not None or source_key is not None:
                return f"Invalid env_vars[{index}]. Literal env vars cannot include 'source_name' or 'source_key'"
            if is_secret is not None and not isinstance(is_secret, bool):
                return f"Invalid env_vars[{index}]. 'is_secret' must be a boolean when provided"
            if kubernetes_mode and is_secret is True:
                return (
                    f"Invalid env_vars[{index}]. Kubernetes secret env vars must use "
                    "'value_source=secret_key_ref' instead of a literal value"
                )
        else:
            if value is not None:
                return f"Invalid env_vars[{index}]. Referenced env vars cannot include a literal 'value'"
            if is_secret is not None and not isinstance(is_secret, bool):
                return f"Invalid env_vars[{index}]. 'is_secret' must be a boolean when provided"
            if not isinstance(source_name, str) or not source_name.strip():
                return f"Invalid env_vars[{index}]. 'source_name' is required for referenced env vars"
            if kubernetes_mode and not KUBERNETES_RESOURCE_NAME_RE.fullmatch(source_name):
                return f"Invalid env_vars[{index}]. 'source_name' must be a valid Kubernetes resource name"
            if not isinstance(source_key, str) or not source_key.strip():
                return f"Invalid env_vars[{index}]. 'source_key' is required for referenced env vars"

    return None


def validate_deployment_request_payload(payload):
    if not payload.get("commit_sha"):
        return "Missing required field: commit_sha"

    status = payload.get("status", "pending")
    if status not in PlatformDeployment.VALID_STATUSES:
        return "Invalid deployment status. Expected one of: " + ", ".join(PlatformDeployment.VALID_STATUSES)

    build_status = payload.get("build_status", "pending")
    if build_status not in Build.VALID_STATUSES:
        return "Invalid build status. Expected one of: " + ", ".join(Build.VALID_STATUSES)

    if "test_command" in payload:
        test_command_error = validate_optional_command(payload.get("test_command"), "test_command")
        if test_command_error:
            return test_command_error

    return None


def validate_project_deploy_payload(payload):
    branch = payload.get("branch")
    if branch is not None and not isinstance(branch, str):
        return "Invalid branch. Expected a string"
    if isinstance(branch, str) and not branch.strip():
        return "Invalid branch. Expected a non-empty string"

    test_command = payload.get("test_command")
    test_command_error = validate_optional_command(test_command, "test_command")
    if test_command_error:
        return test_command_error

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


def resolve_effective_test_command(project, *, requested_test_command, payload_includes_test_command):
    if payload_includes_test_command:
        return requested_test_command
    return project.default_test_command


def kubernetes_project_validation_enabled():
    executor_name = (current_app.config.get("CONTROL_PLANE_EXECUTOR", "fake") or "").strip().lower()
    executor_contract = executor_contract_for_name(executor_name)
    return executor_contract.deploy_target == "kubernetes"


def executor_deployment_prereq_missing_settings(executor_name=None):
    resolved_executor_name = executor_name or current_app.config.get("CONTROL_PLANE_EXECUTOR", "fake")
    executor_contract = executor_contract_for_name(resolved_executor_name)
    missing = []

    for required_setting in executor_contract.required_config:
        if required_setting.endswith("=true"):
            config_name = required_setting[:-5]
            if not current_app.config.get(config_name, False):
                missing.append(required_setting)
            continue

        config_value = current_app.config.get(required_setting)
        if isinstance(config_value, str):
            if not config_value.strip():
                missing.append(required_setting)
            continue

        if config_value in (None, False):
            missing.append(required_setting)

    return missing


def executor_deployment_prereq_error(executor_name=None):
    resolved_executor_name = (executor_name or current_app.config.get("CONTROL_PLANE_EXECUTOR", "fake") or "").strip().lower()
    missing = executor_deployment_prereq_missing_settings(resolved_executor_name)
    if not missing:
        return None

    executor_contract = executor_contract_for_name(resolved_executor_name)
    return (
        f"{executor_contract.name.capitalize()} executor is not ready for deployments. Missing required settings: "
        + ", ".join(missing)
    )


def kubernetes_deployment_prereq_missing_settings():
    if not kubernetes_project_validation_enabled():
        return []
    return executor_deployment_prereq_missing_settings("kubernetes")


def kubernetes_deployment_prereq_error():
    if not kubernetes_project_validation_enabled():
        return None
    return executor_deployment_prereq_error("kubernetes")
