---
title: Async and performance in FastAPI
section: ai-stack
order: 4
phase: "Phase 1 · FastAPI"
summary: Choose async def or def per endpoint, catch event loop blocking, fan out LLM calls with limits and timeouts, and size workers for production.
---

After this note you can choose `async def` or `def` per endpoint, prove with numbers when something blocks the event loop, run dozens of LLM calls concurrently within rate limits, choose between `BackgroundTasks` and a queue, and set workers and timeouts. LLM calls are network I/O that takes seconds, so for an AI service the concurrency model matters far more than raw Python speed. This is the "latency, cost and reliability" line in the job description.

## Prerequisites

[Dependency injection in FastAPI]({{ '/ai-stack/fastapi-dependency-injection/' | relative_url }}), plus basic asyncio ([Asyncio essentials]({{ '/python-django/asyncio-essentials/' | relative_url }})).

## The execution model

A uvicorn worker is one process. Inside it, one thread runs the event loop, and a separate thread pool (AnyIO's, capped at 40 threads by default) handles sync code.

- **`async def` endpoints and dependencies run on the event loop thread.** While one awaits I/O, the loop serves other requests. If one does anything blocking (`time.sleep`, `requests.get`, a sync DB driver, a sync LLM SDK, a long CPU loop), every request in that worker waits.
- **`def` endpoints and dependencies run in the thread pool** through `anyio.to_thread.run_sync`. They are allowed to block, but only 40 run at once per worker, and each one pays for a thread hop.

```text
uvicorn worker process
+-------------------------------------------------------------+
| event loop thread                                           |
|   async def A  -- await httpx ...  -> loop serves others    |
|   async def B  -- time.sleep(2)    -> EVERYTHING WAITS      |
|                                                             |
| thread pool (AnyIO limiter, 40 by default)                  |
|   def C              -- requests.get -> blocks one thread   |
|   run_in_threadpool(parse_pdf)       -> blocks one thread   |
+-------------------------------------------------------------+
```

| The endpoint calls | Declare it as |
|---|---|
| Only async libraries (httpx `AsyncClient`, asyncpg, async SQLAlchemy, `AsyncOpenAI`) | `async def` |
| Sync I/O libraries (`requests`, a sync ORM, boto3, sync SDKs) | `def` |
| A mix of both | `async def`, offloading the sync part |
| CPU-heavy work (PDF parsing, large pandas jobs) | A process pool or a job queue |
| Nothing slow at all | `async def`, which avoids the thread hop |

## Measured: blocking vs non-blocking

The demo sends five requests at once to four versions of the same 200 ms operation. `TestClient` is synchronous and sends one request at a time, so for concurrency you need httpx's `AsyncClient` with `ASGITransport`. That still runs in-process with no server, but every request shares one event loop, just as they would inside a real worker.

```python
import asyncio
import time

import httpx
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

app = FastAPI()


def slow_vendor_lookup() -> str:
    time.sleep(0.2)                  # a sync SDK call, a file parse, etc.
    return "ok"


@app.get("/blocking")
async def blocking() -> str:
    return slow_vendor_lookup()      # BAD: freezes the event loop for 200 ms


@app.get("/sync-def")
def sync_def() -> str:
    return slow_vendor_lookup()      # FastAPI runs def endpoints in a thread


@app.get("/offloaded")
async def offloaded() -> str:
    return await run_in_threadpool(slow_vendor_lookup)


@app.get("/non-blocking")
async def non_blocking() -> str:
    await asyncio.sleep(0.2)         # stands in for an awaited DB or LLM call
    return "ok"


async def measure(path: str, n: int = 5) -> float:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as client:
        start = time.perf_counter()
        responses = await asyncio.gather(*(client.get(path) for _ in range(n)))
        assert all(r.status_code == 200 for r in responses)
        return time.perf_counter() - start


async def main() -> dict[str, float]:
    return {p: await measure(p) for p in
            ["/blocking", "/sync-def", "/offloaded", "/non-blocking"]}


if __name__ == "__main__":
    timings = asyncio.run(main())
    for path, seconds in timings.items():
        print(f"{path:14} 5 concurrent requests: {seconds:.2f}s")
    assert timings["/blocking"] >= 0.95          # 5 x 200 ms, one at a time
    assert all(timings[p] < 0.6 for p in
               ["/sync-def", "/offloaded", "/non-blocking"])
    print("ok")
```

One run printed 1.02 s for `/blocking` and 0.20 to 0.22 s for the other three. The blocking version runs the five requests one after another. In production that shows up as every endpoint in the worker, health checks included, slowing down together while CPU stays low.

## Detecting a blocked loop

Symptoms: p99 rises across unrelated endpoints at once, throughput flattens while CPU idles, liveness probes fail under load, and gunicorn logs `WORKER TIMEOUT` for async workers (their heartbeat runs on the loop). Three tools find the cause:

- **asyncio debug mode** (`PYTHONASYNCIODEBUG=1` or `asyncio.run(..., debug=True)`) logs every step that holds the loop longer than `slow_callback_duration` (100 ms by default). It adds overhead, so use it in development and load tests.
- **A loop-lag heartbeat** costs almost nothing and belongs in production. Start it in the lifespan and export the lag as a metric.
- **`py-spy dump --pid <worker pid>`** prints the stack of the stuck thread while the stall is happening.

```python
import asyncio
import logging
import time

warnings: list[str] = []


class Collect(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        warnings.append(record.getMessage())


logging.getLogger("asyncio").addHandler(Collect())


async def lag_monitor(samples: list[float], interval: float = 0.05) -> None:
    """Heartbeat: how late does a 50 ms sleep wake up? Export as a metric."""
    while True:
        start = time.perf_counter()
        await asyncio.sleep(interval)
        samples.append(time.perf_counter() - start - interval)


async def reconcile_batch() -> None:
    time.sleep(0.3)                   # the hidden blocking call


async def main() -> float:
    asyncio.get_running_loop().slow_callback_duration = 0.1  # default 0.1 s
    samples: list[float] = []
    monitor = asyncio.create_task(lag_monitor(samples))
    await asyncio.sleep(0.1)
    await reconcile_batch()
    await asyncio.sleep(0.1)
    monitor.cancel()
    return max(samples)


if __name__ == "__main__":
    worst = asyncio.run(main(), debug=True)   # or PYTHONASYNCIODEBUG=1
    slow = [float(w.rsplit("took ", 1)[1].split()[0]) for w in warnings
            if "took" in w]
    print(f"worst loop lag: {worst * 1000:.0f} ms; debug mode flagged: {slow}")
    assert worst > 0.25 and slow and slow[0] >= 0.25
    print("ok")
```

## Offloading blocking work

- **`await run_in_threadpool(fn, *args, **kwargs)`** (from `fastapi.concurrency`) is a thin wrapper over `anyio.to_thread.run_sync`, and it shares the 40-thread limiter with `def` endpoints. `asyncio.to_thread` also works, but it uses a second, separate pool. Stick to one of them.
- **Pool size.** If many sync DB calls queue for threads, raise the limit in the lifespan with `anyio.to_thread.current_default_thread_limiter().total_tokens = 80`. Check the database connection pool first, because it is usually the real ceiling.
- **CPU-bound work** gains nothing from threads because of the GIL, unless the library releases it (NumPy and PyArrow often do). Create a `ProcessPoolExecutor` in the lifespan and call `await loop.run_in_executor(pool, fn, arg)`, or move the work to a queue. See [GIL, threads and processes]({{ '/python-django/gil-threads-processes/' | relative_url }}).

## Lifespan and shared clients

The lifespan is an async context manager that runs once per worker process. Before `yield` is startup (open HTTP and DB pools, create semaphores, load an embedding model); after `yield` is shutdown (close clients, flush telemetry). A yielded dict shows up on `request.state`. `@app.on_event("startup")` is deprecated. Three behaviors to know:

- **One copy per worker.** Four workers means four copies of everything you load, which matters for in-memory models.
- **Startup errors stop the worker from booting.** That gives you fail-fast on bad config.
- **`TestClient` only runs the lifespan inside `with TestClient(app) as client:`.**

Create **one** `httpx.AsyncClient`, or one SDK client such as `AsyncOpenAI` or `AsyncAnthropic`, per process and reuse it. Kept-alive connections skip a TCP and TLS handshake (tens to hundreds of milliseconds) on every LLM call; a client per request pays it every time and leaks sockets if nobody closes it. Set timeouts explicitly. httpx defaults to 5 s for everything, which is too short for a long generation and too long for a connect, and provider SDKs default to timeouts measured in minutes.

## Concurrent LLM calls with gather and a semaphore

A common finance task: summarize 12 policy documents for one request. Run them one after another and the latency adds up. Run them all at once and you get 429s from the provider plus an exhausted connection pool. The fix is to run them concurrently with a cap, and give the whole fan-out a deadline:

```python
import asyncio
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

MAX_PARALLEL_LLM_CALLS = 4             # per worker process; size to your quota


class SummarizeRequest(BaseModel):
    document_ids: list[str] = Field(min_length=1, max_length=50)


def create_app(llm_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup: one pooled client and one limiter for the whole process.
        llm = httpx.AsyncClient(
            base_url="https://llm.internal",
            transport=llm_transport,            # None in production
            timeout=httpx.Timeout(15.0, connect=2.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        yield {"llm": llm, "llm_slots": asyncio.Semaphore(MAX_PARALLEL_LLM_CALLS)}
        await llm.aclose()                      # shutdown

    app = FastAPI(lifespan=lifespan)

    @app.post("/summaries")
    async def summarize(body: SummarizeRequest, request: Request) -> dict:
        llm: httpx.AsyncClient = request.state.llm
        slots: asyncio.Semaphore = request.state.llm_slots

        async def summarize_one(doc_id: str) -> str:
            async with slots:                   # at most N calls in flight
                r = await llm.post("/v1/complete",
                                   json={"prompt": f"Summarize policy {doc_id}"})
                r.raise_for_status()
                return r.json()["text"]

        try:
            async with asyncio.timeout(20):     # budget for the whole fan-out
                results = await asyncio.gather(
                    *(summarize_one(d) for d in body.document_ids),
                    return_exceptions=True,     # one failure != total failure
                )
        except TimeoutError:
            raise HTTPException(504, "summaries timed out")
        pairs = list(zip(body.document_ids, results))
        return {
            "summaries": {d: r for d, r in pairs if not isinstance(r, Exception)},
            "failed": [d for d, r in pairs if isinstance(r, Exception)],
        }

    return app


if __name__ == "__main__":
    stats = {"in_flight": 0, "peak": 0}

    async def fake_llm(request: httpx.Request) -> httpx.Response:
        stats["in_flight"] += 1
        stats["peak"] = max(stats["peak"], stats["in_flight"])
        await asyncio.sleep(0.1)                # 100 ms "model latency"
        stats["in_flight"] -= 1
        if b"doc-7" in request.content:
            return httpx.Response(503, json={"error": "overloaded"})
        return httpx.Response(200, json={"text": "summary"})

    app = create_app(httpx.MockTransport(fake_llm))
    with TestClient(app) as client:             # `with` runs the lifespan
        start = time.perf_counter()
        r = client.post("/summaries",
                        json={"document_ids": [f"doc-{i}" for i in range(12)]})
        elapsed = time.perf_counter() - start

    body = r.json()
    print(f"12 calls, peak {stats['peak']} in flight, {elapsed:.2f}s")
    assert r.status_code == 200 and body["failed"] == ["doc-7"]
    assert len(body["summaries"]) == 11
    assert stats["peak"] == MAX_PARALLEL_LLM_CALLS
    assert 0.29 < elapsed < 0.8                # 3 waves of 4, not 12 x 100 ms
    print("ok")
```

The run took about 0.31 s: three waves of four calls, instead of 1.2 s sequentially. Things to defend:

- **The semaphore lives in the lifespan**, so it caps calls across all requests in the worker, not just within one request. The real ceiling is workers × pods × 4, so when a provider quota has to be shared across the whole fleet, use a shared limiter such as a Redis token bucket or an LLM gateway.
- **`return_exceptions=True`** returns partial results. Without it, `gather` raises the first error and *leaves the other calls running*. Use `asyncio.TaskGroup` when one failure should cancel the rest.
- **Two layers of timeouts.** httpx bounds each call and `asyncio.timeout` bounds the whole request. Add retries with jittered backoff on 429 and 5xx (most SDKs have `max_retries`), as covered in [Retries and backoff]({{ '/backend/retries-and-backoff/' | relative_url }}).
- **`create_app` injects the transport**, so tests run the real code path against `httpx.MockTransport` with no network or API key.
- **Per-endpoint shortcut:** `Depends(dep, max_concurrency=n)` from the dependency injection note.

## BackgroundTasks vs Celery and queues

```python
from fastapi import BackgroundTasks, FastAPI, status
from fastapi.testclient import TestClient

AUDIT_LOG: list[dict] = []
EMBED_QUEUE: list[str] = []


def write_audit(event: str, invoice_id: int) -> None:   # sync: runs in a thread
    AUDIT_LOG.append({"event": event, "invoice_id": invoice_id})


async def enqueue_reindex(invoice_id: int) -> None:      # async: runs on the loop
    EMBED_QUEUE.append(f"invoice:{invoice_id}")


app = FastAPI()


@app.post("/invoices/{invoice_id}/approve", status_code=status.HTTP_202_ACCEPTED)
async def approve(invoice_id: int, tasks: BackgroundTasks) -> dict:
    tasks.add_task(write_audit, "approved", invoice_id)
    tasks.add_task(enqueue_reindex, invoice_id)
    return {"invoice_id": invoice_id, "status": "approved"}


if __name__ == "__main__":
    r = TestClient(app).post("/invoices/42/approve")
    assert r.status_code == 202
    # TestClient waits for background tasks; a real client would not.
    assert AUDIT_LOG == [{"event": "approved", "invoice_id": 42}]
    assert EMBED_QUEUE == ["invoice:42"]
    print("ok")
```

| | `BackgroundTasks` | Celery, arq, a Postgres queue |
|---|---|---|
| Runs in | The same worker, after the response | Separate worker processes |
| Survives a crash or deploy | No | Yes, the broker keeps it |
| Retries, schedules, rate limits | No | Yes |
| Fits | Audit writes, cache busting, metrics, enqueueing | Document ingestion and embedding, month-end reconciliation, report generation, batch LLM jobs |

If losing the task would matter, or it takes more than a few seconds, use a queue. For long LLM work, return 202 with a job ID and let the client poll. A heavy `async` background task still blocks the loop.

## Workers and timeouts in production

```bash
# development: one process, auto-reload
fastapi dev app/main.py

# production: uvicorn's own process manager
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4 \
  --timeout-keep-alive 75 --timeout-graceful-shutdown 30

# production: gunicorn as the manager (pip install uvicorn-worker)
gunicorn app.main:app -k uvicorn_worker.UvicornWorker -w 4 \
  --bind 0.0.0.0:8000 --timeout 60 --graceful-timeout 30 --keep-alive 75
```

- **Worker count.** The sync-worker 2n+1 rule doesn't apply. Start with one worker per core; on Kubernetes, run one or two per pod and scale pods. Each worker has its own lifespan resources, semaphores and pools, so DB connections add up to workers × pool size × pods.
- **Keep-alive.** Set the server keep-alive (uvicorn defaults to 5 s) longer than the load balancer's idle timeout (60 s by default on an AWS ALB). Otherwise the server closes connections the LB is about to reuse, and clients get sporadic 502s.
- **Request deadlines.** Uvicorn has no per-request timeout. Set budgets inside the app: the per-call httpx timeout is less than the request budget (`asyncio.timeout`), which is less than the LB or ingress timeout. If the LB gives up first, the client sees a 504 while your worker keeps paying for tokens.
- **Shutdown.** `--timeout-graceful-shutdown` has to cover your longest in-flight request (streams included), and the pod's termination grace period has to be longer still.
- **`uvicorn.workers` is deprecated.** Use the `uvicorn-worker` package, as above.

## Coming from Django

| Django | FastAPI |
|---|---|
| WSGI, a thread or process per request | ASGI, one event loop per worker |
| Gunicorn sync workers, 2n+1 | About one async worker per core |
| `sync_to_async` / `async_to_sync` (asgiref) | `run_in_threadpool` / `anyio.from_thread` |
| `SynchronousOnlyOperation` guards the ORM | Nothing warns you; choose async drivers |
| Celery for background work | Still Celery or a queue; `BackgroundTasks` for small post-response work |
| Gunicorn `--timeout` kills slow requests | `asyncio.timeout` inside the app |

## Common mistakes

- **`requests`, a sync SDK or a sync ORM inside `async def`.** Switch to the async client, declare the endpoint `def`, or offload the call.
- **A new `AsyncClient` per request.** Create one in the lifespan and close it on shutdown.
- **Unbounded `gather` over user-supplied lists.** Cap the input size (`max_length=50`) and the concurrency (a semaphore).
- **Expecting `gather` to cancel siblings on error.** It doesn't. Use `TaskGroup` for all-or-nothing work.
- **Creating clients or asyncio primitives at import time.** They end up bound to the wrong loop in tests. Create them in the lifespan.
- **Critical work in `BackgroundTasks`.** A deploy or crash drops it silently. Use a queue.

## Practice tasks

1. Extend the measured demo with a `/health` route. Fire 20 `/blocking` requests and time `/health` in the middle of them. Done: a table showing health latency with and without the blocking endpoint.
2. Start `lag_monitor` from the lifespan and log a warning when lag exceeds 100 ms. Done: a test hits `/blocking` and asserts that the warning was logged.
3. Swap the fake LLM for `AsyncOpenAI` or `AsyncAnthropic` created in the lifespan, keep the semaphore, and add jittered retries on 429. Done: a 50-document request completes with zero 429s in the logs.
4. Move a month-end reconciliation (pandas over 1M rows) out of the request: `POST` returns 202 and a job ID, and `GET /jobs/{id}` reports status. Done: the POST returns in under 100 ms and the job survives an app restart.
5. Load-test at 200 requests per second with Locust or k6. Compare 1 vs 4 workers, and `def` vs `async def` on a sync DB endpoint. Done: p50, p99 and CPU numbers, with one paragraph explaining them.

## Interview questions

**`async def` or `def` in FastAPI?** `async def` runs on the event loop and must only await non-blocking I/O. `def` runs in a 40-thread pool and may block. Pick based on the libraries you call, not on which one looks faster.

**What happens if you call `requests.get` inside `async def`?** The event loop stops for the whole call, so every other request in that worker waits, while CPU stays idle.

**How do you find a blocking call?** Watch for correlated p99 spikes and loop-lag metrics, run asyncio debug mode under load to log slow steps, and use `py-spy dump` on a stuck worker to see the stack.

**How do you make 50 LLM calls for one request safely?** Use one shared async client, `gather` under a semaphore sized to the quota, per-call and overall timeouts, retries with jittered backoff, and partial results via `return_exceptions`. For a cap across the whole fleet, use a shared rate limiter.

**`gather` or `TaskGroup`?** `gather` collects results and, with `return_exceptions=True`, tolerates partial failure, but it doesn't cancel siblings. `TaskGroup` cancels everything on the first failure, which suits all-or-nothing work.

**BackgroundTasks or Celery?** `BackgroundTasks` runs in-process after the response, with no persistence or retries, so it fits small side effects. Durable, slow or retryable work such as ingestion, reconciliation and batch LLM jobs belongs on a queue.

**How many workers, and why?** Roughly one per core for async I/O-bound services, adjusted for memory per worker, since each loads its own models and pools. On Kubernetes, prefer fewer workers per pod and more pods.

**Why create HTTP clients in the lifespan?** Pooled, kept-alive connections avoid a handshake per call, shutdown closes them cleanly, and tests can inject a mock transport.

## Further reading

- FastAPI docs: Concurrency and async / await, Lifespan Events, Background Tasks, and Deployment (Server Workers).
- Python docs: "Developing with asyncio" (debug mode), `asyncio.TaskGroup`, `asyncio.timeout`.
- HTTPX docs: Async Support, Timeouts, Resource Limits, Transports.
- AnyIO docs: Working with threads.
- Uvicorn docs: Settings and Deployment; the uvicorn-worker README.
- Related notes: [Latency and cost engineering]({{ '/llm/latency-and-cost/' | relative_url }}), [Non-blocking I/O]({{ '/backend/non-blocking-io/' | relative_url }}), [Celery in production]({{ '/python-django/celery-in-production/' | relative_url }}).
