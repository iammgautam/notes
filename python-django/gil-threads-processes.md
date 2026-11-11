---
title: The GIL, threads and processes
section: python-django
order: 6
summary: What the GIL actually serializes, when threads help and when you need processes, how to size Gunicorn and Celery, and what free-threaded Python changes.
---

"Python can't do concurrency" is wrong, and "just add threads" is also wrong. The truth decides real choices: how many Gunicorn workers per container, whether Celery should run prefork or threads, and whether parallelizing a PDF-generation loop will help. The rule is simple once you know what the GIL locks and when it lets go.

## How it works

### What the GIL is

The Global Interpreter Lock is a mutex in CPython (default build) that a thread must hold to execute Python bytecode or touch Python objects. It exists because reference counting and many interpreter internals are not thread-safe. Consequence: **only one thread runs Python code at a time per process**, no matter how many cores you have.

### When the GIL is released

- **Blocking I/O**: socket reads/writes, file I/O, `time.sleep`, `select`. psycopg waiting for PostgreSQL, `requests`/`httpx` waiting for an API, Redis clients waiting on the network all release it.
- **C extensions that opt in**: NumPy on large arrays, `hashlib` and `zlib` on large buffers, Pillow for some operations, lxml parsing, cryptography.
- **Periodically**: a running thread is asked to drop the GIL every switch interval (`sys.getswitchinterval()`, default 5 ms) so others get a turn.

### What that means

| Workload | Threads in one process | Multiple processes |
|---|---|---|
| I/O-bound (DB, HTTP, Redis) | Scale well; waiting releases the GIL | Also fine, more memory |
| CPU-bound pure Python (JSON building, loops, templating) | No speedup, often slower from contention | Scale with cores |
| CPU in C extension that releases the GIL | Can scale | Scale |

A typical Django request is mostly waiting on PostgreSQL and other services, with some CPU for serialization and template rendering. That's why both threads and processes help, and why CPU-heavy serializers are where you hit the ceiling.

### The GIL is not a lock for your code

The GIL makes individual bytecode operations atomic-ish, not your logic. `counter += 1` is a read, add and write; a thread switch in between loses updates. Use `threading.Lock`, a database row lock, or an atomic `F()` update.

### Processes

Each process has its own interpreter, its own GIL and its own memory. You get true parallelism but pay for:

- Memory per process (a Django app is commonly 100–300 MB RSS per worker).
- Inter-process communication by pickling (`multiprocessing`, `ProcessPoolExecutor`).
- Separate DB connections per process, which adds up against `max_connections`.

`fork()` shares memory pages copy-on-write until written. Preloading the app (`gunicorn --preload`) saves memory and startup time but means anything opened at import (DB connections, sockets, thread pools) is shared by children unsafely. Open those lazily, after fork.

## In practice

### Gunicorn worker models

```bash
# sync: 1 request per process, simplest, crash-isolated
gunicorn config.wsgi --workers 5 --timeout 30

# gthread: N threads per process, good for I/O-heavy Django
gunicorn config.wsgi --workers 3 --threads 8 --worker-class gthread

# ASGI with uvicorn workers, for async views / websockets
gunicorn config.asgi -k uvicorn.workers.UvicornWorker --workers 4
```

Sizing:

- The classic `2 * cores + 1` worker rule is a starting point for sync workers on a dedicated host. In containers, base it on the **CPU limit** of the container, not the host core count that `os.cpu_count()` reports.
- With `gthread`, concurrency is `workers * threads`. Each thread may hold a DB connection, so `workers * threads * replicas` must fit your pool or PgBouncer limit.
- Measure: if CPU is saturated, more threads won't help; add processes or replicas. If CPU is low and latency is high, you are I/O-bound and threads or async help.

### Celery pool choices

