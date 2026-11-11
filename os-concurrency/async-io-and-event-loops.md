---
title: Async I/O and event loops
section: os-concurrency
order: 6
summary: Blocking vs non-blocking I/O, how epoll event loops work, why async helps I/O-bound services, and how to avoid blocking the loop in asyncio and Django.
---

A typical API request spends most of its time waiting: on Postgres, on Redis, on a payment API, on an LLM provider that takes 20 seconds to stream an answer. A thread that waits still costs a thread. An event loop lets one thread juggle thousands of waiting operations. That is the whole pitch of Node.js and asyncio, and also the source of their main failure mode: one blocking call freezes everything.

## How it works

### Blocking vs non-blocking I/O

- **Blocking**: `sock.recv()` puts the thread to sleep until data arrives. Simple code, one thread per concurrent operation.
- **Non-blocking**: the socket is set `O_NONBLOCK`; `recv()` returns immediately with data or `EAGAIN` ("nothing yet"). Now you need a way to know **when** to try again without spinning.

### I/O multiplexing: select, poll, epoll

The kernel can watch many file descriptors and tell you which are ready.

| API | Cost per wait | Notes |
|---|---|---|
| `select` | `O(n)` fds, limit 1024 | portable, old |
| `poll` | `O(n)` fds | no hard limit |
| `epoll` (Linux) | `O(ready)` | register once, get ready list |
| `kqueue` (BSD/macOS) | `O(ready)` | same idea |
| `io_uring` (Linux) | submission/completion rings | true async I/O incl. files |

With `epoll` you register sockets once (`epoll_ctl`) and then `epoll_wait` returns only the ready ones, so 10,000 idle connections cost almost nothing per loop iteration.

### The event loop

```text
loop:
  1. run all callbacks / coroutine steps that are ready
  2. compute timeout = time until next scheduled timer
  3. epoll_wait(timeout)     <- the only place the thread sleeps
  4. for each ready fd: mark the waiting coroutine as ready
  5. fire expired timers
```

A coroutine runs until it hits `await` on something not ready, which registers interest with the loop and yields control. The loop runs other ready work, and resumes the coroutine when its socket is readable. This is **cooperative multitasking**: nothing preempts a coroutine. If it doesn't `await`, nobody else runs.

### Why it helps I/O-bound work

Threads cost memory (stack, kernel structures) and context switches; practical limits are hundreds to low thousands per process. Coroutines cost a few KB each and switch in user space. For workloads like "hold 5,000 SSE streams open" or "fan out 200 HTTP calls and gather results", async wins clearly.

It does **not** make CPU-bound work faster. A coroutine computing embeddings in pure Python blocks the loop for its whole duration. And for a typical CRUD request with 3 fast DB queries, sync Django with a sensible worker count is just as fast.

### Node.js vs asyncio

Same model. Node runs JS on one thread with libuv's event loop (epoll/kqueue) and pushes file I/O, DNS and some crypto onto a small thread pool (default 4 threads, `UV_THREADPOOL_SIZE`). Everything in the Node ecosystem is async by default. In Python, asyncio is opt-in, so the risk is calling a sync library (requests, psycopg2, boto3, the Django ORM) from async code.

## In practice

### Concurrent fan-out with asyncio

```python
import asyncio
import httpx

async def fetch_all(urls, limit=20):
    sem = asyncio.Semaphore(limit)
    async with httpx.AsyncClient(timeout=10) as client:
        async def one(url):
            async with sem:
                r = await client.get(url)
                r.raise_for_status()
                return r.json()
        return await asyncio.gather(*(one(u) for u in urls), return_exceptions=True)

results = asyncio.run(fetch_all(urls))
```

Python 3.11+ `asyncio.TaskGroup` gives structured concurrency: if one task fails, the rest are cancelled.

```python
async with asyncio.TaskGroup() as tg:
    user_t = tg.create_task(get_user(uid))
    plan_t = tg.create_task(get_plan(tid))
user, plan = user_t.result(), plan_t.result()
```

Always bound concurrency (semaphore) and always set timeouts (`asyncio.timeout(5)` in 3.11+), or one slow dependency piles up unbounded tasks.

### Calling blocking code from async

