from backend.api.deployments import deployments_bp
from backend.api.health import health_bp
from backend.api.projects import projects_bp


def register_blueprints(app):
    app.register_blueprint(health_bp)
    app.register_blueprint(deployments_bp)
    app.register_blueprint(projects_bp)
