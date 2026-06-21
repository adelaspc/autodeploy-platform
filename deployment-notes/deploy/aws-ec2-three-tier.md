# AWS EC2 Three-Tier Deployment

This guide matches the target routing model:

```text
Browser
-> Public ALB HTTPS
-> Web tier EC2 Auto Scaling Group
   - Nginx
   - Vue static build
   - reverse proxy for /api to internal ALB
-> Internal ALB
-> App tier EC2 Auto Scaling Group
   - Gunicorn
   - Flask API only
-> RDS MySQL
```

The public ALB should target only the web tier. Do not route `/api` from the public ALB directly to app EC2 instances. Nginx on the web tier owns API proxying to the internal app ALB.

## Deployment Units

The repo keeps the original combined `Dockerfile` for local Docker Compose use. AWS deployment should use the split Dockerfiles:

- `frontend/Dockerfile`: builds Vue and serves it with Nginx.
- `backend/Dockerfile`: runs Flask/Gunicorn API only.
- `deploy/nginx/templates/default.conf.template`: web-tier Nginx config.

## Required Environment Variables

App tier:

```dotenv
DEPLOYMENT_NOTES_ENV=production
DEPLOYMENT_NOTES_SERVE_FRONTEND=false
DEPLOYMENT_NOTES_DATABASE_URL=mysql+pymysql://deployment_notes_user:REPLACE_WITH_SECRET_PASSWORD@REPLACE_WITH_RDS_ENDPOINT:3306/deployment_notes
FLASK_APP=wsgi.py
PORT=5000
GUNICORN_WORKERS=2
GUNICORN_THREADS=4
```

Web tier:

```dotenv
APP_INTERNAL_ALB_DNS=internal-deployment-notes-app-123456789.us-east-1.elb.amazonaws.com
```

Use Secrets Manager, SSM Parameter Store, EC2 user data, CodeDeploy environment files, or your CI/CD secret store for real values. Do not commit production secrets.

## Build Commands

Backend image:

```bash
docker build -f backend/Dockerfile -t deployment-notes-backend:latest .
```

Frontend image:

```bash
docker build -f frontend/Dockerfile -t deployment-notes-frontend:latest .
```

If you are not using Docker on EC2, the same split still applies: build `frontend/dist` and place it under the Nginx document root on web instances; install Python dependencies and run Gunicorn only on app instances.

## Suggested Runtime Commands

Backend EC2 container example:

```bash
docker run --rm -p 5000:5000 \
  -e DEPLOYMENT_NOTES_ENV=production \
  -e DEPLOYMENT_NOTES_SERVE_FRONTEND=false \
  -e DEPLOYMENT_NOTES_DATABASE_URL='mysql+pymysql://deployment_notes_user:REPLACE_WITH_SECRET_PASSWORD@REPLACE_WITH_RDS_ENDPOINT:3306/deployment_notes' \
  deployment-notes-backend:latest
```

Frontend EC2 container example:

```bash
docker run --rm -p 80:80 \
  -e APP_INTERNAL_ALB_DNS=internal-deployment-notes-app-123456789.us-east-1.elb.amazonaws.com \
  deployment-notes-frontend:latest
```

For systemd-based hosts, run the equivalent commands as services and configure the CloudWatch Agent to collect container logs or journald logs.

## ALB Layout

Public ALB:

| Listener | Target group | Targets | Health check |
| --- | --- | --- | --- |
| HTTPS `443` | web target group | Web EC2 ASG, port `80` | `GET /health` |
| HTTP `80` | redirect to HTTPS | none | none |

Internal ALB:

| Listener | Target group | Targets | Health check |
| --- | --- | --- | --- |
| HTTP `80` | app target group | App EC2 ASG, port `5000` | `GET /health` |

Nginx on the web tier proxies `/api/` to the internal ALB DNS name. The frontend continues to call relative paths such as `/api/deployments`, so browser traffic stays same-origin and CORS is not needed for the normal AWS deployment.

The web-tier `/health` endpoint is served by Nginx itself. The app-tier `/health` endpoint is lightweight and does not touch the database. The app-tier `/health/db` endpoint remains available for deeper diagnostics but should be restricted to operator access paths.

## Security Groups

| Source | Destination | Port | Purpose |
| --- | --- | --- | --- |
| Internet | Public ALB SG | `443` | Browser HTTPS traffic |
| Public ALB SG | Web EC2 SG | `80` | Serve Vue and receive proxied browser requests |
| Web EC2 SG | Internal ALB SG | `80` | Proxy `/api/` to app tier |
| Internal ALB SG | App EC2 SG | `5000` | Gunicorn/Flask API traffic |
| App EC2 SG | RDS SG | `3306` | MySQL connection |

Do not allow direct internet ingress to web EC2, app EC2, or RDS. Admin access should use SSM Session Manager or a controlled bastion pattern.

## RDS Notes

- Use MySQL-compatible RDS.
- Create the `deployment_notes` database and an application user with the least privileges needed by the app and migrations.
- Use private DB subnets only.
- Enable backups and deletion protection for non-demo environments.
- Store the database password outside the repo.
- Use `DEPLOYMENT_NOTES_DATABASE_URL=mysql+pymysql://...` on the app tier.

## Migration Workflow

Do not run migrations automatically on every app instance boot. Run migrations once per release:

```bash
flask --app wsgi.py db upgrade
```

Run this manually on one app host, through SSM Run Command, in a CodeDeploy hook, or from CI/CD with network access to RDS. The command needs the same production database environment variables as the app tier.

## Logging

Gunicorn in `backend/Dockerfile` writes access and error logs to stdout/stderr. On EC2, collect those logs with one of:

- CloudWatch Agent reading Docker/container logs.
- CloudWatch Agent reading systemd journal logs.
- A service manager that writes stdout/stderr to a known file path collected by CloudWatch Agent.

Nginx access/error logs from the web tier should also be collected. Keep ALB access logs enabled for the public and internal ALBs when troubleshooting routing or target health.
