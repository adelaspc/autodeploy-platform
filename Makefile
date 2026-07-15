SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

PROFILE ?= demo
ENV_PROFILE := .env.$(PROFILE)
ENV_SECRETS := .env.secrets
PYTHON ?= .venv/bin/python
NPM ?= npm
COMPOSE ?= docker compose
DOCKER_SOCKET_GID := $(shell stat -c '%g' /var/run/docker.sock 2>/dev/null || printf '1002')
COMPOSE_KUBECONFIG := .runtime/kubeconfig.compose
COMPOSE_DOCKER_SOCKET_OVERRIDE := docker-compose.docker-socket.yml
GITLEAKS_VERSION := 8.30.1
export DOCKER_BUILDKIT ?= 1

.PHONY: help env-init require-env api db-upgrade worker-once worker reconciler reconciler-once reconciler-loop compose-up compose-recreate-runtime compose-config compose-toolcheck k8s-demo-check wsl-ingress-domain health-platform test lint secret-scan secret-scan-history frontend-test frontend-build

help:
	@printf '%s\n' \
		'Usage: make <target> [PROFILE=demo|local-docker|local-kubernetes]' \
		'' \
		'Environment targets:' \
		'  env-init          Create local ignored env profile files if missing' \
		'' \
		'Runtime targets:' \
		'  api               Run the Flask API with the selected profile' \
		'  db-upgrade        Apply database migrations with the selected profile' \
		'  worker-once       Process one pending deployment with the selected profile' \
		'  worker            Run the worker loop with the selected profile' \
		'  reconciler-once   Run one reconciliation pass with the selected profile' \
		'  reconciler-loop   Run the reconciler loop with the selected profile' \
		'  reconciler        Alias for reconciler-once' \
		'  health-platform   Call /health/platform using a configured API token when present' \
		'' \
		'Docker Compose targets:' \
		'  compose-up        Start the Compose stack with the selected profile and secrets' \
		'  compose-recreate-runtime Rebuild and recreate worker/reconciler with the selected profile' \
		'  compose-config    Render Compose config with the selected profile and secrets' \
		'  compose-toolcheck Verify required worker container CLIs are available' \
		'  k8s-demo-check    Verify local Kubernetes demo readiness from the worker' \
		'  wsl-ingress-domain Print the nip.io base domain for the current WSL IP' \
		'' \
		'Quality targets:' \
		'  test              Run backend tests' \
		'  lint              Run Ruff checks' \
		'  secret-scan       Scan the working tree for secrets with Gitleaks' \
		'  secret-scan-history Scan all Git history for secrets with Gitleaks' \
		'  frontend-test     Run frontend unit tests' \
		'  frontend-build    Build the frontend'

env-init:
	@umask 077; if [[ ! -f .env.demo ]]; then \
		cp .env.example .env.demo; \
		printf 'created .env.demo\n'; \
	else \
		printf 'exists  .env.demo\n'; \
	fi
	@umask 077; if [[ ! -f .env.local-docker ]]; then \
		cp .env.local-docker.example .env.local-docker; \
		printf 'created .env.local-docker\n'; \
	else \
		printf 'exists  .env.local-docker\n'; \
	fi
	@umask 077; if [[ ! -f .env.local-kubernetes ]]; then \
		cp .env.local-kubernetes.example .env.local-kubernetes; \
		printf 'created .env.local-kubernetes\n'; \
	else \
		printf 'exists  .env.local-kubernetes\n'; \
	fi
	@umask 077; if [[ ! -f .env.secrets ]]; then \
		cp .env.secrets.example .env.secrets; \
		printf 'created .env.secrets\n'; \
	else \
		printf 'exists  .env.secrets\n'; \
	fi
	@chmod 0600 .env.demo .env.local-docker .env.local-kubernetes .env.secrets
	@command -v openssl >/dev/null || { printf 'openssl is required to generate local database passwords\n' >&2; exit 1; }; \
	for key in CONTROL_PLANE_MYSQL_PASSWORD CONTROL_PLANE_MYSQL_ROOT_PASSWORD; do \
		if grep -q "^$${key}=$$" .env.secrets; then \
			value="$$(openssl rand -hex 24)"; \
			sed -i "s|^$${key}=$$|$${key}=$${value}|" .env.secrets; \
			printf 'generated %s\n' "$$key"; \
		fi; \
	done

