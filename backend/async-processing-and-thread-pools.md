---
title: Asynchronous processing and thread pools
section: backend
order: 57
phase: "Phase 8 · Performance and reliability"
summary: Move slow work off the request path with queues and workers, report results back, and size thread and worker pools for CPU- or IO-bound load.
---

A web request has a latency budget of a few hundred milliseconds and a timeout somewhere between 30 and 60 seconds at the load balancer. Generating a PDF, calling an LLM, sending 5,000 emails or exporting a tenant's data doesn't fit. Asynchronous processing moves that work to a queue and a pool of workers so the request returns quickly, and thread pools are the mechanism that bounds how much work runs at once inside any single process. Both are about the same question: how much concurrency, where, and what happens when demand exceeds it.

## Asynchronous processing

### Why move work off the request path

| Reason | Example |
|---|---|
| Latency | Thumbnails, PDFs, search indexing, LLM calls |
| Reliability | Third-party APIs that time out; retry later instead of failing the user |
| Load smoothing | A spike of 10,000 imports drains at the rate workers allow |
| Isolation | A slow report can't exhaust the web workers that serve logins |
| Scheduling | Nightly rollups, reminders, retention cleanup |

The cost: a broker to run, eventual results instead of immediate ones, at-least-once delivery (so tasks must be idempotent), and a status model the client has to understand. Keep work synchronous when it is fast (tens of milliseconds), when the user needs the answer to continue, or when the write must be confirmed before you return (charging a card, then showing the receipt).

```text
 client        API (Django)               broker         workers
   | POST /reports  |                         |               |
   |--------------->| INSERT job(queued)      |               |
   |                | COMMIT, then enqueue -->|               |
   | 202 + Location |                         |-- deliver --->|
   |<---------------|                         |               | run task,
   | GET /jobs/42   |                         |               | UPDATE job
   |--------------->| SELECT job              |<---- ack -----|
   | status: done   |                         |               |
   |<---------------|                         |               |
```

### Fire-and-forget vs request-reply

| Style | Caller needs | Mechanism | Examples |
|---|---|---|---|
| Fire-and-forget | Nothing back | Enqueue and return `202` or `200` | Audit events, emails, cache warmups |
| Request-reply (async) | A result later | Job id + polling, webhook, or push | Exports, reports, video transcode |
| Request-reply over messaging | A result, service to service | Reply queue + correlation id | RPC over RabbitMQ |
| Synchronous | The result now | Plain HTTP or gRPC call | Auth checks, price quote |

"Fire-and-forget" still needs failure handling: retries with backoff, a dead-letter queue, and alerting on queue age. It means the *caller* doesn't wait, not that nobody cares whether it worked. See [Message queue fundamentals]({{ '/backend/message-queue-fundamentals/' | relative_url }}) and [Delivery semantics]({{ '/backend/delivery-semantics/' | relative_url }}).

### Status polling, webhooks and push

- **Polling.** Return `202 Accepted` with `Location: /jobs/42` and optionally `Retry-After: 2`. The client polls until `done` or `failed`. Simple, works through every proxy, costs some extra reads; cache the status endpoint or back off the poll interval.
- **Webhooks.** For server-to-server integrations, POST the result to a URL the customer registered. Sign the body (HMAC-SHA256 over timestamp and payload), include an event id so receivers can deduplicate, retry with exponential backoff for hours, and offer a replay UI. Webhook delivery is itself an async job.
- **Push to the browser.** Server-Sent Events or WebSockets when a UI waits for progress. Back it with a pub/sub channel so any web node can deliver the event. See [Real-time communication]({{ '/backend/real-time-communication/' | relative_url }}).

The job row is the source of truth for status in all three. Push and webhooks are notifications; a client that missed one can still poll.

### Celery as the typical Python stack

Celery has producers (your Django code), a broker (Redis, RabbitMQ or SQS), workers that execute tasks, and an optional result backend. The settings that matter most for correctness:

| Setting | Why |
|---|---|
| `task_acks_late=True` | Ack after the task finishes, so a crashed worker's task is redelivered (requires idempotent tasks) |
| `task_reject_on_worker_lost=True` | With late acks, requeue tasks whose worker process was killed (e.g. OOM) |
| `worker_prefetch_multiplier=1` | Don't let one worker reserve many long tasks while others sit idle |
| `task_soft_time_limit` / `task_time_limit` | Raise inside the task for cleanup, then hard-kill the child process |
| `visibility_timeout` (Redis, SQS) | Must exceed the longest task, or the broker redelivers it to a second worker mid-run |
| Separate queues | Keep a 10-minute export from blocking 50 ms notification tasks |

