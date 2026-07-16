from datetime import datetime, timezone

from control_plane.extensions import db
from control_plane.deployment_spec import project_for_deployment
from control_plane.security import redact_text, secret_values_from_env_vars


class DeploymentCommand(db.Model):
    __tablename__ = "deployment_commands"
    __table_args__ = (
        db.UniqueConstraint(
            "deployment_id",
            "command_type",
            "active_key",
            name="uq_deployment_commands_active_type",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    deployment_id = db.Column(db.Integer, db.ForeignKey("platform_deployments.id"), nullable=False, index=True)
    command_type = db.Column(db.String(32), nullable=False, index=True)
    status = db.Column(db.String(32), nullable=False, default="pending", index=True)
    active_key = db.Column(db.String(16), nullable=True, default="active")
    message = db.Column(db.Text, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    claimed_by = db.Column(db.String(255), nullable=True)
    claimed_at = db.Column(db.DateTime, nullable=True)
    requested_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    completed_at = db.Column(db.DateTime, nullable=True)

    deployment = db.relationship("PlatformDeployment", back_populates="commands")

    def to_dict(self):
        secret_values = secret_values_from_env_vars(
            project_for_deployment(self.deployment).env_vars if self.deployment else []
        )
        return {
            "id": self.id,
            "deployment_id": self.deployment_id,
            "command_type": self.command_type,
            "status": self.status,
            "message": self.message,
            "last_error": redact_text(self.last_error, secret_values=secret_values) if self.last_error else None,
            "claimed_by": self.claimed_by,
            "claimed_at": self.claimed_at.isoformat() if self.claimed_at else None,
            "requested_at": self.requested_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }
