"""Probe the live runtime behind a deployment without changing persisted state."""

from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from control_plane.deployment_spec import project_for_deployment


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler()).open


def deployment_healthcheck_url(deployment):
    service_url = (deployment.service_url or "").strip().rstrip("/")
    healthcheck_path = (project_for_deployment(deployment).healthcheck_path or "/").strip()
    parsed = urlsplit(service_url)
    if (
        not service_url
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        return None
    try:
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return None
    except ValueError:
        return None
    if not healthcheck_path.startswith("/"):
        healthcheck_path = f"/{healthcheck_path}"
    return f"{service_url}{healthcheck_path}"


def probe_deployment_health(deployment, *, timeout=3, opener=_NO_REDIRECT_OPENER):
    checked_at = datetime.now(timezone.utc).isoformat()
    healthcheck_url = deployment_healthcheck_url(deployment)
    if deployment.status != "running":
        return {
            "status": "unavailable",
            "message": "Live health is checked only for running deployments.",
            "http_status": None,
            "checked_at": checked_at,
        }
    if not healthcheck_url:
        return {
            "status": "unavailable",
            "message": "No public healthcheck URL is available.",
            "http_status": None,
            "checked_at": checked_at,
        }

    request = Request(healthcheck_url, headers={"User-Agent": "autodeploy-control-plane-health-monitor/1.0"})
    try:
        with opener(request, timeout=timeout) as response:
            http_status = response.getcode()
    except HTTPError as exc:
        return {
            "status": "unhealthy",
            "message": f"Workload healthcheck returned HTTP {exc.code}.",
            "http_status": exc.code,
            "checked_at": checked_at,
        }
    except (URLError, TimeoutError, OSError):
        return {
            "status": "unhealthy",
            "message": "Workload healthcheck is unreachable.",
            "http_status": None,
            "checked_at": checked_at,
        }

    healthy = 200 <= http_status < 300
    return {
        "status": "healthy" if healthy else "unhealthy",
        "message": (
            f"Workload healthcheck returned HTTP {http_status}."
            if healthy
            else f"Workload healthcheck returned unexpected HTTP {http_status}."
        ),
        "http_status": http_status,
        "checked_at": checked_at,
    }
