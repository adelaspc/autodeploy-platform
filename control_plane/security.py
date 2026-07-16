from __future__ import annotations

import re
from collections.abc import Mapping, Sequence


REDACTED = "[REDACTED]"
LOG_SECRET_PATTERNS = (
    re.compile(r"(?i)(\bbearer\s+)([A-Za-z0-9._~+/=-]+)"),
    re.compile(r"(?i)(\b(?:authorization|password|passwd|pwd|token|api[_-]?key)\b\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^\s:/@]+:)([^\s/@]+)(@)"),
)
SENSITIVE_KEY_PARTS = {
    "authorization",
    "password",
    "token",
    "api_key",
    "private_key",
}
NON_SENSITIVE_SECRET_REFERENCE_KEYS = {
    "git_secret_ref",
    "secret_refs_used",
    "image_pull_secret",
}


def normalize_project_env_vars(env_vars):
    normalized = []
    for item in env_vars or []:
        if not isinstance(item, Mapping):
            normalized.append(item)
            continue
        current = dict(item)
        if current.get("value_source") == "secret_key_ref":
            current["is_secret"] = True
        elif "is_secret" in current:
            current["is_secret"] = bool(current["is_secret"])
        normalized.append(current)
    return normalized


def env_var_is_secret(item):
    if not isinstance(item, Mapping):
        return False
    if item.get("value_source") == "secret_key_ref":
        return True
    return bool(item.get("is_secret"))


def serialize_project_env_var(item):
    if not isinstance(item, Mapping):
        return item

    current = dict(item)
    secret = env_var_is_secret(current)
    if secret:
        current["is_secret"] = True
        if current.get("value") is not None:
            current["value"] = REDACTED
    return current


def serialized_project_env_vars(env_vars):
    return [serialize_project_env_var(item) for item in normalize_project_env_vars(env_vars)]


def secret_values_from_env_vars(env_vars):
    values = []
    for item in normalize_project_env_vars(env_vars):
        if not isinstance(item, Mapping):
            continue
        if env_var_is_secret(item) and item.get("value") is not None:
            values.append(str(item["value"]))
    return tuple(value for value in values if value)


def redact_text(text, *, secret_values=()):
    sanitized = str(text)
    for value in secret_values:
        if value:
            sanitized = sanitized.replace(value, REDACTED)
    return sanitized


def redact_log_text(text, *, secret_values=()):
    """Best-effort redaction for untrusted exception and infrastructure log text."""
    sanitized = redact_text(text, secret_values=secret_values)
    for pattern in LOG_SECRET_PATTERNS:
        sanitized = pattern.sub(
            lambda match: f"{match.group(1)}{REDACTED}{match.group(3) if match.lastindex == 3 else ''}",
            sanitized,
        )
    return sanitized


def _is_sensitive_key(key):
    lowered = str(key).strip().lower()
    if lowered in NON_SENSITIVE_SECRET_REFERENCE_KEYS or lowered.endswith("_ref") or lowered.endswith("_refs"):
        return False
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def redact_sensitive_data(value, *, secret_values=()):
    if value is None:
        return None
    if isinstance(value, Mapping):
        sanitized = {}
        for key, item in value.items():
            if key == "env_vars" and isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
                sanitized[key] = serialized_project_env_vars(item)
                continue
            if _is_sensitive_key(key):
                sanitized[key] = REDACTED
                continue
            sanitized[key] = redact_sensitive_data(item, secret_values=secret_values)
        return sanitized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_sensitive_data(item, secret_values=secret_values) for item in value]
    if isinstance(value, str):
        return redact_text(value, secret_values=secret_values)
    return value
