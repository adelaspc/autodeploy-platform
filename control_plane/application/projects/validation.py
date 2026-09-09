"""Validate and normalize project specifications at the API boundary."""

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
KUBERNETES_RESOURCE_NAME_RE = re.compile(
    r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*$"
)
KUBERNETES_CONFIG_KEY_RE = re.compile(r"^[-._A-Za-z0-9]+$")
GIT_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,119}$")
GIT_COMMIT_RE = re.compile(r"^(?:[0-9a-fA-F]{7,40}|[0-9a-fA-F]{64})$")
FULL_GIT_COMMIT_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
CPU_QUANTITY_RE = re.compile(r"^(?:[1-9][0-9]*m|(?:0\.[0-9]{1,3}|[1-9][0-9]*(?:\.[0-9]{1,3})?))$")
MEMORY_QUANTITY_RE = re.compile(r"^[1-9][0-9]*(?:[KMGTPE]i|[kMGTPE])?$")

PROJECT_ALLOWED_FIELDS = {
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
MANUAL_DEPLOYMENT_ALLOWED_FIELDS = {
    "commit_sha",
    "registry",
    "image_name",
    "image_tag",
    "image_ref",
    "build_status",
    "test_command",
    "environment",
    "status",
    "service_url",
    "message",
}
DEPLOY_ALLOWED_FIELDS = {"branch", "test_command"}
ENV_VAR_ALLOWED_FIELDS = {"name", "value", "value_source", "source_name", "source_key", "is_secret"}

PROJECT_STRING_LIMITS = {
    "name": 120,
    "repo_url": 255,
    "branch": 120,
    "git_secret_ref": 120,
    "dockerfile_path": 255,
    "build_context": 255,
    "healthcheck_path": 255,
}
MANUAL_DEPLOYMENT_STRING_LIMITS = {
    "registry": 255,
    "image_name": 255,
    "image_tag": 255,
    "image_ref": 512,
    "environment": 64,
    "service_url": 255,
    "message": 2000,
}
MAX_ENV_VARS = 100
MAX_ENV_VAR_NAME_LENGTH = 253
MAX_ENV_VALUE_LENGTH = 65535
KUBERNETES_NAME_MAX_LENGTH = 253
KUBERNETES_CONFIG_KEY_MAX_LENGTH = 253


def validate_bounded_string(value, field_name, max_length, *, allow_none=False, allow_empty=False):
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        return f"Invalid {field_name}. Expected a string"
    if not allow_empty and not value.strip():
        return f"Invalid {field_name}. Expected a non-empty string"
    if len(value) > max_length:
        return f"Invalid {field_name}. Expected at most {max_length} characters"
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return f"Invalid {field_name}. Control characters are not allowed"
    return None


def unsupported_fields_error(payload, allowed_fields, payload_name):
    unsupported = sorted(set(payload) - allowed_fields)
    if unsupported:
        return f"Unsupported {payload_name} fields: {', '.join(unsupported)}"
    return None


def validate_git_commit_sha(value, *, allow_abbreviated=True):
    if not isinstance(value, str):
        return "Invalid commit_sha. Expected a string"
    commit_re = GIT_COMMIT_RE if allow_abbreviated else FULL_GIT_COMMIT_RE
    if commit_re.fullmatch(value) is None:
        expected = "7 to 40, or exactly 64" if allow_abbreviated else "exactly 40 or 64"
        return f"Invalid commit_sha. Expected a hexadecimal Git object ID with {expected} characters"
    return None


def validate_resource_quantity(value, field_name):
    if value is None:
        return None
    if not isinstance(value, str):
        return f"Invalid {field_name}. Expected a string"
    if len(value) > 32:
        return f"Invalid {field_name}. Expected at most 32 characters"
    if field_name == "cpu" and CPU_QUANTITY_RE.fullmatch(value) is None:
        return "Invalid cpu. Expected a positive CPU quantity such as '250m', '0.5', or '1'"
    if field_name == "memory" and MEMORY_QUANTITY_RE.fullmatch(value) is None:
        return "Invalid memory. Expected a positive byte quantity such as '512Mi', '1Gi', or '100M'"
    return None


def validate_kubernetes_resource_name(value, field_name):
    string_error = validate_bounded_string(value, field_name, KUBERNETES_NAME_MAX_LENGTH)
    if string_error:
        return string_error
    if KUBERNETES_RESOURCE_NAME_RE.fullmatch(value) is None:
        return f"Invalid {field_name}. Expected a valid Kubernetes resource name using DNS subdomain syntax"
    return None


def validate_kubernetes_config_key(value, field_name):
    string_error = validate_bounded_string(value, field_name, KUBERNETES_CONFIG_KEY_MAX_LENGTH)
    if string_error:
        return string_error
    if KUBERNETES_CONFIG_KEY_RE.fullmatch(value) is None or value in {".", ".."} or value.startswith(".."):
        return (
            f"Invalid {field_name}. Expected a valid Kubernetes Secret/ConfigMap key containing only "
            "letters, digits, '-', '_' or '.'"
        )
    return None


def validate_healthcheck_path(value):
    string_error = validate_bounded_string(
        value,
        "healthcheck_path",
        PROJECT_STRING_LIMITS["healthcheck_path"],
    )
    if string_error:
        return string_error
    if not value.startswith("/"):
        return "Invalid healthcheck_path. Expected an absolute path starting with '/'"
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        return "Invalid healthcheck_path. Whitespace and control characters are not allowed"
    return None


def validate_branch_name(value):
    string_error = validate_bounded_string(value, "branch", PROJECT_STRING_LIMITS["branch"])
    if string_error:
        return string_error
    if (
        GIT_BRANCH_NAME_RE.fullmatch(value) is None
        or ".." in value
        or "//" in value
        or "@{" in value
        or value.endswith(("/", "."))
        or value.endswith(".lock")
    ):
        return "Invalid branch. Expected a safe Git branch name"
    return None


def normalize_github_repo_url(value):
    parsed = urlparse(value)
    # Only accept GitHub HTTPS URLs without extra URL components.
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        return None
    if parsed.params or parsed.query or parsed.fragment:
        return None

    # The path must contain exactly an owner and a repository name.
    path_match = re.fullmatch(r"/([^/]+)/([^/]+)/?", parsed.path)
    if path_match is None:
        return None

    owner, repo = path_match.groups()

    if repo.endswith(".git"):
        repo = repo[:-4]
    # Reject empty repository names and repeated .git suffixes.
    if not repo or repo.endswith(".git"):
        return None

    return f"https://github.com/{owner.lower()}/{repo.lower()}.git"


def validate_repo_url(value):
    string_error = validate_bounded_string(value, "repo_url", PROJECT_STRING_LIMITS["repo_url"])
    if string_error:
        return string_error

    normalized_remote = normalize_github_repo_url(value)
    if normalized_remote is not None:
        if len(normalized_remote) > PROJECT_STRING_LIMITS["repo_url"]:
            return f"Invalid repo_url. Expected at most {PROJECT_STRING_LIMITS['repo_url']} characters after normalization"
        return None

    # Local repositories are useful for development but must stay inside the
    # explicitly configured local repository root.
    app_env = (current_app.config.get("CONTROL_PLANE_ENV") or "").strip().lower()
    local_repo_root = Path(
        current_app.config.get("CONTROL_PLANE_LOCAL_REPO_ROOT", "/tmp/paas-local-repos")
    ).resolve()
    repo_path = Path(value).resolve()
    try:
        repo_path.relative_to(local_repo_root)
    except ValueError:
        repo_is_allowed = False
    else:
        repo_is_allowed = repo_path.is_dir()

    if app_env == "development" and repo_is_allowed:
        return None

    return (
        "Invalid repo_url. Local repository paths are allowed only when "
        "CONTROL_PLANE_ENV=development and under CONTROL_PLANE_LOCAL_REPO_ROOT; otherwise "
        "use a canonical GitHub HTTPS repository URL"
    )


def validate_repo_relative_path(value, field_name, *, allow_dot=False):
    string_error = validate_bounded_string(value, field_name, PROJECT_STRING_LIMITS[field_name])
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
    # This validation is shared by create and patch requests.
    for field_name in ("name",):
        if field_name in payload:
            string_error = validate_bounded_string(
                payload[field_name],
                field_name,
                PROJECT_STRING_LIMITS[field_name],
            )
            if string_error:
                return string_error

    if "branch" in payload:
        branch_error = validate_branch_name(payload["branch"])
        if branch_error:
            return branch_error

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

    if "git_secret_ref" in payload:
        git_secret_ref = payload["git_secret_ref"]
        string_error = validate_bounded_string(
            git_secret_ref,
            "git_secret_ref",
            PROJECT_STRING_LIMITS["git_secret_ref"],
            allow_none=True,
        )
        if string_error:
            return string_error
        if git_secret_ref is not None and KUBERNETES_ENV_VAR_NAME_RE.fullmatch(git_secret_ref) is None:
            return "Invalid git_secret_ref. Expected an environment-variable identifier"

    for resource_field in ("cpu", "memory"):
        if resource_field in payload:
            quantity_error = validate_resource_quantity(payload[resource_field], resource_field)
            if quantity_error:
                return quantity_error

    return None


def normalize_project_spec_fields(payload):
    # Work on a copy so the original request payload is not changed.
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
    unsupported_error = unsupported_fields_error(payload, PROJECT_ALLOWED_FIELDS, "project")
    if unsupported_error:
        return unsupported_error

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
        "git_secret_ref": payload.get("git_secret_ref"),
        "cpu": payload.get("cpu"),
        "memory": payload.get("memory"),
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
    port = payload.get("port")
    if type(port) is not int or port < 1 or port > 65535:
        return "Invalid port. Expected an integer between 1 and 65535"

    env_vars = payload.get("env_vars", [])
    env_vars_error = validate_project_env_vars(env_vars)
    if env_vars_error:
        return env_vars_error

    return None


def validate_project_patch_payload(payload, project):
    unsupported_error = unsupported_fields_error(payload, PROJECT_ALLOWED_FIELDS, "project")
    if unsupported_error:
        return None, unsupported_error

    update_data = dict(payload)
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

    # Validate the final auth configuration, including values already stored on the project.
    effective_git_auth_type = update_data.get("git_auth_type", project.git_auth_type)
    effective_git_secret_ref = update_data.get("git_secret_ref", project.git_secret_ref)
    if effective_git_auth_type == "token" and not effective_git_secret_ref:
        return None, "git_secret_ref is required when git_auth_type is 'token'"

    if "port" in update_data:
        port = update_data["port"]
        if type(port) is not int or port < 1 or port > 65535:
            return None, "Invalid port. Expected an integer between 1 and 65535"

    if "env_vars" in update_data:
        env_vars_error = validate_project_env_vars(update_data["env_vars"])
        if env_vars_error:
            return None, env_vars_error

    return update_data, None


def validate_project_env_vars(env_vars):
    if not isinstance(env_vars, list):
        return "Invalid env_vars. Expected a list of environment variable definitions"
    if len(env_vars) > MAX_ENV_VARS:
        return f"Invalid env_vars. Expected at most {MAX_ENV_VARS} entries"

    # Reference names and keys are Kubernetes-specific and are always validated
    # so a later executor switch cannot activate an invalid persisted spec. The
    # fake and local Docker modes intentionally keep broader literal-name rules.
    kubernetes_mode = kubernetes_project_validation_enabled()
    seen_names = set()

    for index, item in enumerate(env_vars):
        if not isinstance(item, dict):
            return f"Invalid env_vars[{index}]. Expected an object"
        unsupported_error = unsupported_fields_error(item, ENV_VAR_ALLOWED_FIELDS, f"env_vars[{index}]")
        if unsupported_error:
            return unsupported_error

        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            return f"Invalid env_vars[{index}]. 'name' is required"
        if len(name) > MAX_ENV_VAR_NAME_LENGTH:
            return f"Invalid env_vars[{index}]. 'name' must be at most {MAX_ENV_VAR_NAME_LENGTH} characters"
        if kubernetes_mode and not KUBERNETES_ENV_VAR_NAME_RE.fullmatch(name):
            return f"Invalid env_vars[{index}]. 'name' must be a valid Kubernetes environment variable name"
        if kubernetes_mode and name in seen_names:
            return f"Invalid env_vars[{index}]. Duplicate env var name '{name}' is not allowed for Kubernetes deployments"
        seen_names.add(name)

        value_source = item.get("value_source")
        # Older clients sent only `value`; treat that shape as a literal.
        if value_source is None:
            value_source = "literal" if item.get("value") is not None else None

        source_name = item.get("source_name")
        source_key = item.get("source_key")
        value = item.get("value")
        is_secret = item.get("is_secret")

        if value_source is None:
            return f"Invalid env_vars[{index}]. Provide a literal 'value' or a supported 'value_source'"
        if not isinstance(value_source, str):
            return f"Invalid env_vars[{index}]. 'value_source' must be a string"
        if value_source not in VALID_ENV_VALUE_SOURCES:
            return (
                f"Invalid env_vars[{index}]. 'value_source' must be one of: "
                + ", ".join(sorted(VALID_ENV_VALUE_SOURCES))
            )

        if value_source == "literal":
            if value is None:
                return f"Invalid env_vars[{index}]. 'value' is required when value_source is 'literal'"
            if not isinstance(value, str):
                return f"Invalid env_vars[{index}]. 'value' must be a string"
            if len(value) > MAX_ENV_VALUE_LENGTH:
                return f"Invalid env_vars[{index}]. 'value' must be at most {MAX_ENV_VALUE_LENGTH} characters"
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
            source_name_error = validate_kubernetes_resource_name(
                source_name,
                f"env_vars[{index}].source_name",
            )
            if source_name_error:
                return source_name_error
            source_key_error = validate_kubernetes_config_key(
                source_key,
                f"env_vars[{index}].source_key",
            )
            if source_key_error:
                return source_key_error

    return None


def validate_deployment_request_payload(payload):
    unsupported_error = unsupported_fields_error(
        payload,
        MANUAL_DEPLOYMENT_ALLOWED_FIELDS,
        "manual deployment",
    )
    if unsupported_error:
        return unsupported_error

    if payload.get("commit_sha") in (None, ""):
        return "Missing required field: commit_sha"
    commit_error = validate_git_commit_sha(payload["commit_sha"])
    if commit_error:
        return commit_error

    status = payload.get("status", "pending")
    if not isinstance(status, str):
        return "Invalid deployment status. Expected a string"
    if status not in PlatformDeployment.VALID_STATUSES:
        return "Invalid deployment status. Expected one of: " + ", ".join(PlatformDeployment.VALID_STATUSES)

    build_status = payload.get("build_status", "pending")
    if not isinstance(build_status, str):
        return "Invalid build status. Expected a string"
    if build_status not in Build.VALID_STATUSES:
        return "Invalid build status. Expected one of: " + ", ".join(Build.VALID_STATUSES)

    if "test_command" in payload:
        test_command_error = validate_optional_command(payload.get("test_command"), "test_command")
        if test_command_error:
            return test_command_error

    for field_name, max_length in MANUAL_DEPLOYMENT_STRING_LIMITS.items():
        if field_name not in payload:
            continue
        allow_none = field_name not in {"environment"}
        string_error = validate_bounded_string(
            payload[field_name],
            field_name,
            max_length,
            allow_none=allow_none,
            allow_empty=field_name == "message",
        )
        if string_error:
            return string_error

    environment = payload.get("environment", "production")
    if re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", environment) is None:
        return "Invalid environment. Expected an alphanumeric name containing only '-', '_' or '.'"

    service_url = payload.get("service_url")
    if service_url is not None:
        parsed_service_url = urlparse(service_url)
        if parsed_service_url.scheme not in {"http", "https"} or not parsed_service_url.netloc:
            return "Invalid service_url. Expected an absolute HTTP or HTTPS URL"

    return None


def normalize_deployment_request_payload(payload):
    normalized = dict(payload)
    commit_sha = normalized.get("commit_sha")
    if isinstance(commit_sha, str):
        normalized["commit_sha"] = commit_sha.lower()
    return normalized


def validate_project_deploy_payload(payload):
    unsupported_error = unsupported_fields_error(payload, DEPLOY_ALLOWED_FIELDS, "deploy")
    if unsupported_error:
        return unsupported_error

    branch = payload.get("branch")
    if branch is not None:
        branch_error = validate_branch_name(branch)
        if branch_error:
            return branch_error

    test_command = payload.get("test_command")
    test_command_error = validate_optional_command(test_command, "test_command")
    if test_command_error:
        return test_command_error

    return None


def validate_deployment_patch_payload(payload, deployment):
    allowed_fields = {"status", "build_status", "message"}
    unsupported_error = unsupported_fields_error(payload, allowed_fields, "deployment patch")
    if unsupported_error:
        return None, unsupported_error
    update_data = dict(payload)
    if not ({"status", "build_status"} & set(update_data)):
        return None, "Provide at least one updatable field: status, build_status"

    # Valid status names can still be invalid at this point in the state machine.
    if "status" in update_data:
        next_status = update_data["status"]
        if not isinstance(next_status, str):
            return None, "Invalid deployment status. Expected a string"
        if next_status not in PlatformDeployment.VALID_STATUSES:
            return None, "Invalid deployment status. Expected one of: " + ", ".join(PlatformDeployment.VALID_STATUSES)
        if not deployment.can_transition_to(next_status):
            return None, (
                f"Invalid deployment transition from {deployment.status} to {next_status}. "
                f"Allowed transitions: {', '.join(deployment.allowed_transitions) or 'none'}"
            )

    if "build_status" in update_data:
        build_status = update_data["build_status"]
        if not isinstance(build_status, str):
            return None, "Invalid build status. Expected a string"
        if build_status not in Build.VALID_STATUSES:
            return None, "Invalid build status. Expected one of: " + ", ".join(Build.VALID_STATUSES)
        if not deployment.build.can_transition_to(build_status):
            return None, f"Invalid build transition from {deployment.build.status} to {build_status}"

    if "message" in update_data:
        message_error = validate_bounded_string(
            update_data["message"],
            "message",
            MANUAL_DEPLOYMENT_STRING_LIMITS["message"],
            allow_none=True,
            allow_empty=True,
        )
        if message_error:
            return None, message_error

    return update_data, None


def resolve_effective_test_command(project, *, requested_test_command, payload_includes_test_command):
    # An explicit null disables the project default for this deployment.
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
        # Settings declared with =true must be explicitly enabled.
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
