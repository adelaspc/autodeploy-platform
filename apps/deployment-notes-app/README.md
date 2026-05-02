# Deployment Notes App

This project is an independent sample application. It is intended to represent a user-owned repository that the PaaS would clone, build, and deploy.

## Scope

- `GET/POST/PATCH/DELETE /api/deployments`
- `GET /health`
- `GET /health/db`
- Vue frontend for deployment record CRUD

## Database

The application uses its own database configuration:

- `DEPLOYMENT_NOTES_DATABASE_URL`
- `DEPLOYMENT_NOTES_ENV`

For local development, `DEPLOYMENT_NOTES_ENV=development` falls back to `sqlite:///instance/deployment_notes.db`.

The app does not import the control plane, its worker, or any platform models.
