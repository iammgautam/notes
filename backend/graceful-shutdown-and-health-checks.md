---
title: Graceful shutdown and health checks
section: backend
order: 61
phase: "Phase 8 · Performance and reliability"
summary: Handle SIGTERM, drain connections and in-flight work, tune preStop and grace periods, and design liveness, readiness and startup probes safely.
---

Every deploy, autoscaler scale-down and node drain kills processes on purpose. If the process dies mid-request, users see `502`s on every release; if a worker dies mid-task, jobs half-run. Health checks are the other half of the contract: they tell the platform when a process can take traffic and when it is beyond saving. Done well, both are invisible. Done badly, a deploy causes errors and a database blip makes the orchestrator restart your entire fleet at once.

## Graceful shutdown

### What graceful means

On a termination signal a well-behaved server:

1. Stops being routed new work: fails readiness, deregisters, or waits for the load balancer to notice.
2. Stops accepting new connections and stops consuming new messages.
3. Finishes in-flight requests and tasks, up to a deadline.
4. Closes idle keep-alive connections and tells clients on long-lived ones (WebSockets, SSE) to reconnect elsewhere.
5. Releases resources: commits consumer offsets, closes DB pools, flushes logs, traces and metrics buffers.
6. Exits with status 0 before the platform's hard kill.

### SIGTERM handling and PID 1

Orchestrators ask nicely with `SIGTERM`, wait a grace period, then send `SIGKILL`, which can't be caught. Three things go wrong regularly:

- **No handler.** Python's default `SIGTERM` action terminates the process immediately: no `finally` blocks, no `atexit`, no flushed buffers. Servers like gunicorn, uvicorn and Celery install their own handlers. Custom consumers and scripts must install one.
- **PID 1 ignores it.** The Linux kernel doesn't apply default signal actions to PID 1 in a container. A process that runs as PID 1 without a `SIGTERM` handler ignores the signal and sits there until `SIGKILL` 30 seconds later. Shutdowns that always take exactly the grace period are the symptom.
- **A shell in the middle.** Dockerfile shell form (`CMD gunicorn app.wsgi`) runs your server under `/bin/sh -c`, which may not forward signals. Use exec form (`CMD ["gunicorn", ...]`), end entrypoint scripts with `exec "$@"`, and consider a tiny init (`tini`, or `docker run --init`) to forward signals and reap zombies.

Keep the handler minimal: set a flag or an event and return. Do the actual shutdown work in the main loop, since a signal handler can run between any two bytecodes.

### Kubernetes termination: preStop and the grace period

```text
 t=0s   pod marked Terminating
        |-- kubelet runs preStop (sleep 10) --+   in parallel:
        |                                     |   endpoints controller
        |                                     |   removes the pod; proxies,
        |                                     |   ingress and mesh catch up
 t=10s  preStop ends -> SIGTERM to PID 1 of each container
        app: stop accepting, finish in-flight, close pools, exit 0
 t=45s  terminationGracePeriodSeconds (counted from t=0) -> SIGKILL
```

The key race: removing the pod from Service endpoints and signaling the container happen **concurrently**. kube-proxy rules, ingress controllers and cloud load balancers take seconds to stop sending traffic. If the app closes its listener the instant `SIGTERM` arrives, requests routed during that window hit a closed port and become `502`s. A `preStop` sleep of 5–15 seconds delays `SIGTERM` until routing has caught up.

- Recent Kubernetes versions support a native `sleep` action for `preStop` (beta and on by default since 1.30). On older clusters use `exec: command: ["sleep", "10"]`, which needs a `sleep` binary in the image; distroless images don't have one.
- `terminationGracePeriodSeconds` (default 30) covers the preStop hook **and** the app's own drain. Budget it: preStop 10 s + gunicorn `graceful_timeout` 25 s + margin = 45 s.
- In Kubernetes the endpoint is removed because the pod is terminating, whatever its readiness probe says. Flipping readiness to failing on `SIGTERM` still matters for load balancers that health-check targets directly.
- AWS target groups have their own deregistration delay (300 seconds by default), which should cover your longest normal request, not the default.

### Draining connections and in-flight requests

