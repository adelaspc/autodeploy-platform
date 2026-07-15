from datetime import datetime, timezone

from control_plane.extensions import db
from control_plane.security import serialized_project_env_vars


class Project(db.Model):
    __tablename__ = "projects"
    VALID_TRIGGERS = ("manual", "github_push")
    VALID_RUNTIMES = ("dockerfile",)
    VALID_GIT_AUTH_TYPES = ("none", "token")

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    repo_url = db.Column(db.String(255), nullable=False)
    branch = db.Column(db.String(120), nullable=False, default="main")
    git_auth_type = db.Column(db.String(32), nullable=False, default="none")
    git_secret_ref = db.Column(db.String(120), nullable=True)
    dockerfile_path = db.Column(db.String(255), nullable=False, default="Dockerfile")
    build_context = db.Column(db.String(255), nullable=False, default=".")
    port = db.Column(db.Integer, nullable=False)
    healthcheck_path = db.Column(db.String(255), nullable=False)
    env_vars = db.Column(db.JSON, nullable=False, default=list)
    default_test_command = db.Column(db.String(255), nullable=True)
    migration_command = db.Column(db.String(255), nullable=True)
    cpu = db.Column(db.String(32), nullable=True)
    memory = db.Column(db.String(32), nullable=True)
    trigger = db.Column(db.String(32), nullable=False, default="manual")
    runtime = db.Column(db.String(32), nullable=False, default="dockerfile")
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    builds = db.relationship("Build", back_populates="project", cascade="all, delete-orphan")
    deployments = db.relationship("PlatformDeployment", back_populates="project", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "repo_url": self.repo_url,
            "branch": self.branch,
            "git_auth_type": self.git_auth_type,
            "git_secret_ref": self.git_secret_ref,
            "dockerfile_path": self.dockerfile_path,
            "build_context": self.build_context,
            "port": self.port,
            "healthcheck_path": self.healthcheck_path,
            "env_vars": serialized_project_env_vars(self.env_vars),
            "default_test_command": self.default_test_command,
            "migration_command": self.migration_command,
            "cpu": self.cpu,
            "memory": self.memory,
            "trigger": self.trigger,
            "runtime": self.runtime,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
