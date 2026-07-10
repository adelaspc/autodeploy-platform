import hmac
import json
from dataclasses import dataclass
from functools import wraps

from flask import current_app, jsonify, request

from backend.api.request_context import error_payload


ROLE_LEVELS = {
    "read_only": 1,
    "deployer": 2,
    "admin": 3,
}

CONFIG_KEY_BY_ROLE = {
    "read_only": "CONTROL_PLANE_API_TOKEN_READ_ONLY",
    "deployer": "CONTROL_PLANE_API_TOKEN_DEPLOYER",
    "admin": "CONTROL_PLANE_API_TOKEN_ADMIN",
}

LOCAL_AUTH_DISABLED_ENVS = {"development", "local", "test"}
TOKEN_MISSING = "missing"
TOKEN_MALFORMED = "malformed"
TOKEN_PRESENT = "present"


@dataclass(frozen=True)
class ApiPrincipal:
    role: str

    def allows(self, required_role):
        return ROLE_LEVELS[self.role] >= ROLE_LEVELS[required_role]


@dataclass(frozen=True)
class ApiTokenConfig:
    role: str
    token: str
    name: str | None = None


def _normalize_json_token_entry(entry, index):
    if not isinstance(entry, dict):
        raise ValueError(f"API token entry {index + 1} must be an object")

    role = entry.get("role")
    token = entry.get("token")
    name = entry.get("name")

    if role not in ROLE_LEVELS:
        raise ValueError(f"API token entry {index + 1} has an invalid role")
    if not isinstance(token, str) or not token.strip():
        raise ValueError(f"API token entry {index + 1} must include a non-empty token")
    if name is not None and not isinstance(name, str):
        raise ValueError(f"API token entry {index + 1} name must be a string")

    return ApiTokenConfig(role=role, token=token.strip(), name=name.strip() if isinstance(name, str) else None)


def _truthy_config_value(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def api_auth_disabled_allowed_for_config(config):
    env_name = (config.get("CONTROL_PLANE_ENV") or "").strip().lower()
    return env_name in LOCAL_AUTH_DISABLED_ENVS and _truthy_config_value(
        config.get("CONTROL_PLANE_ALLOW_AUTH_DISABLED", False)
    )


def _json_configured_api_tokens(config):
    raw_value = config.get("CONTROL_PLANE_API_TOKENS_JSON")
    if not isinstance(raw_value, str) or not raw_value.strip():
        return []

    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError("CONTROL_PLANE_API_TOKENS_JSON must be valid JSON") from exc

    if not isinstance(parsed, list):
        raise ValueError("CONTROL_PLANE_API_TOKENS_JSON must be a JSON array")

    return [_normalize_json_token_entry(entry, index) for index, entry in enumerate(parsed)]


def api_token_configs_from_config(config):
    configured = []
    configured.extend(_json_configured_api_tokens(config))
    for role, config_key in CONFIG_KEY_BY_ROLE.items():
        token = config.get(config_key)
        if isinstance(token, str) and token.strip():
            configured.append(ApiTokenConfig(role=role, token=token.strip(), name=config_key))
    return configured


def configured_api_tokens():
    return api_token_configs_from_config(current_app.config)


def configured_api_roles():
    roles = {token_config.role for token_config in configured_api_tokens()}
    return [role for role in ROLE_LEVELS if role in roles]


def api_auth_enabled():
    return bool(configured_api_tokens())


def _unauthorized_response(message):
    response = jsonify(error_payload(message))
    response.headers["WWW-Authenticate"] = "Bearer"
    return response, 401


def _missing_token_response():
    return _unauthorized_response("Missing bearer token")


def _invalid_token_response():
    return _unauthorized_response("Invalid bearer token")


def _malformed_token_response():
    return _unauthorized_response("Malformed bearer token")


def _forbidden_response():
    return jsonify(error_payload("Forbidden")), 403


def _extract_bearer_token():
    authorization = request.headers.get("Authorization")
    if not isinstance(authorization, str) or not authorization.strip():
        return TOKEN_MISSING, None

    parts = authorization.strip().split(None, 1)
    if len(parts) != 2:
        return TOKEN_MALFORMED, None
    scheme, token = parts
    if scheme.lower() != "bearer" or not token.strip():
        return TOKEN_MALFORMED, None
    return TOKEN_PRESENT, token.strip()


def authenticate_bearer_token(token):
    for token_config in configured_api_tokens():
        if hmac.compare_digest(token, token_config.token):
            return ApiPrincipal(role=token_config.role)
    return None


def _cached_request_principal():
    cached_role = request.environ.get("control_plane.api_principal_role")
    if not cached_role:
        return None
    return ApiPrincipal(role=cached_role)


def _cache_request_principal(principal):
    request.environ["control_plane.api_principal_role"] = principal.role


def current_request_actor_role():
    principal = _cached_request_principal()
    return principal.role if principal is not None else None


def _should_audit_denied_request():
    return request.path.startswith("/api/projects") and request.method in {"POST", "PATCH", "PUT", "DELETE"}


def _record_denied_request_audit(*, reason, required_role, actor_role=None):
    if not _should_audit_denied_request():
        return
    from backend.api.audit_service import record_audit_event

    record_audit_event(
        action="api.access_denied",
        resource_type="route",
        resource_id=request.path,
        status="failure",
        actor_role=actor_role,
        metadata={
            "reason": reason,
            "required_role": required_role,
            "method": request.method,
            "path": request.path,
        },
    )


def authorize_request(required_role):
    if not api_auth_enabled():
        return None

    principal = _cached_request_principal()
    if principal is None:
        token_status, token = _extract_bearer_token()
        if token_status == TOKEN_MISSING:
            _record_denied_request_audit(reason="missing_token", required_role=required_role)
            return _missing_token_response()
        if token_status == TOKEN_MALFORMED:
            _record_denied_request_audit(reason="malformed_token", required_role=required_role)
            return _malformed_token_response()
        principal = authenticate_bearer_token(token)
        if principal is None:
            _record_denied_request_audit(reason="invalid_token", required_role=required_role)
            return _invalid_token_response()
        _cache_request_principal(principal)

    if not principal.allows(required_role):
        _record_denied_request_audit(
            reason="insufficient_role",
            required_role=required_role,
            actor_role=principal.role,
        )
        return _forbidden_response()

    return None


def require_api_role(required_role):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            response = authorize_request(required_role)
            if response is not None:
                return response
            return view_func(*args, **kwargs)

        return wrapped

    return decorator