- **HTTP keep-alive.** Clients and load balancers reuse idle connections. During drain, send `Connection: close` on responses and close idle connections so clients reconnect to healthy pods. Separately, keep the server's keep-alive timeout **longer** than the load balancer's idle timeout. Otherwise the server closes a connection just as the LB sends a request on it, a classic source of sporadic `502`s even without deploys.
- **Long requests.** Uploads, exports and streaming responses may outlive any reasonable grace period. Move them to async jobs (see [Asynchronous processing and thread pools]({{ '/backend/async-processing-and-thread-pools/' | relative_url }})) or make clients resume.
- **WebSockets and SSE.** Send a close frame (status 1001, "going away") or a final event, and have clients reconnect with jittered backoff so 10,000 sockets don't land on the remaining pods in the same second.

### Worker shutdown

| Process | On SIGTERM | Notes |
|---|---|---|
| Gunicorn | Graceful: stop accepting, wait up to `graceful_timeout` for workers' current requests | `SIGINT`/`SIGQUIT` mean quick shutdown |
| Uvicorn | Stop accepting, finish in-flight, run ASGI lifespan shutdown | `--timeout-graceful-shutdown` caps the wait |
| Celery | Warm shutdown: stop consuming, finish running tasks | A second signal forces a cold shutdown; Celery 5.5 added a configurable soft-shutdown phase |
| Kafka consumer | Your code: stop polling, finish the batch, commit offsets, close | Closing leaves the group promptly, so rebalancing starts sooner |

