from pathlib import Path

from flask import Flask, send_from_directory

from control_plane.api import register_blueprints
from control_plane.api.request_context import (
    attach_request_id_header,
    install_request_logging,
    log_request_completed,
    set_request_id_for_current_request,
)
from control_plane.config import Config
from control_plane.extensions import db, migrate
from control_plane.logging_config import install_structured_logging
from worker import run_reconciler, run_reconciler_loop_command, run_reconciler_once, run_worker, run_worker_once


def create_app(config_class=Config):
    app = Flask(__name__, static_folder="../frontend/dist", static_url_path="")
    app.config.from_object(config_class)
    init_app = getattr(config_class, "init_app", None)
    if callable(init_app):
        init_app(app)

    db.init_app(app)
    migrate.init_app(app, db)
    install_structured_logging(app)
    install_request_logging(app)

    @app.before_request
    def assign_request_id():
        set_request_id_for_current_request()

    @app.after_request
    def apply_request_context(response):
        response = attach_request_id_header(response)
        return log_request_completed(response)

    register_blueprints(app)
    app.cli.add_command(run_worker)
    app.cli.add_command(run_worker_once)
    app.cli.add_command(run_reconciler_once)
    app.cli.add_command(run_reconciler)
    app.cli.add_command(run_reconciler_loop_command)

    dist_dir = Path(app.static_folder or "")

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def serve_frontend(path):
        if path == "health" or path.startswith(("api/", "health/")):
            return {"error": "Not found"}, 404

        if path and (dist_dir / path).is_file():
            return send_from_directory(dist_dir, path)

        if dist_dir.is_dir():
            return send_from_directory(dist_dir, "index.html")

        return {"error": "Frontend build not available"}, 404

    return app
