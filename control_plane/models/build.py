from datetime import datetime, timezone

from control_plane.extensions import db
from control_plane.security import redact_text, secret_values_from_env_vars


class Build(db.Model):
    __tablename__ = "builds"
    VALID_STATUSES = ("pending", "cloning", "building", "testing", "pushing_image", "succeeded", "failed")
    VALID_REGISTRY_PUSH_STATUSES = ("skipped", "succeeded", "failed")

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    commit_sha = db.Column(db.String(64), nullable=False)
    registry = db.Column(db.String(255), nullable=True)
    image_name = db.Column(db.String(255), nullable=True)
    image_tag = db.Column(db.String(255), nullable=True)
    image_ref = db.Column(db.String(512), nullable=True)
    status = db.Column(db.String(32), nullable=False, default="pending")
    registry_push_status = db.Column(db.String(32), nullable=True)
    test_command = db.Column(db.String(255), nullable=True)
    workspace_path = db.Column(db.String(1024), nullable=True)
    log_path = db.Column(db.String(1024), nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    project = db.relationship("Project", back_populates="builds")
    deployments = db.relationship("PlatformDeployment", back_populates="build", cascade="all, delete-orphan")

    def to_dict(self):
        secret_values = secret_values_from_env_vars(self.project.env_vars if self.project else [])
        return {
            "id": self.id,
            "project_id": self.project_id,
            "commit_sha": self.commit_sha,
            "registry": self.registry,
            "image_name": self.image_name,
            "image_tag": self.image_tag,
            "image_ref": self.image_ref,
            "status": self.status,
            "registry_push_status": self.registry_push_status,
            "test_command": self.test_command,
            "workspace_path": self.workspace_path,
            "log_path": self.log_path,
            "last_error": redact_text(self.last_error, secret_values=secret_values) if self.last_error else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
