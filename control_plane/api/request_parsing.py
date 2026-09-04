"""Parse shared pagination and filtering parameters at the HTTP boundary."""

from flask import jsonify


def parse_limit_arg(name, *, default, request, min_value=1, max_value=100):
    value = request.args.get(name, default=default, type=int)
    if value is None or value < min_value or value > max_value:
        return None, jsonify({"error": f"{name} must be an integer between {min_value} and {max_value}"}), 400
    return value, None, None


def parse_optional_int_arg(name, *, request, min_value=1):
    raw_value = request.args.get(name, type=str)
    if raw_value is None:
        return None, None, None
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None, jsonify({"error": f"{name} must be an integer greater than or equal to {min_value}"}), 400
    if value < min_value:
        return None, jsonify({"error": f"{name} must be an integer greater than or equal to {min_value}"}), 400
    return value, None, None


def parse_status_filter_arg(name, *, request, allowed_values):
    raw_value = request.args.get(name, type=str)
    if raw_value is None:
        return None, None, None

    values = [item.strip() for item in raw_value.split(",") if item.strip()]
    if not values:
        allowed_list = ", ".join(sorted(allowed_values))
        return None, jsonify({"error": f"{name} must include at least one of: {allowed_list}"}), 400

    invalid = sorted({item for item in values if item not in allowed_values})
    if invalid:
        allowed_list = ", ".join(sorted(allowed_values))
        return None, jsonify({"error": f"{name} contains invalid values: {', '.join(invalid)}. Allowed: {allowed_list}"}), 400
    return values, None, None


def parse_bool_arg(name, *, request):
    raw_value = request.args.get(name, type=str)
    if raw_value is None:
        return None, None, None

    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True, None, None
    if normalized in {"0", "false", "no", "off"}:
        return False, None, None
    return None, jsonify({"error": f"{name} must be a boolean"}), 400