When the database itself is a fine queue for your volume, `SELECT ... FOR UPDATE SKIP LOCKED` avoids running a broker at all; see [Postgres as a queue]({{ '/postgresql/postgres-as-a-queue/' | relative_url }}).

## Thread pools

### Why pool threads

Creating a thread per task costs a system call, a stack allocation (on Linux, reserved virtual memory on the order of megabytes per thread), and scheduler pressure. More importantly, unbounded concurrency moves the bottleneck downstream: 2,000 threads each opening a database connection will exhaust Postgres long before your CPU. A pool reuses a fixed set of threads and puts excess work in a queue, which turns "how much concurrency" into one explicit number.

```text
 submit() --> [ queue: q slots ] --> [ worker 1 .. worker n ] --> result
                    |
                 full? --> rejection policy:
                           abort | caller-runs | discard | block
```

### Sizing for CPU-bound vs IO-bound work

- **CPU-bound:** threads = cores (or cores + 1). More threads only add context switching. In CPython the GIL means only one thread runs Python bytecode at a time, so CPU-bound Python needs processes (`ProcessPoolExecutor`, gunicorn or Celery prefork) to use more than one core. Free-threaded CPython builds (3.13 experimental, officially supported in 3.14) remove that limit but aren't the default yet.
- **IO-bound:** threads ≈ `cores × (1 + wait_time / compute_time)`, the formula popularized by *Java Concurrency in Practice*. A request that computes 5 ms and waits 45 ms on the database wants about 10 threads per core.
- **Little's law** gives the same answer from the outside: concurrency = throughput × latency. 200 requests/s at 150 ms each needs 30 in flight. Size for the p99 latency of the dependency, not the median, or you saturate during slow periods.

The ceiling is usually not your CPU but what the threads talk to. Thirty threads in each of 20 pods is 600 database connections. Size the pool against the downstream limit, and use a pooler (see [Connection pooling]({{ '/backend/connection-pooling/' | relative_url }})).

### Queueing

Every pool has a queue in front of it, and its size is a policy decision:

| Queue | Behavior under overload | Risk |
|---|---|---|
| Unbounded | Accepts everything; latency grows without limit | Memory growth, requests time out after waiting, then still execute |
| Bounded | Rejects or blocks when full | Callers must handle rejection |
| Zero-length (direct handoff) | Accept only if a thread is free | Many rejections under bursts |

Python's `ThreadPoolExecutor` uses an unbounded queue, so `submit()` never pushes back. Behind a web server that is how a process slowly fills with work for clients that already gave up. Bound it, as in the demo below.

### Rejection policies

Java's `ThreadPoolExecutor` names the standard options, and they apply in any language:

| Policy | What happens when full | Use when |
|---|---|---|
| Abort | Raise an error (maps to `429` or `503`) | Callers can retry; you prefer fast failure |
| Caller-runs | The submitting thread runs the task itself | Natural backpressure: the producer slows down |
| Discard | Drop the new task silently | Best-effort work (metrics samples, cache warmups) |
| Discard-oldest | Drop the oldest queued task | Only the latest value matters (UI state, position updates) |
| Block | `submit()` waits for a slot | Batch jobs and pipelines, never request handlers |

Rejection is the visible half of [Backpressure]({{ '/backend/backpressure/' | relative_url }}). A rejected request that returns `503` in 1 ms is better for everyone than one that waits 30 s and times out.

### Gunicorn worker models

Gunicorn's master process forks workers; the worker class decides how each one handles concurrency:

| Worker class | Concurrency per process | Fits | Watch out for |
|---|---|---|---|
| `sync` (default) | 1 request | CPU-light apps behind a buffering proxy | A slow upstream call blocks the whole process |
| `gthread` (`--threads N`) | N threads | IO-heavy Django/DRF | GIL limits CPU parallelism; thread-safety of module globals |
| `gevent` / `eventlet` | Hundreds of greenlets | Many slow outbound calls, long polling | Monkey-patching; C drivers that block stall all greenlets |
| `uvicorn` worker (ASGI) | One event loop | Async views, WebSockets, SSE | Any blocking call stalls the loop; see [Non-blocking I/O]({{ '/backend/non-blocking-io/' | relative_url }}) |

Gunicorn's docs suggest `2 × cores + 1` workers as a starting point, then measure. Setting `--threads` above 1 with the default worker class switches it to `gthread`. Celery has the same choice: `prefork` (default, processes, concurrency defaults to the CPU count), `threads`, `gevent`/`eventlet`, and `solo`.

