from datetime import datetime, timezone

from control_plane.extensions import db
from control_plane.security import redact_sensitive_data, redact_text, secret_values_from_env_vars


class DeploymentEvent(db.Model):
    __tablename__ = "deployment_events"

    id = db.Column(db.Integer, primary_key=True)
    deployment_id = db.Column(db.Integer, db.ForeignKey("platform_deployments.id"), nullable=False, index=True)
    event_type = db.Column(db.String(120), nullable=False)
    step = db.Column(db.String(120), nullable=True)
    level = db.Column(db.String(16), nullable=False, default="info")
    status = db.Column(db.String(32), nullable=False)
    message = db.Column(db.Text, nullable=True)
    metadata_json = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    deployment = db.relationship("PlatformDeployment", back_populates="events")

    def to_dict(self):
        secret_values = secret_values_from_env_vars(
            self.deployment.project.env_vars if self.deployment and self.deployment.project else []
        )
        return {
            "id": self.id,
            "deployment_id": self.deployment_id,
            "event_type": self.event_type,
            "step": self.step,
            "level": self.level,
            "status": self.status,
            "message": redact_text(self.message, secret_values=secret_values) if self.message else None,
            "metadata_json": redact_sensitive_data(self.metadata_json, secret_values=secret_values),
            "created_at": self.created_at.isoformat(),
        }
