from pathlib import Path

from flask import Flask, send_from_directory
from werkzeug.exceptions import NotFound
from werkzeug.middleware.proxy_fix import ProxyFix

from control_plane.api import register_blueprints
from control_plane.api.auth import (
    AUTH_DISABLED_ALLOWED_CONFIG_KEY,
    api_auth_disabled_allowed_for_config,
)
from control_plane.api.request_context import (
    attach_request_id_header,
    install_request_logging,
    log_request_completed,
    set_request_id_for_current_request,
)
from control_plane.config import Config, validate_numeric_config
from control_plane.extensions import db, migrate
from control_plane.logging_config import install_structured_logging
from control_plane.retention import cleanup_observability
from worker import (
    check_worker_readiness,
    run_reconciler,
    run_reconciler_loop_command,
    run_reconciler_once,
    run_worker,
    run_worker_once,
)


def create_app(config_class=None):
    """Build the Flask app used by the API, worker, and reconciler processes."""
    if config_class is None or config_class is Config:
        config_class = Config.from_env()
    app = Flask(__name__, static_folder="../frontend/dist", static_url_path="")
    app.config.from_object(config_class)
    init_app = getattr(config_class, "init_app", None)
    if callable(init_app):
        init_app(app)
    app.config[AUTH_DISABLED_ALLOWED_CONFIG_KEY] = api_auth_disabled_allowed_for_config(app.config)
    validate_numeric_config(app)
    trusted_proxy_count = app.config.get("CONTROL_PLANE_TRUSTED_PROXY_COUNT", 0)
    if trusted_proxy_count < 0:
        raise RuntimeError("CONTROL_PLANE_TRUSTED_PROXY_COUNT cannot be negative")
    if trusted_proxy_count:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=trusted_proxy_count)

    db.init_app(app)
    migrate.init_app(app, db)
    install_structured_logging(app)
    install_request_logging(app)

    @app.before_request
    def assign_request_id():
        set_request_id_for_current_request()

    @app.after_request
    def apply_request_context(response):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response = attach_request_id_header(response)
        return log_request_completed(response)

    register_blueprints(app)
    app.cli.add_command(run_worker)
    app.cli.add_command(run_worker_once)
    app.cli.add_command(check_worker_readiness)
    app.cli.add_command(run_reconciler_once)
    app.cli.add_command(run_reconciler)
    app.cli.add_command(run_reconciler_loop_command)
    app.cli.add_command(cleanup_observability)

    dist_dir = Path(app.static_folder or "")

    # In a packaged build Flask serves the Vue app as well as the API. Unknown
    # frontend paths fall back to index.html so client-side navigation still works.
    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def serve_frontend(path):
        if path == "health" or path.startswith(("api/", "health/")):
            return {"error": "Not found"}, 404

        if path:
            try:
                return send_from_directory(dist_dir, path)
            except NotFound:
                pass

        if dist_dir.is_dir():
            return send_from_directory(dist_dir, "index.html")

        return {"error": "Frontend build not available"}, 404

    return app
