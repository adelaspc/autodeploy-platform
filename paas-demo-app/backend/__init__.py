from flask import Flask

from backend.api import register_blueprints
from backend.config import Config
from backend.extensions import db, migrate
from worker import run_reconciler, run_worker, run_worker_once


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)
    init_app = getattr(config_class, "init_app", None)
    if callable(init_app):
        init_app(app)

    db.init_app(app)
    migrate.init_app(app, db)

    register_blueprints(app)
    app.cli.add_command(run_worker)
    app.cli.add_command(run_worker_once)
    app.cli.add_command(run_reconciler)

    return app
