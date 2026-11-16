---
title: Docker and containers
section: backend
order: 64
phase: "Phase 9 · DevOps and observability"
summary: How containers isolate with namespaces and cgroups, how image layers work, a multi-stage non-root Python Dockerfile, healthchecks, Compose and image scanning.
---

A container is an ordinary Linux process that the kernel lies to: it sees its own process tree, network stack and filesystem, and it can only use the CPU and memory it was given. Docker packages that process with everything it needs into an image, so the same artifact runs on a laptop, in CI and in production. For a Django service this solves "works on my machine", pins the Python version and system libraries, and gives orchestrators like Kubernetes a uniform unit to schedule. It also brings its own failure modes: bloated images, processes that ignore `SIGTERM`, root inside the container, and CVEs inherited from the base image.

## Containers vs virtual machines

| | Container | Virtual machine |
|---|---|---|
| Isolation boundary | Shared host kernel; namespaces, cgroups, seccomp | Hypervisor; separate guest kernel |
| Start time | Milliseconds to seconds | Seconds to minutes |
| Overhead | Close to a normal process | Guest OS memory and CPU |
| Density | Hundreds per host | Tens per host |
| Security | Kernel bug can mean host escape | Stronger boundary |

When you need stronger isolation for untrusted code (multi-tenant job runners, customer-supplied plugins), sandboxed runtimes narrow the gap: gVisor intercepts syscalls in a user-space kernel, and Kata Containers and Firecracker run each container or function in a lightweight VM.

## Namespaces and cgroups

### Namespaces: what the process can see

Each namespace type virtualizes one global resource:

| Namespace | Isolates | Effect inside the container |
|---|---|---|
| PID | Process IDs | Your app is PID 1; host processes are invisible |
| Network | Interfaces, routes, ports, iptables | Own `eth0` and loopback; port 8000 doesn't clash with the host |
| Mount | Mount table | Own root filesystem from the image |
| UTS | Hostname | Container ID as hostname |
| IPC | Shared memory, semaphores | No cross-container shared memory |
| User | UID/GID mapping | Root inside can map to an unprivileged UID outside |
| Cgroup | Cgroup tree view | Sees only its own cgroup |
| Time | Monotonic and boot clocks | Rarely used by app containers |

You can see the mechanism without Docker. This is illustrative and needs root on a Linux host:

```bash
sudo unshare --pid --fork --mount-proc bash
ps aux        # only bash and ps are visible, bash is PID 1
```

A Kubernetes pod is a set of containers sharing a network namespace (hence `localhost` between a sidecar and the app) and optionally volumes.

### Cgroups: what the process can use

Control groups (cgroups v2 on current distributions) limit and account resources:

