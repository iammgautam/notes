---
title: asyncio essentials
section: python-django
order: 7
summary: The event loop, tasks, gather and TaskGroup, the blocking-call trap, and how async views, the async ORM and sync_to_async actually behave in Django 5.x.
---

asyncio gives you thousands of concurrent I/O waits in one thread: fan out to five LLM providers, call ten tenant webhooks at once, hold open websocket connections. It also has one way to fail badly: a single blocking call freezes every coroutine on the loop. In Django the extra layer is the sync/async boundary, which decides whether async views help or just add overhead.

## How it works

### Coroutines and the event loop

- `async def` defines a coroutine function. Calling it returns a coroutine object; nothing runs yet.
- `await x` suspends the current coroutine until `x` completes, handing control back to the **event loop**.
- The event loop is a single-threaded scheduler: it runs ready callbacks, then asks the OS (epoll/kqueue) which sockets are ready, then resumes the coroutines waiting on them.

Concurrency comes only from `await` points. Between two awaits, your code runs uninterrupted, which makes reasoning about shared state easier than with threads, and makes blocking calls catastrophic.

### Tasks

`await coro()` runs coroutines sequentially. To run them concurrently, wrap them in **tasks**, which the loop schedules independently.

```python
import asyncio

async def main():
    t1 = asyncio.create_task(fetch_invoice(1))
    t2 = asyncio.create_task(fetch_invoice(2))
    a, b = await t1, await t2     # both were in flight at once
```

### gather vs TaskGroup

```python
# gather: returns results in order
results = await asyncio.gather(*(notify(h) for h in hooks), return_exceptions=True)

# TaskGroup (3.11+): structured concurrency
async with asyncio.TaskGroup() as tg:
    tasks = [tg.create_task(notify(h)) for h in hooks]
results = [t.result() for t in tasks]
```

| | `gather` | `TaskGroup` |
|---|---|---|
| One fails | Others keep running (they are not cancelled) | Others are cancelled |
| Errors | First exception raised, or all returned with `return_exceptions=True` | `ExceptionGroup` with all failures |
| Leaked tasks | Possible | Impossible: block waits for all |

Use `TaskGroup` by default. Use `gather(return_exceptions=True)` when partial success is the goal (notify all webhooks, log the failures).

### Timeouts and limits

```python
async def call_provider(client, payload, sem: asyncio.Semaphore):
    async with sem:                          # cap concurrency
        async with asyncio.timeout(10):      # 3.11+
            r = await client.post("/v1/complete", json=payload)
            return r.json()
```

Without a semaphore, fanning out to 2000 tenants opens 2000 connections and gets you rate-limited. Without timeouts, one hung upstream holds the request forever.

## In practice

### The blocking-call trap

```python
async def handler():
    data = requests.get(url).json()     # blocks the whole loop
    time.sleep(1)                       # blocks the whole loop
    Order.objects.get(pk=1)             # raises SynchronousOnlyOperation in Django
```

Everything else on the loop stops for the duration. Fixes:

- Use async libraries: `httpx.AsyncClient`, `redis.asyncio`, `asyncpg`/psycopg async, `aiofiles`.
- Push unavoidable sync work to a thread: `await asyncio.to_thread(render_pdf, invoice_id)`.
- CPU-heavy work: a process pool via `loop.run_in_executor(process_pool, fn, arg)`.

Detect it with `PYTHONASYNCIODEBUG=1` (or `asyncio.run(main(), debug=True)`), which logs any callback that holds the loop longer than 100 ms.

### Async views in Django

```python
import httpx
from django.http import JsonResponse

async def tenant_dashboard(request):
    tenant = request.tenant
    async with httpx.AsyncClient(timeout=5) as client:
        async with asyncio.TaskGroup() as tg:
            usage = tg.create_task(client.get(f"{USAGE_API}/{tenant.id}"))
            billing = tg.create_task(client.get(f"{BILLING_API}/{tenant.id}"))
    open_orders = await Order.objects.filter(tenant=tenant, status="open").acount()
    return JsonResponse({
        "usage": usage.result().json(),
        "billing": billing.result().json(),
        "open_orders": open_orders,
    })
```

What actually happens:

