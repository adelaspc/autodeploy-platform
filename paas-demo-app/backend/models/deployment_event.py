from datetime import datetime, timezone

from backend.extensions import db


class DeploymentEvent(db.Model):
    __tablename__ = "deployment_events"

    id = db.Column(db.Integer, primary_key=True)
    deployment_id = db.Column(db.Integer, db.ForeignKey("platform_deployments.id"), nullable=False, index=True)
    event_type = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(32), nullable=False)
    message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    deployment = db.relationship("PlatformDeployment", back_populates="events")

    def to_dict(self):
        return {
            "id": self.id,
            "deployment_id": self.deployment_id,
            "event_type": self.event_type,
            "status": self.status,
            "message": self.message,
            "created_at": self.created_at.isoformat(),
        }