require-env:
	@if [[ ! -f "$(ENV_PROFILE)" ]]; then \
		printf 'Missing %s. Run: make env-init\n' "$(ENV_PROFILE)" >&2; \
		exit 1; \
	fi
	@if [[ ! -f "$(ENV_SECRETS)" ]]; then \
		printf 'Missing %s. Run: make env-init\n' "$(ENV_SECRETS)" >&2; \
		exit 1; \
	fi

api: require-env
	@set -a; source "$(ENV_PROFILE)"; source "$(ENV_SECRETS)"; set +a; \
	$(PYTHON) -m flask --app wsgi:app run --debug

db-upgrade: require-env
	@set -a; source "$(ENV_PROFILE)"; source "$(ENV_SECRETS)"; set +a; \
	$(PYTHON) -m flask --app wsgi:app db upgrade

worker-once: require-env
	@set -a; source "$(ENV_PROFILE)"; source "$(ENV_SECRETS)"; set +a; \
	$(PYTHON) -m flask --app wsgi:app run-worker-once

worker: require-env
	@set -a; source "$(ENV_PROFILE)"; source "$(ENV_SECRETS)"; set +a; \
	$(PYTHON) -m flask --app wsgi:app run-worker

reconciler: reconciler-once

reconciler-once: require-env
	@set -a; source "$(ENV_PROFILE)"; source "$(ENV_SECRETS)"; set +a; \
	$(PYTHON) -m flask --app wsgi:app run-reconciler-once

reconciler-loop: require-env
	@set -a; source "$(ENV_PROFILE)"; source "$(ENV_SECRETS)"; set +a; \
	$(PYTHON) -m flask --app wsgi:app run-reconciler-loop

compose-up: require-env
	@set -a; source "$(ENV_PROFILE)"; source "$(ENV_SECRETS)"; set +a; \
	compose_files=(-f docker-compose.yml); \
	case "$${CONTROL_PLANE_COMPOSE_DOCKER_SOCKET_ENABLED:-false}" in 1|true|yes|on) \
		compose_files+=(-f "$(COMPOSE_DOCKER_SOCKET_OVERRIDE)"); \
		export CONTROL_PLANE_DOCKER_SOCKET_GID="$(DOCKER_SOCKET_GID)"; \
		;; \
	esac; \
	if [[ "$${CONTROL_PLANE_EXECUTOR:-}" = "kubernetes" && -n "$${CONTROL_PLANE_KUBECONFIG_HOST:-}" ]]; then \
		sh scripts/prepare-compose-kubeconfig.sh "$${CONTROL_PLANE_KUBECONFIG_HOST}" "$(COMPOSE_KUBECONFIG)" "$${CONTROL_PLANE_KUBECONFIG_CONTAINER_SERVER:-https://host.docker.internal:16443}"; \
		export CONTROL_PLANE_KUBECONFIG_HOST="$(COMPOSE_KUBECONFIG)"; \
	fi; \
	export CONTROL_PLANE_ENV_FILE="$(ENV_PROFILE)"; \
	export CONTROL_PLANE_KUBECONFIG_GID="$$(if [[ -n "$${CONTROL_PLANE_KUBECONFIG_HOST:-}" && -e "$${CONTROL_PLANE_KUBECONFIG_HOST}" ]]; then stat -c '%g' "$${CONTROL_PLANE_KUBECONFIG_HOST}"; else printf '0'; fi)"; \
	$(COMPOSE) "$${compose_files[@]}" --env-file "$(ENV_PROFILE)" --env-file "$(ENV_SECRETS)" up --build

compose-recreate-runtime: require-env
	@set -a; source "$(ENV_PROFILE)"; source "$(ENV_SECRETS)"; set +a; \
	compose_files=(-f docker-compose.yml); \
	case "$${CONTROL_PLANE_COMPOSE_DOCKER_SOCKET_ENABLED:-false}" in 1|true|yes|on) \
		compose_files+=(-f "$(COMPOSE_DOCKER_SOCKET_OVERRIDE)"); \
		export CONTROL_PLANE_DOCKER_SOCKET_GID="$(DOCKER_SOCKET_GID)"; \
		;; \
	esac; \
	if [[ "$${CONTROL_PLANE_EXECUTOR:-}" = "kubernetes" && -n "$${CONTROL_PLANE_KUBECONFIG_HOST:-}" ]]; then \
		sh scripts/prepare-compose-kubeconfig.sh "$${CONTROL_PLANE_KUBECONFIG_HOST}" "$(COMPOSE_KUBECONFIG)" "$${CONTROL_PLANE_KUBECONFIG_CONTAINER_SERVER:-https://host.docker.internal:16443}"; \
		export CONTROL_PLANE_KUBECONFIG_HOST="$(COMPOSE_KUBECONFIG)"; \
	fi; \
	export CONTROL_PLANE_ENV_FILE="$(ENV_PROFILE)"; \
	export CONTROL_PLANE_KUBECONFIG_GID="$$(if [[ -n "$${CONTROL_PLANE_KUBECONFIG_HOST:-}" && -e "$${CONTROL_PLANE_KUBECONFIG_HOST}" ]]; then stat -c '%g' "$${CONTROL_PLANE_KUBECONFIG_HOST}"; else printf '0'; fi)"; \
	$(COMPOSE) "$${compose_files[@]}" --env-file "$(ENV_PROFILE)" --env-file "$(ENV_SECRETS)" up -d --build --no-deps --force-recreate control-plane-worker control-plane-reconciler

compose-config: require-env
	@set -a; source "$(ENV_PROFILE)"; source "$(ENV_SECRETS)"; set +a; \
	compose_files=(-f docker-compose.yml); \
	case "$${CONTROL_PLANE_COMPOSE_DOCKER_SOCKET_ENABLED:-false}" in 1|true|yes|on) \
		compose_files+=(-f "$(COMPOSE_DOCKER_SOCKET_OVERRIDE)"); \
		export CONTROL_PLANE_DOCKER_SOCKET_GID="$(DOCKER_SOCKET_GID)"; \
		;; \
	esac; \
	if [[ "$${CONTROL_PLANE_EXECUTOR:-}" = "kubernetes" && -n "$${CONTROL_PLANE_KUBECONFIG_HOST:-}" ]]; then \
		sh scripts/prepare-compose-kubeconfig.sh "$${CONTROL_PLANE_KUBECONFIG_HOST}" "$(COMPOSE_KUBECONFIG)" "$${CONTROL_PLANE_KUBECONFIG_CONTAINER_SERVER:-https://host.docker.internal:16443}"; \
		export CONTROL_PLANE_KUBECONFIG_HOST="$(COMPOSE_KUBECONFIG)"; \
	fi; \
	export CONTROL_PLANE_ENV_FILE="$(ENV_PROFILE)"; \
	export CONTROL_PLANE_KUBECONFIG_GID="$$(if [[ -n "$${CONTROL_PLANE_KUBECONFIG_HOST:-}" && -e "$${CONTROL_PLANE_KUBECONFIG_HOST}" ]]; then stat -c '%g' "$${CONTROL_PLANE_KUBECONFIG_HOST}"; else printf '0'; fi)"; \
	$(COMPOSE) "$${compose_files[@]}" --env-file "$(ENV_PROFILE)" --env-file "$(ENV_SECRETS)" config

