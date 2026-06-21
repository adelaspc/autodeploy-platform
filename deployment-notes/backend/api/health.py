from flask import Blueprint, current_app, jsonify
from sqlalchemy import text

from backend.extensions import db


health_bp = Blueprint("health", __name__)


@health_bp.get("/health")
def health_check():
    return jsonify({"status": "ok"}), 200


@health_bp.get("/health/db")
def database_health_check():
    try:
        db.session.execute(text("SELECT 1"))
    except Exception as exc:
        current_app.logger.exception(
            "Database health check failed",
            extra={"event": "database_health_check_failed"},
        )
        return jsonify({"status": "error", "database": "unreachable", "details": str(exc)}), 503

    return jsonify({"status": "ok", "database": "reachable"}), 200
