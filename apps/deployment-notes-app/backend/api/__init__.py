from backend.api.deployments import deployments_bp
from backend.api.health import health_bp


def register_blueprints(app):
    app.register_blueprint(health_bp)
    app.register_blueprint(deployments_bp)
