---
title: Docker essentials
section: production
order: 6
summary: How image layers and caching work, a multi-stage Dockerfile for Django, .dockerignore, running as non-root, healthchecks, and Compose for local dev.
---

A container image is the unit you build once and run everywhere: CI, staging, production, a teammate's laptop. A good image builds in seconds when only code changed, is a few hundred MB instead of 1.5 GB, runs as an unprivileged user and tells the orchestrator when it is actually ready. A bad one rebuilds every dependency on every commit and ships your `.env` to the registry.

## How it works

### Layers and the build cache

Each instruction in a Dockerfile (`FROM`, `RUN`, `COPY`, `ADD`) produces a read-only **layer**. The image is a stack of layers; a container adds a thin writable layer on top.

Docker caches each layer keyed by the instruction and its inputs. For `COPY`, the input is the checksum of the copied files. When a layer's cache is invalidated, **every layer after it rebuilds**. So order instructions from least to most frequently changing:

```text
FROM python:3.12-slim          rarely changes
apt-get install system deps    rarely
COPY requirements.txt          when deps change
pip install                    when deps change   (slow: cache this)
COPY . .                       every commit
```

If you `COPY . .` before `pip install`, any code change reinstalls all dependencies.

Layers are additive. Deleting a file in a later layer hides it but doesn't shrink the image. Clean up in the **same** `RUN` that created the mess.

### Multi-stage builds

A multi-stage Dockerfile uses one stage with compilers and headers to build wheels, then copies only the results into a slim runtime stage. Build tools (`gcc`, `libpq-dev`, `build-essential`) never reach production. Typical result for a Django app: ~1.2 GB single-stage → ~250 MB multi-stage.

## In practice

### A production Dockerfile for Django

```dockerfile
# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/home/app/.local/bin:$PATH"
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels
COPY --chown=app:app . .
ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA
RUN DJANGO_SETTINGS_MODULE=app.settings.build \
    python manage.py collectstatic --noinput
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"
CMD ["gunicorn", "app.wsgi:application", "--bind", "0.0.0.0:8000", \
     "--workers", "4", "--timeout", "60", "--access-logfile", "-"]
```

Notes:

- `PYTHONUNBUFFERED=1` so logs reach stdout immediately instead of sitting in a buffer when the container dies.
- `libpq-dev` in the builder, only the runtime `libpq5` in the final image.
- `--mount=type=cache` keeps pip's download cache across builds (BuildKit) without baking it into a layer.
- `GIT_SHA` as a build arg feeds Sentry's `release` and the `version` log field.
- Exec-form `CMD` (JSON array) so gunicorn is PID 1 and receives `SIGTERM` directly for graceful shutdown. Shell form wraps it in `/bin/sh -c`, which doesn't forward signals.
- Same image, different command for workers: `python manage.py run_worker` or `celery -A app worker`.

### .dockerignore

The build context is everything sent to the builder. Without a `.dockerignore`, `COPY . .` includes your virtualenv, git history, and secrets, and any change to them busts the cache.

```text
.git
.gitignore
**/__pycache__
**/*.pyc
.venv
venv
node_modules
.env
.env.*
*.sqlite3
media/
staticfiles/
.pytest_cache
.coverage
docker-compose*.yml
Dockerfile*
```

### Non-root

By default containers run as root. If an attacker gets code execution through a deserialization bug or a vulnerable dependency, root in the container makes container escape and host damage much easier. Create a user with a fixed UID, `chown` what it needs to write, and switch with `USER`. Most orchestrators can also enforce `runAsNonRoot`.

Gotchas: binding to ports below 1024 needs root (use 8000), and mounted volumes must be writable by that UID.

### Healthchecks

Two different questions:

- **Liveness**: is the process alive and not deadlocked? If not, restart it. Keep it trivial: a `/healthz` that returns 200 without touching the DB.
- **Readiness**: can it serve traffic now? Check the DB connection and required migrations. If not, stop sending traffic but don't restart.

Do not put the database in the liveness check. A 30-second DB blip would restart every pod at once and turn a short hiccup into an outage. Docker's `HEALTHCHECK` is a single check; Kubernetes ignores it and uses its own `livenessProbe` / `readinessProbe`.

### Compose for local dev

```yaml
services:
  db:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: app
      POSTGRES_DB: app
    ports: ["5432:5432"]
    volumes: [pgdata:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U app -d app"]
      interval: 5s
      retries: 10

  redis:
    image: redis:7-alpine

  web:
    build: .
    command: python manage.py runserver 0.0.0.0:8000
    env_file: .env
    environment:
      DATABASE_URL: postgres://app:app@db:5432/app
    ports: ["8000:8000"]
    volumes: [".:/app"]
    depends_on:
      db:
        condition: service_healthy

  worker:
    build: .
    command: python manage.py run_worker
    env_file: .env
    environment:
      DATABASE_URL: postgres://app:app@db:5432/app
    volumes: [".:/app"]
    depends_on:
      db:
        condition: service_healthy

volumes:
  pgdata:
```

- `depends_on` with `condition: service_healthy` waits for Postgres to accept connections, not just for the container to start.
- Bind-mounting `.` gives live reload; production images never mount source.
- Service names are DNS names on the Compose network: the app connects to `db:5432`, not `localhost`.

## Pitfalls

- **Cache busting** from copying code before installing deps, or from a `.git` folder in the context.
- **Secrets in layers.** `COPY .env` or `ARG API_KEY` stays in image history even if removed later. Use runtime env vars, a secrets manager, or BuildKit `--mount=type=secret`.
- **`latest` tags.** You can't tell what is running or roll back reliably. Tag images with the git SHA.
- **Alpine with Python.** musl libc means many wheels aren't prebuilt; builds get slow and occasionally behave differently. `-slim` (Debian) is the safer default.
- **Signals.** Shell-form `CMD` swallows `SIGTERM`; the orchestrator waits out the grace period and `SIGKILL`s mid-request. Use exec form and set gunicorn's `--graceful-timeout`.
- **Running migrations in the container's start command** on every replica, racing each other. Run them as a separate one-off job.
- **Huge images** from leaving apt lists, build tools and pip caches in layers.

## Checklist

- Deps before code; `.dockerignore` in place.
- Multi-stage: build tools only in the builder.
- `-slim` base, pinned version, image tagged with git SHA.
- Non-root `USER` with a fixed UID.
- Exec-form `CMD`, `PYTHONUNBUFFERED=1`.
- Liveness cheap, readiness checks dependencies.
- No secrets in the image.

## Interview angle

- **"Why multi-stage builds?"** Smaller, safer runtime images: compilers and build deps stay in the builder stage; only wheels and app code ship.
- **"How does layer caching work?"** Each instruction is a layer keyed by its inputs; a change invalidates that layer and all after it, so put rarely changing steps first.

Related: [CI/CD and deploy strategies]({{ '/production/ci-cd-and-deploys/' | relative_url }}).