## In practice

### Accept, enqueue after commit, poll (illustrative)

```python
from django.db import transaction
from rest_framework.response import Response

def create_report(request):
    with transaction.atomic():
        job = ReportJob.objects.create(
            tenant=request.tenant, params=request.data, status="queued"
        )
        # Enqueue only after COMMIT, or a fast worker may not see the row yet.
        transaction.on_commit(lambda: generate_report.delay(job.id))
    return Response(
        {"job_id": job.id, "status": "queued"},
        status=202,
        headers={"Location": f"/api/report-jobs/{job.id}/", "Retry-After": "2"},
    )
```

```python
from celery import shared_task

@shared_task(bind=True, autoretry_for=(ProviderTimeout,), retry_backoff=True,
             retry_jitter=True, max_retries=5)
def generate_report(self, job_id):
    job = ReportJob.objects.get(pk=job_id)
    if job.status == "done":              # redelivery is normal with acks_late
        return
    ReportJob.objects.filter(pk=job_id).update(status="running")
    url = build_and_upload(job)           # pass ids into tasks, load fresh state here
    ReportJob.objects.filter(pk=job_id).update(status="done", result_url=url)
```

`on_commit` still loses the task if the process dies between commit and enqueue. When that matters, write the job to an outbox table in the same transaction and let a relay publish it (see [Event-driven architecture]({{ '/backend/event-driven-architecture/' | relative_url }})).

### Gunicorn for a container with 2 CPUs (illustrative)

```python
# gunicorn.conf.py
workers = 3                  # don't use multiprocessing.cpu_count(): it sees the host
worker_class = "gthread"
threads = 8                  # IO-heavy DRF; 3 x 8 = 24 concurrent requests per pod
timeout = 30                 # kill a worker stuck for 30 s
graceful_timeout = 25        # time to finish in-flight requests on SIGTERM
max_requests = 2000          # recycle workers to contain slow leaks
max_requests_jitter = 200    # so workers don't all restart at once
```

With 20 pods that is 480 possible concurrent requests, and potentially 480 database connections. Do the multiplication before you deploy.

### Sizing and rejection, runnable

```python
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor


def io_pool_size(cores, wait_ms, compute_ms):
    """Threads needed to keep `cores` busy when each task mostly waits."""
    return round(cores * (1 + wait_ms / compute_ms))


def littles_law(throughput_per_s, latency_s):
    """Requests in flight = arrival rate x time in system."""
    return throughput_per_s * latency_s


def io_task(_):
    time.sleep(0.05)          # stands in for a DB or HTTP call; the GIL is released


def run_batch(workers, n=40):
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(io_task, range(n)))
    return time.perf_counter() - start


class RejectedError(Exception):
    pass


class BoundedExecutor:
    """ThreadPoolExecutor with a bounded queue and an explicit rejection policy."""

    def __init__(self, workers, queue_size, policy="abort"):
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.slots = threading.BoundedSemaphore(workers + queue_size)
        self.policy = policy

    def submit(self, fn, *args):
        if not self.slots.acquire(blocking=self.policy == "block"):
            if self.policy == "abort":
                raise RejectedError("pool saturated")
            if self.policy == "caller_runs":
                done = Future()
                done.set_result(fn(*args))     # the producer pays, so it slows down
                return done
            return None                        # discard
        try:
            future = self.pool.submit(fn, *args)
        except BaseException:
            self.slots.release()
            raise
        future.add_done_callback(lambda _: self.slots.release())
        return future

    def shutdown(self):
        self.pool.shutdown(wait=True)


def slow(ran_on):
    time.sleep(0.1)
    ran_on.append(threading.current_thread() is threading.main_thread())


if __name__ == "__main__":
    assert io_pool_size(cores=4, wait_ms=45, compute_ms=5) == 40
    assert littles_law(200, 0.150) == 30

    t4, t40 = run_batch(4), run_batch(40)
    print(f"40 IO tasks: 4 threads {t4:.2f}s, 40 threads {t40:.2f}s")
    assert t4 > 4 * t40                       # IO-bound work scales with threads

    ex = BoundedExecutor(workers=2, queue_size=2, policy="abort")
    ran, rejected = [], 0
    for _ in range(10):
        try:
            ex.submit(slow, ran)
        except RejectedError:
            rejected += 1
    ex.shutdown()
    assert rejected == 6 and len(ran) == 4    # 2 running + 2 queued, rest rejected
    print(f"abort: accepted 4, rejected {rejected}")

    ex = BoundedExecutor(workers=2, queue_size=2, policy="caller_runs")
    ran = []
    for _ in range(10):
        ex.submit(slow, ran)
    ex.shutdown()
    assert len(ran) == 10 and any(ran)        # nothing lost; caller absorbed overflow
    print(f"caller_runs: completed {len(ran)}, run by caller {sum(ran)}")
```