- **Under ASGI** (uvicorn, daphne, granian), the view runs on the server's event loop. Two HTTP calls take max(latency), not sum.
- **Under WSGI** (gunicorn sync/gthread), Django runs each async view in a fresh event loop per request. It works, but you get no concurrency benefit across requests and pay setup overhead.
- **Middleware**: if any middleware is sync-only, Django adapts by switching to a thread for it, per request. Check the startup debug logs ("Asynchronous handler adapted for middleware ...") and make your middleware async-capable.

### The async ORM

Django 4.1+ has async query methods: `aget`, `afirst`, `acount`, `aexists`, `acreate`, `aupdate`, `adelete`, `async for` over a queryset, and `aiterator`.

```python
invoice = await Invoice.objects.select_related("customer").aget(pk=pk)
async for line in invoice.lines.all():
    ...
```

Underneath, as of Django 5.x, these still run the query through `sync_to_async` on a thread: the database driver calls are synchronous. You get a non-blocking API, not a faster database. Other limits:

- No async transactions: `transaction.atomic` can't wrap async code. Put transactional logic in a sync function and call it with `sync_to_async`.
- Lazy relations don't work in async code: `invoice.customer` without `select_related` raises `SynchronousOnlyOperation`. You must prefetch explicitly, which is a good habit anyway.

### sync_to_async and async_to_sync

```python
from asgiref.sync import sync_to_async, async_to_sync
from django.db import transaction

@sync_to_async
def close_invoice(invoice_id: int) -> None:
    with transaction.atomic():
        inv = Invoice.objects.select_for_update().get(pk=invoice_id)
        inv.status = "closed"
        inv.save(update_fields=["status"])

async def close_view(request, pk):
    await close_invoice(pk)
    ...
```

- `thread_sensitive=True` (the default) runs all such calls in **one shared thread** per request context, matching how Django expects DB connections to be used. Safe, but sync calls are serialized.
- `thread_sensitive=False` runs in a thread pool for parallelism. Don't use it for ORM code.
- `async_to_sync(coro_fn)(...)` calls async code from sync code (a Celery task, a management command). Don't call it from inside a running event loop.

## Pitfalls

- **Fire-and-forget tasks disappearing.** The loop keeps only a weak reference to tasks. `asyncio.create_task(send_audit(...))` without storing the task can be garbage collected mid-flight. Keep a reference (a set, `task.add_done_callback(tasks.discard)`) or use Celery for background work.
- **Unbounded `gather`.** 10k coroutines at once means 10k sockets. Use a `Semaphore` or batches.
- **Swallowed exceptions.** A task that fails and is never awaited logs "Task exception was never retrieved" at garbage collection, often long after the fact.
- **Mixing sync DB access in async views.** Either `SynchronousOnlyOperation` (good, loud) or, with `DJANGO_ALLOW_ASYNC_UNSAFE`, connection sharing bugs (bad, silent). Never set that env var in production.
- **Expecting async to make CPU work faster.** It doesn't. It only overlaps waiting.
- **Cancellation.** A client disconnect or timeout cancels the coroutine at the next `await`. Code after an await may never run; use `try/finally` for cleanup and don't swallow `CancelledError`.

## Checklist

- Async pays off only under an ASGI server, for I/O fan-out or long-lived connections.
- Every network call in async code uses an async client with a timeout.
- Bound concurrency with `Semaphore`; prefer `TaskGroup`.
- Use `select_related`/`prefetch_related` in async views; no lazy relation access.
- Transactions go in sync functions behind `sync_to_async`.
- Keep background jobs in Celery, not detached tasks.

## Interview angle

**How is asyncio concurrent with one thread?** Coroutines yield control at `await` while waiting on I/O; the event loop resumes whichever is ready. No parallel CPU work.

**What happens if you call `requests.get` in an async view?** It blocks the event loop; every other request on that loop stalls. Use `httpx.AsyncClient` or `asyncio.to_thread`.

**Is Django's async ORM truly async?** The API is, but as of 5.x the queries run in a thread via `sync_to_async`, and transactions aren't supported in async context.

**gather or TaskGroup?** TaskGroup for all-or-nothing work with cancellation on failure; gather with `return_exceptions=True` for best-effort fan-out.