```python
import asyncio

# blocking library call -> run in a thread
pdf_bytes = await asyncio.to_thread(render_pdf, invoice)

# CPU-heavy work -> run in a process pool
loop = asyncio.get_running_loop()
vec = await loop.run_in_executor(process_pool, compute_heavy, data)
```

### Async Django

Django supports async views (since 3.1) and an async ORM interface (`aget`, `acreate`, `async for`), but under the hood ORM queries still run in a worker thread via `sync_to_async`. Rules:

- Serve with ASGI (uvicorn, daphne, or gunicorn with `uvicorn.workers.UvicornWorker`) to get any benefit.
- Use async views where waiting dominates: streaming LLM responses, SSE, fan-out to external APIs, WebSockets via Channels.
- Keep ordinary CRUD sync. Mixing sync middleware into an async stack forces thread hops on every request.
- Wrap sync-only code with `sync_to_async`; never call the ORM synchronously inside an async view (Django raises `SynchronousOnlyOperation`).

```python
from asgiref.sync import sync_to_async

async def project_summary(request, pk):
    project = await Project.objects.aget(pk=pk, tenant=request.tenant)
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(LLM_URL, json={"prompt": build_prompt(project)})
    await sync_to_async(log_usage)(request.tenant, r.json()["usage"])
    return JsonResponse({"summary": r.json()["text"]})
```

### Detect a blocked loop

```bash
PYTHONASYNCIODEBUG=1 python app.py
# logs: "Executing <Task ...> took 0.412 seconds"
```

```python
loop.slow_callback_duration = 0.05   # warn on anything >50 ms between awaits
```

In production, symptoms are latency spikes across **all** requests on a worker at the same time, and health checks timing out while CPU is pegged on one core. `py-spy dump --pid <pid>` shows what the loop thread is stuck on.

### Celery and async

Celery tasks are sync. To call async code from a task, `asyncio.run(main())` per task is fine for occasional use. For high-volume I/O-bound tasks (webhooks, LLM calls), a gevent or threads pool is often simpler than async inside Celery.

## Pitfalls

- **Blocking calls in coroutines**: `requests.get`, `time.sleep`, `psycopg2`, sync Redis clients, big `json.loads`, file reads. One 2-second call stalls every connection on that loop for 2 seconds. Use async clients (`httpx`, `asyncpg`/psycopg 3 async, `redis.asyncio`) or `to_thread`.
- **Forgetting `await`**: the coroutine never runs; you get a "coroutine was never awaited" warning and a silent bug.
- **Fire-and-forget tasks garbage-collected**: `asyncio.create_task()` without keeping a reference can be collected mid-flight. Keep references or use a `TaskGroup`.
- **Unbounded gather**: `gather` over 50,000 URLs opens 50,000 sockets. Use a semaphore.
- **Swallowed cancellation**: catching `Exception` is fine, but catching `BaseException` or `CancelledError` breaks timeouts and shutdown.
- **Async for the sake of it**: rewriting a CRUD API to async adds complexity with no throughput gain if the DB is the bottleneck.
- **Too many DB connections**: async makes it easy to run 1,000 concurrent queries. Postgres can't handle 1,000 connections; bound with a pool.

## Rules of thumb

- Async for many concurrent waits (streams, fan-out, WebSockets); sync for ordinary request/response with a DB.
- Nothing blocking on the loop; offload with `to_thread` or a process pool.
- Every external call has a timeout; every fan-out has a concurrency limit.
- Use structured concurrency (`TaskGroup`) so failures cancel siblings.
- Measure loop lag; alert when it exceeds tens of milliseconds.

## Interview angle

- **"How does an event loop handle thousands of connections on one thread?"** Non-blocking sockets registered with epoll; the loop sleeps in `epoll_wait`, wakes with the list of ready sockets, and resumes the coroutines waiting on them. Coroutines yield at each `await`.
- **"When does async not help?"** CPU-bound work, and workloads bottlenecked on a resource with fixed capacity, like a database with a small connection pool.
- **"What happens if you call `time.sleep(5)` in an async handler?"** The whole event loop blocks for 5 seconds; every other request on that worker stalls. Use `await asyncio.sleep(5)`.
- **"Threads vs async in Python?"** Both handle I/O-bound concurrency because the GIL is released on I/O. Threads work with sync libraries and are preemptive; async scales to far more concurrent operations but needs async libraries end to end.
