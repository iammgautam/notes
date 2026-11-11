---
title: Processes vs threads
section: os-concurrency
order: 1
summary: How processes and threads differ in memory, isolation, context-switch cost and fork behavior, and how that shapes gunicorn and Celery worker settings.
---

Every Django deployment makes a processes-vs-threads decision, usually implicitly: gunicorn `--workers` and `--threads`, Celery `--pool` and `--concurrency`. The right numbers depend on how processes and threads share memory, how the GIL behaves, and what `fork()` does to open connections. Getting it wrong shows up as OOM kills, idle CPUs, or mysterious "SSL error: decryption failed" in workers.

## How it works

### Process

A process is a running program with its **own virtual address space**, file descriptor table, signal handlers, and at least one thread. Processes are isolated: one crashing or leaking memory doesn't corrupt another. They communicate via pipes, sockets, shared memory, or an external system (Redis, Postgres).

### Thread

A thread is an execution context (registers, stack, program counter) **inside** a process. Threads share the heap, globals, open files and sockets. Sharing makes communication cheap and makes data races possible. On Linux, both are created with `clone()`; a thread is a task that shares the memory map.

| | Process | Thread |
|---|---|---|
| Memory | separate address space | shared heap, own stack (~8 MB virtual default) |
| Creation cost | higher (`fork`, page tables) | lower |
| Context switch | changes address space, TLB effects | same address space, cheaper |
| Crash isolation | yes | one segfault kills all threads |
| Communication | IPC, sockets, external store | shared variables + locks |
| Python GIL | one GIL per process | one GIL shared by all threads |

### Context switches

The kernel scheduler switches tasks when a time slice ends or a task blocks on I/O. A switch costs on the order of 1–5 µs directly, plus indirect cost from cold CPU caches. Thousands per second per core are normal; hundreds of thousands means too many runnable threads fighting for cores.

```bash
vmstat 1            # "cs" column: context switches per second
pidstat -w -p <pid> 1   # voluntary (blocked on I/O) vs involuntary (preempted)
```

High **involuntary** switches mean CPU contention. High **voluntary** switches mean lots of waiting on I/O or locks.

### fork() and copy-on-write

`fork()` creates a child with a copy of the parent's address space. Pages are shared and marked copy-on-write: only pages that get written are actually copied. So a gunicorn master that loads Django (say 150 MB) and forks 8 workers doesn't use 1.2 GB on day one.

Two catches:

- **CPython reference counting writes to objects**, and the cyclic GC touches object headers, so shared pages slowly get copied. `gc.freeze()` (Python 3.7+) right before forking moves existing objects out of GC tracking and reduces this.
- **Inherited resources are shared, not duplicated.** A database socket opened in the parent is the **same** TCP connection in every child. Two workers writing to one Postgres or TLS connection corrupts the protocol stream.

Also, `fork()` copies only the calling thread. If another thread held a lock at fork time, the child inherits a locked lock with no owner. This is why mixing threads and fork is risky, and why Python 3.12+ warns about it and 3.14 changed the default `multiprocessing` start method on Linux to `forkserver`.

### The GIL

CPython's Global Interpreter Lock lets one thread execute Python bytecode at a time per process. It is released during blocking I/O (socket reads, `time.sleep`, most DB driver waits) and in many C extensions (NumPy, hashing large buffers). So:

- **I/O-bound** work (HTTP calls, DB queries): threads give real concurrency.
- **CPU-bound** pure Python: threads don't help; use processes.

Free-threaded CPython builds (3.13t/3.14t) remove the GIL, but most production Django stacks still run the standard build.

## In practice

### gunicorn

```bash
gunicorn config.wsgi:application \
  --workers 4 \
  --threads 4 \
  --worker-class gthread \
  --max-requests 2000 --max-requests-jitter 200 \
  --timeout 30 --graceful-timeout 30 \
  --preload
```