- `memory.max`: the hard limit. Exceed it and the kernel OOM killer kills a process in the group. The container exits with code **137** (128 + `SIGKILL`), shown as `OOMKilled`.
- `cpu.max`: a CFS quota such as `200000 100000` (200 ms of CPU per 100 ms period, two cores' worth). Exceeding it doesn't kill; the process is **throttled** until the next period, which shows up as latency spikes rather than errors.
- `pids.max`: caps process count, which stops fork bombs.
- `io.max`: block I/O limits.

Python gotcha: `os.cpu_count()` returns the host's CPU count, not the cgroup quota. A Gunicorn config using `(2 * cpu_count()) + 1` on a 64-core node with a 2-CPU limit starts 129 workers, each with its own memory, and gets OOM-killed or throttled. Set the worker count explicitly from configuration. Python 3.13 added `os.process_cpu_count()` and the `PYTHON_CPU_COUNT` override, but affinity is not quota, so explicit config is still the safe choice.

The rest of the isolation story: Linux capabilities (Docker drops most by default; drop the rest), seccomp syscall filters, and AppArmor or SELinux profiles.

## Images and layers

An image is a content-addressed bundle defined by the OCI image spec: a **manifest** listing a **config** (entrypoint, env, user, labels) and an ordered list of **layers**, each a tarball of filesystem changes identified by its SHA-256 digest. A multi-architecture image adds an index that points at one manifest per platform (amd64, arm64).

At runtime the layers are stacked with a union filesystem (overlayfs): image layers are read-only lower directories, and each container gets a thin writable upper layer. Reads fall through to the topmost layer that has the file; writes copy the file up first (copy-on-write). Deleting a file in a later layer adds a "whiteout" marker, so the bytes still ship in the earlier layer.

```text
 container A (rw)   container B (rw)    <- thin writable layers
        \                 /
   +---------------------------------+
   | COPY . /app          (app code) |  changes every commit
   | COPY /opt/venv       (deps)     |  changes when lockfile changes
   | apt-get libpq5, tini            |  rarely changes
   | python:3.12-slim base layers    |  shared by every image on host
   +---------------------------------+
        read-only, content-addressed, cached and shared
```

Consequences that matter:

- **Build cache is per instruction and cascades.** When one instruction's inputs change, it and every instruction after it rebuild. Put rarely changing steps (system packages, dependency install) before frequently changing ones (source code).
- **Layers are shared.** Ten services on the same base image store and pull the base once per node.
- **Secrets in any layer are permanent.** `COPY .npmrc` then `RUN rm .npmrc` still ships the file in the earlier layer. Use BuildKit secret mounts.
- **Tags are mutable, digests are not.** `python:3.12-slim` moves when Debian ships patches; `python@sha256:...` never changes. Pin by digest for reproducibility and let a bot update it.
- **`.dockerignore` matters.** Without it, `COPY . .` sends `.git`, virtualenvs and local `.env` files into the build context and possibly the image.

| Base image | Size | Notes |
|---|---|---|
| `python:3.12` | ~1 GB | Full Debian with compilers; fine as a build stage |
| `python:3.12-slim` | ~120–150 MB | Debian minimal; good default runtime |
| `python:3.12-alpine` | ~50 MB | musl libc; some wheels must compile from source, subtle runtime differences |
| Distroless or Wolfi-based | Small | No shell or package manager; smallest attack surface, harder to debug |

## A multi-stage Python Dockerfile

Multi-stage builds use a heavy builder stage with compilers and headers, then copy only the result into a slim runtime stage. Illustrative Dockerfile for a Django app using psycopg compiled against libpq:

```dockerfile
# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
# cache mount: pip's download cache survives between builds, not in the image
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --require-hashes -r requirements.txt

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=app:app . .
RUN DJANGO_SETTINGS_MODULE=config.settings.build \
    python manage.py collectstatic --noinput
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", \
     "--workers", "3", "--timeout", "30", "--max-requests", "2000", \
     "--max-requests-jitter", "200"]
```

Why each piece is there:

- **Compilers stay in the builder.** The runtime image gets the virtualenv and the shared library (`libpq5`), not `gcc` and headers. Typical result: 1 GB down to under 200 MB.
- **`requirements.txt` copied before the source**, so code changes don't reinstall dependencies.
- **`PYTHONUNBUFFERED=1`** so logs reach stdout immediately; containers should log to stdout and let the platform collect it.
- **Exec form `CMD` and `ENTRYPOINT`** (JSON arrays). Shell form wraps the command in `/bin/sh -c`, and the shell, as PID 1, doesn't forward `SIGTERM` to Gunicorn, so `docker stop` waits 10 seconds and then `SIGKILL`s mid-request.
- **`tini` as PID 1.** PID 1 has special signal semantics and must reap zombie children. Tini forwards signals and reaps; `docker run --init` does the same. Graceful shutdown details are in [graceful shutdown and health checks]({{ '/backend/graceful-shutdown-and-health-checks/' | relative_url }}).
- **`--max-requests` with jitter** recycles workers to contain slow memory growth without restarting them all at once.

## Running as non-root

By default the process in a container runs as root (UID 0). Without user namespace remapping, that is the same UID 0 as on the host, kept in check only by dropped capabilities, seccomp and the namespace boundary. One kernel or runtime bug, a mounted Docker socket or a writable host path, and it is root on the node.

- Create a fixed, unprivileged user and switch to it with `USER 10001`. A numeric UID lets Kubernetes verify `runAsNonRoot` without reading `/etc/passwd`.
- Give it ownership only of what it writes (`COPY --chown`); code can stay root-owned and read-only.
- Bind to ports above 1024 (8000), since low ports need `CAP_NET_BIND_SERVICE`.
- At runtime: `--read-only` root filesystem with a `tmpfs` for `/tmp`, `--cap-drop ALL`, `--security-opt no-new-privileges`. The Kubernetes equivalents live in the pod `securityContext` (`runAsNonRoot`, `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`).

## Healthchecks

The Dockerfile `HEALTHCHECK` runs a command inside the container on an interval and marks it `healthy` or `unhealthy`. Docker itself only reports the status; Compose uses it for `depends_on: condition: service_healthy`, and Swarm replaces unhealthy tasks.

- **Kubernetes ignores `HEALTHCHECK`.** It uses liveness, readiness and startup probes defined in the pod spec (see [Kubernetes]({{ '/backend/kubernetes/' | relative_url }})).
- Slim images have no `curl`; use the interpreter you already have, as in the Dockerfile above, rather than adding packages.
- Check the process, not the world. A healthcheck that queries Postgres marks every container unhealthy during a 5-second database blip. Use `start-period` for slow boots instead of loose thresholds.

## Docker Compose

Compose describes a multi-container application in one file and is the standard way to run a service and its dependencies locally and in CI. It is not a production orchestrator for multiple hosts: no scheduling across machines, no rolling updates across a fleet.

```yaml
# compose.yaml (illustrative local setup)
services:
  web:
    build: .
    command: python manage.py runserver 0.0.0.0:8000
    env_file: .env
    ports: ["8000:8000"]
    volumes:
      - .:/app                  # live code reload in development
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_started
  worker:
    build: .
    command: celery -A config worker --loglevel=info
    env_file: .env
    depends_on:
      db:
        condition: service_healthy
  db:
    image: postgres:16
    environment:
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: app
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d app"]
      interval: 5s
      timeout: 3s
      retries: 10
  redis:
    image: redis:7
volumes:
  pgdata:
```

Services reach each other by service name (`db:5432`, `redis:6379`) on the project network Compose creates. Named volumes keep database data across `docker compose down`; `down -v` deletes them. Profiles (`profiles: [observability]`) keep optional services like Jaeger or Grafana out of the default `up`.

## Image scanning

Every image inherits the CVEs of its base OS packages and its Python dependencies. Scanners (Trivy, Grype, Docker Scout, registry-native scanners) read the package databases and lockfiles inside the image, or its SBOM, and match versions against vulnerability feeds.

- **Scan in CI and continuously in the registry.** An image clean today has new CVEs next month without changing; rescan what is deployed.
- **Gate on what you can act on.** Fail the build on critical and high CVEs with a fix available (`--ignore-unfixed`); track the rest. Gating on every unfixable low-severity CVE in Debian trains people to ignore the scanner.
- **Shrink the surface.** Fewer packages, fewer findings: slim or distroless runtime stages remove shells, compilers and package managers you never call.
- **Scan more than CVEs.** Misconfiguration (running as root, `ADD` from URLs) with hadolint or Trivy config scanning, and secrets accidentally baked into layers.
- **Rebuild regularly** even without code changes, so base image patches land.

## In practice

Illustrative commands for inspecting, hardening and scanning an image (the `--format` arguments are Go templates):

{% raw %}
```bash
# build with BuildKit and a secret that never lands in a layer
docker build --secret id=pipconf,src=$HOME/.config/pip/pip.conf -t orders-api:dev .

# what made the image big, and which user it runs as
docker history orders-api:dev
docker image inspect --format '{{.Config.User}}' orders-api:dev

# run hardened, with explicit resource limits
docker run -d --name orders-api -p 8000:8000 \
  --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  --memory 512m --cpus 1.0 \
  orders-api:dev

# why did it die? exit 137 with OOMKilled=true means the memory limit
docker inspect --format '{{.State.ExitCode}} {{.State.OOMKilled}}' orders-api

# fail CI on fixable high and critical vulnerabilities
trivy image --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 orders-api:dev
```
{% endraw %}

## Pitfalls

- **`COPY . .` before dependency install.** Symptom: every commit reinstalls all packages, builds take minutes. Fix: copy the lockfile, install, then copy the source.
- **Secrets in build args or layers.** `ARG` values show in `docker history`; deleted files remain in earlier layers. Fix: `RUN --mount=type=secret`, runtime secrets from the platform.
- **Shell-form `CMD`.** Symptom: deploys take exactly 10 or 30 seconds per container and drop requests. Fix: exec form plus an init process.
- **Running as root.** Fix: `USER` with a numeric UID, enforced by `runAsNonRoot` in the cluster.
- **Exit 137 under load.** The memory limit is too low for the worker count, or a worker leaks. Fix: size workers to the limit, recycle workers, profile memory.
- **CPU throttling read as "slow code".** Fix: check `container_cpu_cfs_throttled_periods_total`, raise the limit or remove CPU limits for latency-sensitive services.
- **Writing logs to files inside the container.** They fill the writable layer and vanish with the container. Fix: stdout and stderr.
- **Deploying `:latest`.** Rollback and auditing become guesswork. Fix: SHA tags and digests.

## Interview questions

**What is a container, technically?** A normal process started with its own namespaces (PID, network, mount, UTS, IPC, user) so it sees an isolated system, in a cgroup that limits CPU, memory and PIDs, with a root filesystem assembled from image layers. It shares the host kernel, which is the key difference from a VM.

**How do image layers and the build cache work?** Each filesystem-changing instruction produces a content-addressed layer. Layers are stacked read-only with overlayfs plus a writable container layer. The build cache reuses a layer if the instruction and its inputs are unchanged; once one step changes, every step after it rebuilds, so order from least to most volatile.

**Why use multi-stage builds?** To keep build tools, headers and caches out of the runtime image. The final stage copies only the virtualenv or binary, which shrinks size, pull time and attack surface.

**Why does `docker stop` take 10 seconds for some containers?** PID 1 isn't handling `SIGTERM`, usually because shell-form `CMD` made `/bin/sh` PID 1 and it doesn't forward signals. Docker waits the grace period and sends `SIGKILL`. Use exec form and an init like tini.

**Why not run containers as root?** Without user namespace remapping, root in the container is UID 0 on the host, so any escape or misconfiguration (mounted socket, host path) gives host root. Run as a fixed non-root UID with dropped capabilities and a read-only root filesystem.

**What happens when a container exceeds its memory or CPU limit?** Memory: the OOM killer kills it (exit 137). CPU: it is throttled by the CFS quota and gets slower, with no error.

**Does Kubernetes use the Dockerfile `HEALTHCHECK`?** No. It uses probes from the pod spec. `HEALTHCHECK` matters for plain Docker, Compose dependency ordering and Swarm.

**How do you handle image vulnerabilities?** Small base images, scanning in CI with a gate on fixable high and critical issues, continuous rescans of deployed images, regular rebuilds to pick up patched bases, and SBOMs to answer "are we affected" quickly when a new CVE drops.

## Further reading

- Docker documentation: Multi-stage builds; Building best practices; Dockerfile reference.
- Open Container Initiative: Image Format Specification and Runtime Specification.
- Linux manual pages: `namespaces(7)` and `cgroups(7)`.
- *Container Security*, Liz Rice (O'Reilly, 2020).
