from datetime import datetime, timezone

from control_plane.extensions import db
from control_plane.deployment_spec import project_for_deployment
from control_plane.security import redact_sensitive_data, redact_text, secret_values_from_env_vars


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
        "running": ("failed", "stopped"),
        "failed": ("stopped",),
        "stopped": (),
    }

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    build_id = db.Column(db.Integer, db.ForeignKey("builds.id"), nullable=False, index=True)
    environment = db.Column(db.String(64), nullable=False, default="production")
    status = db.Column(db.String(32), nullable=False, default="pending")
    deploy_target = db.Column(db.String(255), nullable=True)
    container_name = db.Column(db.String(255), nullable=True)
    container_id = db.Column(db.String(255), nullable=True)
    host_port = db.Column(db.Integer, nullable=True)
    healthcheck_url = db.Column(db.String(1024), nullable=True)
    service_url = db.Column(db.String(255), nullable=True)
    helm_release_name = db.Column(db.String(255), nullable=True)
    helm_namespace = db.Column(db.String(255), nullable=True)
    helm_chart_path = db.Column(db.String(1024), nullable=True)
    preflight_status = db.Column(db.String(32), nullable=True)
    preflight_summary = db.Column(db.Text, nullable=True)
    preflight_metadata_json = db.Column(db.JSON, nullable=True)
    spec_snapshot_json = db.Column(db.JSON, nullable=True)
    preflight_completed_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    claimed_at = db.Column(db.DateTime, nullable=True)
    claimed_by = db.Column(db.String(255), nullable=True)
    origin_request_id = db.Column(db.String(128), nullable=True, index=True)
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
    commands = db.relationship("DeploymentCommand", back_populates="deployment", cascade="all, delete-orphan")

    @property
    def allowed_transitions(self):
        return list(self.STATUS_TRANSITIONS.get(self.status, ()))

    def can_transition_to(self, next_status):
        if next_status == self.status:
            return True

        return next_status in self.STATUS_TRANSITIONS.get(self.status, ())

    def transition_to(self, next_status):
        if not self.can_transition_to(next_status):
            raise ValueError(f"Invalid deployment transition from {self.status} to {next_status}")
        self.status = next_status

    def to_dict(self):
        secret_values = secret_values_from_env_vars(project_for_deployment(self).env_vars)
        latest_command = max(self.commands, key=lambda command: command.id, default=None)
        return {
            "id": self.id,
            "project_id": self.project_id,
            "build_id": self.build_id,
            "environment": self.environment,
            "status": self.status,
            "allowed_transitions": self.allowed_transitions,
            "deploy_target": self.deploy_target,
            "container_name": self.container_name,
            "container_id": self.container_id,
            "host_port": self.host_port,
            "healthcheck_url": self.healthcheck_url,
            "service_url": self.service_url,
            "helm_release_name": self.helm_release_name,
            "helm_namespace": self.helm_namespace,
            "helm_chart_path": self.helm_chart_path,
            "preflight_status": self.preflight_status,
            "preflight_summary": self.preflight_summary,
            "preflight_metadata_json": redact_sensitive_data(
                self.preflight_metadata_json,
                secret_values=secret_values,
            ),
            "preflight_completed_at": self.preflight_completed_at.isoformat() if self.preflight_completed_at else None,
            "last_error": redact_text(self.last_error, secret_values=secret_values) if self.last_error else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "claimed_at": self.claimed_at.isoformat() if self.claimed_at else None,
            "claimed_by": self.claimed_by,
            "origin_request_id": self.origin_request_id,
            "latest_command": latest_command.to_dict() if latest_command else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
