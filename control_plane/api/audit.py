from flask import Blueprint, jsonify, request

from control_plane.api.audit_service import list_audit_events, serialize_audit_events_page
from control_plane.api.auth import require_api_role
from control_plane.api.request_parsing import parse_limit_arg, parse_optional_int_arg


audit_bp = Blueprint("audit", __name__, url_prefix="/api/audit-events")


@audit_bp.get("")
@require_api_role("read_only")
def get_audit_events():
    limit, error_response, status_code = parse_limit_arg("limit", default=20, request=request)
    if error_response is not None:
        return error_response, status_code
    before_id, error_response, status_code = parse_optional_int_arg("before_id", request=request, min_value=1)
    if error_response is not None:
        return error_response, status_code

    events = list_audit_events(limit=limit, before_id=before_id)
    return jsonify(serialize_audit_events_page(events, limit=limit))
