from pathlib import Path

from flask import Flask, send_from_directory

from backend.api import register_blueprints
from backend.config import Config
from backend.extensions import db, migrate
from backend.logging import configure_json_logging


def create_app(config_class=Config):
    app = Flask(__name__, static_folder="../frontend/dist", static_url_path="")
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    app.config.from_object(config_class)
    init_app = getattr(config_class, "init_app", None)
    if callable(init_app):
        init_app(app)
    configure_json_logging(app)

    db.init_app(app)
    migrate.init_app(app, db)

    register_blueprints(app)

    if not app.config.get("SERVE_FRONTEND"):
        return app

    dist_dir = Path(app.static_folder or "")

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def serve_frontend(path):
        if path and (dist_dir / path).is_file():
            return send_from_directory(dist_dir, path)

        if dist_dir.is_dir():
            return send_from_directory(dist_dir, "index.html")

        return {"error": "Frontend build not available"}, 404

    return app