compose-toolcheck: require-env
	@set -a; source "$(ENV_PROFILE)"; source "$(ENV_SECRETS)"; set +a; \
	compose_files=(-f docker-compose.yml); \
	case "$${CONTROL_PLANE_COMPOSE_DOCKER_SOCKET_ENABLED:-false}" in 1|true|yes|on) \
		compose_files+=(-f "$(COMPOSE_DOCKER_SOCKET_OVERRIDE)"); \
		export CONTROL_PLANE_DOCKER_SOCKET_GID="$(DOCKER_SOCKET_GID)"; \
		;; \
	esac; \
	$(COMPOSE) "$${compose_files[@]}" --env-file "$(ENV_PROFILE)" --env-file "$(ENV_SECRETS)" exec control-plane-worker sh -lc 'set -eu; id; command -v git; command -v kubectl; kubectl version --client=true; case "$${CONTROL_PLANE_COMPOSE_DOCKER_SOCKET_ENABLED:-false}" in 1|true|yes|on) ls -ln /var/run/docker.sock; command -v docker; docker --version; docker buildx version; test "$${DOCKER_BUILDKIT:-}" = "1";; *) test ! -S /var/run/docker.sock;; esac; if test "$${CONTROL_PLANE_EXECUTOR:-}" = "kubernetes"; then test -n "$${CONTROL_PLANE_KUBECONFIG:-}" && test -s "$${CONTROL_PLANE_KUBECONFIG}" && grep -q "^apiVersion:" "$${CONTROL_PLANE_KUBECONFIG}"; if test "$${CONTROL_PLANE_K8S_DEPLOYMENT_MODE:-manifest}" = "helm"; then command -v helm; helm version --short; fi; fi'

wsl-ingress-domain:
	@ip="$$(hostname -I | awk '{print $$1}')"; \
	if [[ -z "$$ip" ]]; then printf 'Unable to detect the WSL IP\n' >&2; exit 1; fi; \
	printf 'CONTROL_PLANE_K8S_INGRESS_BASE_DOMAIN=%s.nip.io\n' "$$ip"

k8s-demo-check: require-env
	@set -a; source "$(ENV_PROFILE)"; source "$(ENV_SECRETS)"; set +a; \
	if [[ "$${CONTROL_PLANE_EXECUTOR:-}" != "kubernetes" ]]; then \
		printf 'CONTROL_PLANE_EXECUTOR must be kubernetes for k8s-demo-check\n' >&2; \
		exit 1; \
	fi; \
	case "$${CONTROL_PLANE_COMPOSE_DOCKER_SOCKET_ENABLED:-false}" in 1|true|yes|on) ;; *) \
		printf 'CONTROL_PLANE_COMPOSE_DOCKER_SOCKET_ENABLED must be true for k8s-demo-check\n' >&2; \
		exit 1; \
		;; \
	esac; \
	compose_files=(-f docker-compose.yml -f "$(COMPOSE_DOCKER_SOCKET_OVERRIDE)"); \
	if [[ -n "$${CONTROL_PLANE_KUBECONFIG_HOST:-}" ]]; then \
		sh scripts/prepare-compose-kubeconfig.sh "$${CONTROL_PLANE_KUBECONFIG_HOST}" "$(COMPOSE_KUBECONFIG)" "$${CONTROL_PLANE_KUBECONFIG_CONTAINER_SERVER:-https://host.docker.internal:16443}"; \
		export CONTROL_PLANE_KUBECONFIG_HOST="$(COMPOSE_KUBECONFIG)"; \
	fi; \
	export CONTROL_PLANE_ENV_FILE="$(ENV_PROFILE)"; \
	export CONTROL_PLANE_DOCKER_SOCKET_GID="$(DOCKER_SOCKET_GID)"; \
	export CONTROL_PLANE_KUBECONFIG_GID="$$(if [[ -n "$${CONTROL_PLANE_KUBECONFIG_HOST:-}" && -e "$${CONTROL_PLANE_KUBECONFIG_HOST}" ]]; then stat -c '%g' "$${CONTROL_PLANE_KUBECONFIG_HOST}"; else printf '0'; fi)"; \
	$(COMPOSE) "$${compose_files[@]}" --env-file "$(ENV_PROFILE)" --env-file "$(ENV_SECRETS)" exec control-plane-worker sh -lc 'set -eu; \
		command -v git >/dev/null; \
		command -v docker >/dev/null; \
		command -v kubectl >/dev/null; \
		if test "$${CONTROL_PLANE_K8S_DEPLOYMENT_MODE:-manifest}" = "helm"; then command -v helm >/dev/null; helm version --short >/dev/null; fi; \
		docker buildx version >/dev/null; \
		test "$${DOCKER_BUILDKIT:-}" = "1"; \
		test -n "$${CONTROL_PLANE_KUBECONFIG:-}"; \
		test -s "$${CONTROL_PLANE_KUBECONFIG}"; \
		grep -q "^apiVersion:" "$${CONTROL_PLANE_KUBECONFIG}"; \
		test "$$(kubectl --kubeconfig "$${CONTROL_PLANE_KUBECONFIG}" --namespace "$${CONTROL_PLANE_K8S_NAMESPACE:-default}" get --raw=/readyz)" = "ok"; \
		test -n "$${CONTROL_PLANE_K8S_IMAGE_PULL_SECRET:-}"; \
		kubectl --kubeconfig "$${CONTROL_PLANE_KUBECONFIG}" --namespace "$${CONTROL_PLANE_K8S_NAMESPACE:-default}" get secret "$${CONTROL_PLANE_K8S_IMAGE_PULL_SECRET}" >/dev/null; \
		if test "$${CONTROL_PLANE_K8S_INGRESS_ENABLED:-false}" = "true"; then \
			kubectl --kubeconfig "$${CONTROL_PLANE_KUBECONFIG}" api-resources --api-group=networking.k8s.io --output=name | grep -q "^ingresses\(.networking.k8s.io\)\?$$"; \
			if test -n "$${CONTROL_PLANE_K8S_INGRESS_CLASS_NAME:-}"; then kubectl --kubeconfig "$${CONTROL_PLANE_KUBECONFIG}" get ingressclass "$${CONTROL_PLANE_K8S_INGRESS_CLASS_NAME}" >/dev/null; fi; \
			host="$$(kubectl --kubeconfig "$${CONTROL_PLANE_KUBECONFIG}" --namespace "$${CONTROL_PLANE_K8S_NAMESPACE:-default}" get ingress -o jsonpath="{.items[-1].spec.rules[0].host}" 2>/dev/null || true)"; \
			if test -n "$$host"; then HOST="$$host" python -c '"'"'import os, socket, urllib.request; host=os.environ["HOST"]; socket.gethostbyname(host); urllib.request.urlopen("http://" + host, timeout=5).read(1)'"'"'; printf "ingress check passed: http://%s\n" "$$host"; else printf "ingress check skipped: no Ingress resources found\n"; fi; \
		fi; \
		printf "k8s demo check passed\n"'