Long tasks need a decision: raise the grace period for worker deployments (minutes are fine for workers that aren't behind a load balancer), or make tasks checkpointed and idempotent so a killed task can be redelivered and resumed. With `acks_late`, a task killed mid-run is redelivered to another worker, which is safe only if it is idempotent; see [Idempotency]({{ '/backend/idempotency/' | relative_url }}).

## Health checks

### Liveness, readiness and startup

| Probe | Question | On failure | Should check |
|---|---|---|---|
| Liveness | Is this process hopelessly stuck? | Container restarted | Only the process itself: can it answer at all? |
| Readiness | Should this pod get traffic right now? | Removed from Service endpoints, not restarted | Warm-up done, not draining, local capacity |
| Startup | Has the app finished booting? | Restarted after `failureThreshold × periodSeconds` | Same as liveness; holds off the other probes until it passes |

Kubernetes defaults are `periodSeconds: 10`, `timeoutSeconds: 1`, `failureThreshold: 3`. Probes can be `httpGet` (any 2xx or 3xx passes), `tcpSocket`, `exec` or `grpc`. A process that crashes cleanly on fatal errors may not need a liveness probe at all. Liveness exists for the wedged-but-running case: a deadlocked thread pool, an event loop blocked forever.

### Deep vs shallow checks

- **Shallow:** the process answers HTTP. Cheap, no dependencies, and right for liveness.
- **Deep:** also checks Postgres, Redis, the broker and downstream services. Useful for dashboards, synthetic monitoring and on-call diagnosis at a separate endpoint. Dangerous as a probe.

The question to ask of any check: if this dependency fails, does restarting this pod or removing it from rotation help? For a shared database the answer is no. Every pod sees the same failure, so the check turns a partial outage into a total one.

### Dependency check pitfalls

- **Liveness that checks the database.** A 20-second Postgres failover fails liveness on every pod, Kubernetes restarts the whole fleet, and the restarted pods come back with cold caches and a thundering herd of new connections. The outage outlives the blip.
- **Readiness that checks a shared dependency.** All pods go unready together, the Service has zero endpoints, and clients get connection errors instead of the degraded responses you could have served from cache. AWS ALBs fail open when every target is unhealthy (routing to all of them) for exactly this reason; Kubernetes Services don't.
- **Expensive checks.** A probe running `SELECT count(*)` every 5 seconds on 200 pods is constant load. Keep probes trivial or cache the result of a deep check for a few seconds.
- **Probes that compete with traffic.** With gunicorn sync workers, a probe waits behind real requests. Under load, the probe times out after its 1-second default, the pod is restarted, capacity drops, and load per pod rises: a death spiral. Raise `timeoutSeconds` and `failureThreshold` for liveness, or answer probes before the app's main work queue.
- **Probes rejected by the app.** Django's `ALLOWED_HOSTS` returns `400` for the kubelet's `Host: <pod-ip>` header; auth, tenancy and rate-limit middleware can reject probes too. Answer health paths in the first middleware.
- **Readiness that never recovers.** If readiness fails on overload, make sure it passes again once load drops, or you shed capacity exactly when you need it.

## In practice

### Deployment wiring (illustrative)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  template:
    spec:
      terminationGracePeriodSeconds: 45        # preStop 10 + drain 25 + margin
      containers:
        - name: api
          image: registry.example.com/api:1.42.0
          command: ["gunicorn", "app.wsgi", "-c", "gunicorn.conf.py"]
          ports:
            - containerPort: 8000
          lifecycle:
            preStop:
              sleep:
                seconds: 10                    # let endpoint removal propagate
          startupProbe:
            httpGet: {path: /livez, port: 8000}
            periodSeconds: 5
            failureThreshold: 24               # allow up to 120 s to boot
          livenessProbe:
            httpGet: {path: /livez, port: 8000}
            periodSeconds: 10
            timeoutSeconds: 3
            failureThreshold: 3
          readinessProbe:
            httpGet: {path: /readyz, port: 8000}
            periodSeconds: 5
            timeoutSeconds: 2
            failureThreshold: 2
```

### Probe endpoints in Django (illustrative)

```python
import threading
from django.http import HttpResponse

APP_READY = threading.Event()        # set after warm-up (e.g. in AppConfig.ready)


class HealthCheckMiddleware:
    """First in MIDDLEWARE: answers probes before host checks, auth and tenancy."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/livez":
            return HttpResponse("ok")
        if request.path == "/readyz":
            if APP_READY.is_set():
                return HttpResponse("ready")
            return HttpResponse("warming up", status=503)
        return self.get_response(request)
```

Deep checks (`SELECT 1` on Postgres, `PING` on Redis, broker reachability, each with a short timeout) belong at a separate authenticated endpoint that dashboards and synthetic monitors call.

### SIGTERM, readiness and draining, runnable

```python
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class State:
    def __init__(self):
        self.ready = True
        self.inflight = 0
        self.lock = threading.Lock()
        self.stop = threading.Event()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        state = self.server.state
        if self.path == "/livez":                 # shallow: the process answers
            return self.reply(200, b"ok")
        if self.path == "/readyz":                # fails while draining
            return self.reply(200 if state.ready else 503,
                              b"ready" if state.ready else b"draining")
        with state.lock:
            state.inflight += 1
        try:
            time.sleep(0.5)                       # a slow request already in flight
            self.reply(200, b"done")
        finally:
            with state.lock:
                state.inflight -= 1

    def reply(self, status, body):
        self.send_response(status)
        if not self.server.state.ready:
            self.send_header("Connection", "close")   # push keep-alive clients away
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def graceful_shutdown(server, state, drain=0.2, deadline=5.0):
    state.stop.wait()               # set by the SIGTERM handler
    time.sleep(drain)               # let load balancers see /readyz fail
    server.shutdown()               # stop accepting new connections
    end = time.monotonic() + deadline
    while state.inflight and time.monotonic() < end:
        time.sleep(0.01)            # finish in-flight work, up to a deadline
    server.server_close()
    # real services: close DB pools, commit offsets, flush logs and metrics here
    return state.inflight == 0


def get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except urllib.error.URLError:
        return None                 # connection refused: the server is gone


if __name__ == "__main__":
    state = State()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.state = state
    port = server.server_address[1]

    def on_sigterm(signum, frame):  # keep handlers tiny: flip flags and return
        state.ready = False
        state.stop.set()

    signal.signal(signal.SIGTERM, on_sigterm)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    assert get(port, "/readyz") == 200 and get(port, "/livez") == 200

    results = {}
    client = threading.Thread(target=lambda: results.update(slow=get(port, "/report")))
    client.start()
    time.sleep(0.1)                               # the request is now in flight
    os.kill(os.getpid(), signal.SIGTERM)          # what the kubelet sends
    assert state.stop.wait(2)

    assert get(port, "/readyz") == 503            # out of rotation...
    assert get(port, "/livez") == 200             # ...but not a restart candidate
    assert graceful_shutdown(server, state)
    client.join()
    assert results["slow"] == 200                 # in-flight request completed
    assert get(port, "/readyz") is None           # listener closed
    print("drained cleanly: in-flight request finished, new connections refused")
```

## Pitfalls

- **502s on every deploy.** No preStop delay, so the app stops listening before routing updates. Add a preStop sleep and budget the grace period.
- **Shutdown always takes exactly 30 seconds.** The process is PID 1 without a handler, or a shell isn't forwarding the signal. Use exec form, `tini`, or install a handler.
- **Grace period shorter than the drain.** Gunicorn waits 30 s by default, but the pod is killed at 30 s including preStop, so long requests get cut off anyway. Make the numbers add up.
- **Sporadic 502s unrelated to deploys.** Server keep-alive timeout shorter than the load balancer's idle timeout. Make the server's longer.
- **Lost work on worker shutdown.** Tasks acked on receipt and killed mid-run vanish. Use late acks with idempotent tasks, and give workers a grace period that matches task length.
- **Flushed nothing.** Logs, spans and metrics buffered in memory are lost on exit. Flush exporters in the shutdown path.
- **Health checks that cascade.** Liveness or readiness tied to shared dependencies turns a dependency blip into a fleet-wide restart or zero endpoints. Keep liveness shallow and readiness local. See [Fault tolerance patterns]({{ '/backend/fault-tolerance-patterns/' | relative_url }}).

## Interview questions

**Walk through what happens when Kubernetes terminates a pod.**
The pod is marked Terminating. In parallel, the endpoints controller removes it from Services, and the kubelet runs the preStop hook. After preStop, the kubelet sends SIGTERM to each container's PID 1. The app drains and exits. If it's still running when `terminationGracePeriodSeconds` (counted from the start, including preStop) expires, it gets SIGKILL.

**Why do users see 502s during rolling deploys and how do you fix it?**
Endpoint removal propagates asynchronously while the container is signaled immediately, so traffic arrives after the app stopped listening. Add a preStop sleep, drain in-flight requests on SIGTERM, close keep-alive connections, and size the grace period to cover both.

**Liveness vs readiness vs startup probes?**
Liveness restarts a container that is stuck. Readiness removes a pod from load balancing without restarting it. Startup gives slow-booting apps time before liveness and readiness begin. Liveness should be shallow; readiness reflects whether this pod specifically can serve now.

**Should a liveness probe check the database?**
No. A database outage would fail liveness on every pod and restart the whole fleet, which doesn't fix the database and adds cold starts and connection storms. Report dependency health through deep checks, metrics and alerts instead.

**How do you shut down a queue worker that runs 10-minute tasks?**
On SIGTERM, stop consuming and let current tasks finish, with a grace period longer than the longest task. If that isn't practical, make tasks idempotent and checkpointed, use late acks so killed tasks are redelivered, and split long jobs into smaller tasks.

**Why does a container ignore SIGTERM?**
The process is PID 1 and has no handler, so the kernel's default action doesn't apply. Or it runs under a shell that doesn't forward signals. Use exec-form commands, `exec` in entrypoints, an init like `tini`, or install a handler.

**Deep vs shallow health checks: when do you use each?**
Shallow checks (the process responds) for liveness and usually readiness. Deep checks (dependencies reachable) for diagnostics, dashboards and synthetic monitoring, cached and rate-limited, never as a reason to restart pods.

## Further reading

- Kubernetes documentation: "Pod Lifecycle" (termination of Pods), "Container Lifecycle Hooks", and "Configure Liveness, Readiness and Startup Probes".
- Gunicorn documentation: "Signal Handling"; Celery documentation: "Workers Guide" (stopping the worker).
- David Yanacek, "Implementing health checks" (Amazon Builders' Library).
- Python documentation: the `signal` module.
- AWS Elastic Load Balancing documentation: target group health checks and deregistration delay.
