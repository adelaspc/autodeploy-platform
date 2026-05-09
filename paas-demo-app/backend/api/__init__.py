from backend.api.health import health_bp
from backend.api.projects import projects_bp
from backend.api.webhooks import webhooks_bp


def register_blueprints(app):
    app.register_blueprint(health_bp)
    app.register_blueprint(projects_bp)
    app.register_blueprint(webhooks_bp)