health-platform: require-env
	@set -a; source "$(ENV_PROFILE)"; source "$(ENV_SECRETS)"; set +a; \
	token="$${CONTROL_PLANE_API_TOKEN_ADMIN:-$${CONTROL_PLANE_API_TOKEN_DEPLOYER:-$${CONTROL_PLANE_API_TOKEN_READ_ONLY:-}}}"; \
	if [[ -n "$$token" ]]; then \
		curl -fsS -H "Authorization: Bearer $$token" "http://127.0.0.1:$${PORT:-5000}/health/platform"; \
	else \
		curl -fsS "http://127.0.0.1:$${PORT:-5000}/health/platform"; \
	fi; \
	printf '\n'

test:
	$(PYTHON) -m pytest tests

lint:
	$(PYTHON) -m ruff check control_plane worker wsgi.py

secret-scan:
	@command -v gitleaks >/dev/null || { printf 'gitleaks is required: https://github.com/gitleaks/gitleaks#installing\n' >&2; exit 1; }
	@gitleaks version | grep -Fx '$(GITLEAKS_VERSION)' >/dev/null || { printf 'gitleaks $(GITLEAKS_VERSION) is required\n' >&2; exit 1; }
	@scan_dir="$$(mktemp -d)"; trap 'rm -rf "$$scan_dir"' EXIT; \
	git ls-files --cached --others --exclude-standard -z | tar --null --files-from=- --create | tar --extract --directory "$$scan_dir"; \
	gitleaks dir "$$scan_dir" --redact

secret-scan-history:
	@command -v gitleaks >/dev/null || { printf 'gitleaks is required: https://github.com/gitleaks/gitleaks#installing\n' >&2; exit 1; }
	@gitleaks version | grep -Fx '$(GITLEAKS_VERSION)' >/dev/null || { printf 'gitleaks $(GITLEAKS_VERSION) is required\n' >&2; exit 1; }
	gitleaks git . --redact

frontend-test:
	cd frontend && $(NPM) run test

frontend-build:
	cd frontend && $(NPM) run build