| Pool | Use for | Notes |
|---|---|---|
| `prefork` (default) | Mixed and CPU work | Process per slot, crash isolation, `--concurrency` = cores |
| `threads` | I/O-bound tasks (HTTP calls, webhooks) | Lightweight, shared memory, GIL-bound for CPU |
| `gevent` / `eventlet` | Thousands of concurrent I/O waits | Monkey-patching, all libraries must be cooperative |
| `solo` | Debugging, one task at a time | Blocks heartbeats during long tasks |

Run separate worker deployments per queue: CPU-heavy `reports` on prefork with concurrency = cores, `webhooks` on threads with concurrency 50.

### Concurrency inside one task

```python
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor

# I/O-bound: fetch 200 exchange rates concurrently
with ThreadPoolExecutor(max_workers=20) as pool:
    rates = list(pool.map(fetch_rate, currencies))

# CPU-bound: render 500 PDFs across cores
with ProcessPoolExecutor(max_workers=4) as pool:
    pdfs = list(pool.map(render_invoice_pdf, invoice_ids, chunksize=10))
```

Pass IDs, not model instances, to process pools: arguments are pickled, and each child must open its own DB connection (call `django.db.connections.close_all()` before forking, or let the children set Django up).

> **Gotcha:** Django DB connections are per thread. Each thread in a `ThreadPoolExecutor` that touches the ORM opens its own connection and never closes it unless you call `connection.close()` at the end of the work. Twenty threads per worker can quietly exhaust `max_connections`.

### Free-threaded Python (3.13+)

PEP 703 added an optional build without the GIL. In 3.13 it was experimental (`python3.13t`); 3.14 made it officially supported but still not the default.

- Threads can run Python bytecode in parallel on multiple cores.
- Reference counting uses biased and atomic operations, so single-threaded code is slower (roughly 5–10% in 3.14, more in 3.13).
- C extensions must declare support; importing one that doesn't re-enables the GIL at runtime (with a warning), unless you force it off with `PYTHON_GIL=0`.
- Your own code's races become real races. Code that "worked" because of the GIL's coarse switching may now corrupt shared state.

For a Django app this is not yet a reason to change deployment. Processes and I/O-bound threads already cover most workloads. Watch it for CPU-heavy pipelines (embeddings preprocessing, report generation) once your dependencies ship free-threaded wheels.

## Pitfalls

- **Adding threads to CPU-bound code.** Throughput stays flat or drops. Symptom: one core at 100%, others idle. Use processes.
- **Container CPU limits.** A pod limited to 1 CPU but running 9 Gunicorn workers gets throttled; p99 latency climbs. Check `container_cpu_cfs_throttled_seconds_total`.
- **Connection explosion.** `replicas * workers * threads` plus Celery `concurrency` often exceeds PostgreSQL's 100 default `max_connections`. Use PgBouncer and set `CONN_MAX_AGE` deliberately.
- **Work at import in preloaded apps.** Opened sockets and DB connections shared across forked children produce corrupt protocol state and "SSL error: decryption failed" style errors.
- **Long CPU work blocking heartbeats.** A pure-Python loop in a `threads` or `gevent` pool starves other tasks and broker heartbeats.

## Checklist

- I/O-bound: threads or async. CPU-bound: processes.
- Size Gunicorn from container CPU limits; verify with load tests.
- Budget DB connections across every process and thread.
- Separate Celery queues and pools for CPU and I/O work.
- Pass IDs across process boundaries; close DB connections in worker threads.

## Interview angle

**Does the GIL make Python code thread-safe?** No. It protects interpreter internals. Compound operations like `x += 1` or check-then-set can still interleave.

**Why do threads help a Django app if there's a GIL?** Most request time is spent waiting on the database and network, and the GIL is released during that wait.

**When would you choose processes over threads?** CPU-bound pure-Python work, or when you need crash isolation and memory limits per worker.

**What does free-threading change?** True parallel bytecode execution in threads, at some single-thread cost and with C-extension compatibility caveats. Thread-safety bugs that the GIL masked become visible.
