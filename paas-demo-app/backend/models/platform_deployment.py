from datetime import datetime, timezone

from backend.extensions import db


class PlatformDeployment(db.Model):
    __tablename__ = "platform_deployments"
    VALID_STATUSES = (
        "pending",
        "cloning",
        "building",
        "testing",
        "pushing_image",
        "deploying",
        "running",
        "failed",
        "stopped",
    )
    STATUS_TRANSITIONS = {
        "pending": ("cloning", "failed", "stopped"),
        "cloning": ("building", "failed", "stopped"),
        "building": ("testing", "pushing_image", "failed", "stopped"),
        "testing": ("pushing_image", "failed", "stopped"),
        "pushing_image": ("deploying", "failed", "stopped"),
        "deploying": ("running", "failed", "stopped"),
        "running": ("deploying", "failed", "stopped"),
        "failed": (),
        "stopped": (),
    }

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    build_id = db.Column(db.Integer, db.ForeignKey("builds.id"), nullable=False, index=True)
    environment = db.Column(db.String(64), nullable=False, default="production")
    status = db.Column(db.String(32), nullable=False, default="pending")
    service_url = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    project = db.relationship("Project", back_populates="deployments")
    build = db.relationship("Build", back_populates="deployments")
    events = db.relationship("DeploymentEvent", back_populates="deployment", cascade="all, delete-orphan")

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
            "project_id": self.project_id,
            "build_id": self.build_id,
            "environment": self.environment,
            "status": self.status,
            "allowed_transitions": self.allowed_transitions,
            "service_url": self.service_url,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