- `--workers` = processes. Starting point: `2 × cores + 1` for sync workers; in a container, use the container's CPU limit, not the host's core count.
- `--threads` with `gthread` = threads per worker. Good for I/O-heavy views; total concurrency is workers × threads.
- `--preload` imports the app in the master before forking: faster startup, more copy-on-write sharing. Don't open DB connections or start threads at import time, or every child inherits them.
- `--max-requests` recycles workers periodically to cap slow memory leaks.
- For long-lived connections (SSE, WebSockets) use ASGI workers (`uvicorn.workers.UvicornWorker`) instead of holding sync workers.

Django opens DB connections lazily per thread, after fork, so the default setup is safe. Code that creates clients at module import (Redis, boto3, gRPC channels) needs care with `--preload`: create them lazily or in a `post_fork` hook.

```python
# gunicorn.conf.py
def post_fork(server, worker):
    from django.db import connections
    connections.close_all()   # drop anything accidentally inherited
```

### Celery pools

| Pool | Model | Use for |
|---|---|---|
| `prefork` (default) | child processes | CPU work, isolation, memory-leaky libraries |
| `threads` | threads in one process | I/O-bound tasks, low memory |
| `gevent` / `eventlet` | green threads, monkey-patched I/O | thousands of concurrent I/O-bound tasks |
| `solo` | one task at a time | debugging, one-task-per-container setups |

```bash
celery -A proj worker --pool=prefork --concurrency=4 \
  --max-tasks-per-child=500 --max-memory-per-child=400000 \
  --prefetch-multiplier=1 -Q default
celery -A proj worker --pool=threads --concurrency=32 -Q webhooks
```

- Separate queues by workload: CPU-heavy (PDF rendering, embeddings) on a prefork worker, I/O-heavy (webhooks, LLM API calls) on a threads or gevent worker.
- `--max-memory-per-child` (KiB) replaces a child after it exceeds the limit, a cheap guard against leaks.
- `--prefetch-multiplier=1` with `acks_late` stops one child hoarding long tasks while others idle.

### Sizing from memory

Workers are usually memory-bound before CPU-bound. If one worker's RSS settles at 250 MB and the container limit is 2 GiB, leave headroom for spikes and the master: 5–6 workers, not 9. Measure with `ps -o pid,rss,cmd --ppid <master_pid>` after warm-up, under real traffic.

## Pitfalls

- **Inherited connections after fork**: random protocol errors, "SSL error", "server closed the connection unexpectedly", results from another worker's query. Open connections after fork.
- **CPU count in containers**: `multiprocessing.cpu_count()` returns host cores (64) inside a 2-CPU container, so `2 × cores + 1` spawns 129 workers and gets OOM-killed. `os.process_cpu_count()` (3.13+) respects CPU affinity but not cgroup quotas. Read the quota from `/sys/fs/cgroup/cpu.max`, or simply set worker counts explicitly per environment.
- **Threads for CPU-bound Python**: no speedup, plus GIL contention adds latency to everything else in the process.
- **Thread-unsafe globals** under `gthread`: module-level caches, mutable class attributes, non-thread-safe clients.
- **Copy-on-write erosion**: memory per worker grows over hours even without a leak; `gc.freeze()` and `--max-requests` help.
- **Forking while threads hold locks** (e.g., a logging handler thread): child deadlocks on first log call.

## Rules of thumb

- Processes for CPU-bound work and isolation, threads or async for I/O-bound concurrency.
- Size workers from measured RSS and the container's CPU limit.
- Never create sockets or start threads at import time in a pre-forking server.
- Recycle workers (`--max-requests`, `--max-tasks-per-child`) as a leak safety net.
- Split Celery queues by workload type and give each its own pool.

## Interview angle

- **"Process vs thread?"** A process has its own address space; threads share their process's memory. Processes are isolated and heavier; threads are cheaper and need synchronization.
- **"Why doesn't Python threading speed up CPU-bound code?"** The GIL lets only one thread execute bytecode at a time; it is released on blocking I/O, so threads help I/O-bound work only.
- **"How would you configure gunicorn for an I/O-heavy Django API?"** A few processes per core with `gthread` threads, or async workers for long-lived connections; size by memory; recycle workers; use a connection pooler so workers × threads doesn't exceed Postgres `max_connections`.
