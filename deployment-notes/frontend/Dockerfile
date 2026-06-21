FROM node:22-bookworm-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY deploy/nginx/templates/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=frontend-builder /app/frontend/dist /usr/share/nginx/html
EXPOSE 80
