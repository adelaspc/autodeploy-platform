from control_plane.api.audit import audit_bp
from control_plane.api.health import health_bp
from control_plane.api.metrics import metrics_bp
from control_plane.api.projects import projects_bp
from control_plane.api.webhooks import webhooks_bp


def register_blueprints(app):
    app.register_blueprint(audit_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(metrics_bp)
    app.register_blueprint(projects_bp)
    app.register_blueprint(webhooks_bp)
