from flask import Flask

from backend.api import register_blueprints
from backend.config import Config
from backend.extensions import db, migrate


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db)

    register_blueprints(app)

    return app

