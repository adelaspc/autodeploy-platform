from flask import Flask

from backend.api import register_blueprints
from backend.api.request_context import (
    attach_request_id_header,
    install_request_logging,
    log_request_completed,
    set_request_id_for_current_request,
)
from backend.config import Config
from backend.extensions import db, migrate
from worker import run_reconciler, run_reconciler_loop_command, run_worker, run_worker_once


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)
    init_app = getattr(config_class, "init_app", None)
    if callable(init_app):
        init_app(app)

    db.init_app(app)
    migrate.init_app(app, db)
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
    app.cli.add_command(run_reconciler)
    app.cli.add_command(run_reconciler_loop_command)

    return app