## Pitfalls

- **Enqueue before commit.** The worker starts before the transaction commits and gets `DoesNotExist`, or reads stale data. Use `transaction.on_commit` or an outbox.
- **Non-idempotent tasks.** Late acks, visibility timeouts and retries all mean a task can run twice. Guard with status checks, unique constraints or an idempotency key; see [Idempotency]({{ '/backend/idempotency/' | relative_url }}).
- **Visibility timeout shorter than the task.** With Redis or SQS, a 2-hour export and a 1-hour visibility timeout produces a second concurrent run. Raise the timeout or split the task.
- **One queue for everything.** Long exports starve short notification tasks. Route by latency class to separate queues and worker pools.
- **Pickling the world.** Passing model instances or large payloads into tasks serializes stale state. Pass ids and load fresh data in the worker.
- **`threading.Thread` from a request handler.** No retries, no visibility, and the work dies on the next deploy. Use the queue.
- **Pool-induced deadlock.** A task in a pool submits a subtask to the same pool and waits on it; with all threads doing that, nobody runs the subtasks. Use separate pools or don't block inside tasks.
- **Worker count from `cpu_count()` in containers.** It reports host cores, so a 2-CPU pod on a 64-core node starts 129 gunicorn workers and gets OOM-killed. Configure counts explicitly.
- **Swallowed exceptions.** An exception in an executor task is stored in the future and never logged unless someone calls `.result()`. Add a done callback that logs failures.
- **Thread-local context leaks.** Tenant id or request context in a thread-local survives into the next task on that thread. Set and clear it in `try/finally`, or use `contextvars`.

## Interview questions

**When should an endpoint become asynchronous?**
When the work exceeds the request latency budget, depends on unreliable third parties, or arrives in bursts that should be smoothed. Keep it synchronous when the user needs the result to proceed and it completes in tens of milliseconds.

**How does the client get the result of an async job?**
Return `202` with a job id and `Location` header. The client polls the status endpoint, or receives a webhook (server to server) or an SSE/WebSocket event (browser). The job row stays the source of truth so missed notifications can be recovered by polling.

**How do you size a thread pool?**
For CPU-bound work, about one thread per core. For IO-bound work, `cores × (1 + wait/compute)` or, from Little's law, target throughput times dependency latency at p99. Then cap it by the downstream limit, usually database connections, and validate with a load test.

**Why don't more threads speed up CPU-bound Python?**
The GIL lets one thread execute Python bytecode at a time per process. Threads help when they wait on IO, since blocking calls release the GIL. For CPU-bound work, use processes, native extensions that release the GIL, or a free-threaded build.

**What should happen when the pool is full?**
Choose explicitly: reject fast (`429`/`503`), run on the caller to push back on the producer, discard best-effort work, or block in batch pipelines. Unbounded queues hide overload until memory or timeouts expose it.

**Compare gunicorn sync, gthread and gevent workers.**
Sync handles one request per process, which is simple and safe but wasted on IO waits. Gthread adds threads per process for IO concurrency at the cost of shared-state concerns. Gevent multiplexes many greenlets on one thread via monkey-patching; very high concurrency for IO, but any non-cooperative blocking call stalls the whole worker.

**Can a background job run exactly once?**
Delivery is at-least-once in practice: acks can be lost and workers can die after doing the work. You get effectively-once processing by making the task idempotent, with a status check, a unique constraint or a deduplication key recorded in the same transaction as the side effect.

**A worker throws `DoesNotExist` for a record the API just created. Why?**
The task was enqueued inside the transaction and ran before the commit was visible. Enqueue in `transaction.on_commit` or use a transactional outbox.

## Further reading

- Brian Goetz et al., *Java Concurrency in Practice*, chapter 8, "Applying Thread Pools" (sizing and saturation policies).
- Python documentation: `concurrent.futures`.
- Celery documentation: the "Tasks", "Workers" and "Optimizing" user guide pages.
- Gunicorn documentation: "Design" (worker types and how many workers) and the settings reference.
- Gregor Hohpe and Bobby Woolf, *Enterprise Integration Patterns*: Request-Reply, Correlation Identifier, Return Address.
