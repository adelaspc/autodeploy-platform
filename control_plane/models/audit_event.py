from datetime import datetime, timezone

from control_plane.extensions import db
from control_plane.security import redact_sensitive_data


class AuditEvent(db.Model):
    __tablename__ = "audit_events"

    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(120), nullable=False, index=True)
    actor_role = db.Column(db.String(32), nullable=True, index=True)
    resource_type = db.Column(db.String(64), nullable=False, index=True)
    resource_id = db.Column(db.String(255), nullable=True, index=True)
    status = db.Column(db.String(32), nullable=False, index=True)
    request_id = db.Column(db.String(255), nullable=True)
    ip_address = db.Column(db.String(255), nullable=True)
    metadata_json = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "action": self.action,
            "actor_role": self.actor_role,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "status": self.status,
            "request_id": self.request_id,
            "ip_address": self.ip_address,
            "metadata_json": redact_sensitive_data(self.metadata_json),
            "created_at": self.created_at.isoformat(),
        }
