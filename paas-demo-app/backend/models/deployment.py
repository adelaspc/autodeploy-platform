from datetime import datetime, timezone

from backend.extensions import db


class Deployment(db.Model):
    __tablename__ = "deployments"
    VALID_ENVIRONMENTS = ("development", "staging", "production")
    VALID_STATUSES = ("pending", "building", "deployed", "failed")
    STATUS_TRANSITIONS = {
        "pending": ("building", "failed"),
        "building": ("deployed", "failed"),
        "deployed": (),
        "failed": (),
    }

    __table_args__ = (
        db.UniqueConstraint(
            "application_name",
            "version",
            "environment",
            name="uq_deployment_app_version_environment",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    application_name = db.Column(db.String(120), nullable=False)
    version = db.Column(db.String(64), nullable=False)
    environment = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(32), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    @property
    def allowed_transitions(self):
        return list(self.STATUS_TRANSITIONS.get(self.status, ()))

    def can_transition_to(self, next_status):
        if next_status == self.status:
            return True

        return next_status in self.STATUS_TRANSITIONS.get(self.status, ())

    def to_dict(self):
        return {
            "id": self.id,
            "application_name": self.application_name,
            "version": self.version,
            "environment": self.environment,
            "status": self.status,
            "allowed_transitions": self.allowed_transitions,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
