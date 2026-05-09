from datetime import datetime, timezone

from backend.extensions import db


class WebhookDelivery(db.Model):
    __tablename__ = "webhook_deliveries"

    id = db.Column(db.Integer, primary_key=True)
    delivery_id = db.Column(db.String(255), nullable=False, unique=True, index=True)
    event_type = db.Column(db.String(64), nullable=False)
    repository_url = db.Column(db.String(255), nullable=True)
    branch = db.Column(db.String(255), nullable=True)
    commit_sha = db.Column(db.String(64), nullable=True)
    status = db.Column(db.String(32), nullable=False)
    reason = db.Column(db.String(64), nullable=True)
    deployment_id = db.Column(db.Integer, db.ForeignKey("platform_deployments.id"), nullable=True, index=True)
    received_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    deployment = db.relationship("PlatformDeployment")

    def to_dict(self):
        return {
            "id": self.id,
            "delivery_id": self.delivery_id,
            "event_type": self.event_type,
            "repository_url": self.repository_url,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "status": self.status,
            "reason": self.reason,
            "deployment_id": self.deployment_id,
            "received_at": self.received_at.isoformat(),
        }
