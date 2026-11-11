---
title: Memory and garbage collection
section: python-django
order: 2
summary: Reference counting, the cycle collector, why RSS never goes down, and how to find and contain leaks in Gunicorn and Celery workers.
---

A web request lives for 50 ms, so sloppy memory use rarely shows. A Gunicorn worker or Celery worker lives for days. Anything that grows a little per request or per task (a module-level cache, a list of query logs, a reference cycle holding a big queryset) becomes an OOM kill at 3 a.m. To fix it you need to know what CPython actually frees and when.

## How it works

### Reference counting (the main mechanism)

Every CPython object has a reference count. Binding a name, putting the object in a list, or passing it as an argument increments it; unbinding decrements it. When it hits zero, the object is freed **immediately**, deterministically, in the same thread.

```python
import sys

rows = list(range(1_000_000))
sys.getrefcount(rows)   # 2: the name, plus the temporary argument
del rows                # refcount 0, ~8 MB of pointers freed right now
```

This is why most memory in Python is released promptly and why `with open(...)` mostly worked even before people used `with`.

### The cycle collector

Refcounting cannot free cycles: `a.ref = b; b.ref = a` keeps both counts at 1 forever. The `gc` module handles this. It tracks container objects (lists, dicts, class instances) and periodically finds groups that are only reachable from each other.

Up to Python 3.13 it is **generational** with three generations:

- New container objects go into generation 0.
- When allocations minus deallocations exceed `threshold0` (default 700), gen 0 is collected. Survivors move to gen 1.
- Gen 1 and gen 2 are collected less often (`gc.get_threshold()` returns `(700, 10, 10)`).

Python 3.14 replaced this with an **incremental** collector (young and old generations, old scanned in increments) to cut long pauses on big heaps. The tuning knobs changed; the idea (most objects die young, cycles are rare) is the same.

A full collection walks every tracked object, so it costs time proportional to heap size. On a worker holding millions of objects, a gen-2 pass can take tens to hundreds of milliseconds.

### Why RSS doesn't shrink

CPython's small-object allocator (pymalloc) carves memory into arenas and pools. An arena is returned to the OS only when **every** object in it is freed. Load 500k ORM instances, drop all but a few scattered survivors, and the process keeps most of that memory. Memory "leaks" in long-running workers are often this: a high-water mark from one huge task, not a true leak.

### Weak references

`weakref.ref(obj)` points to an object without incrementing its count. `WeakValueDictionary` and `WeakKeyDictionary` let you build caches and registries that don't keep things alive. Django signals use weak references to receivers by default (`weak=True`), which is why a receiver defined as a local closure can silently disappear.

## In practice

### Finding what's growing

`tracemalloc` (stdlib) records allocation tracebacks. Take two snapshots and diff them:

```python
import tracemalloc

tracemalloc.start(25)            # keep 25 frames per allocation
before = tracemalloc.take_snapshot()

for _ in range(200):
    process_invoice_batch(tenant_id=7)

after = tracemalloc.take_snapshot()
for stat in after.compare_to(before, "traceback")[:5]:
    print(stat)
    for line in stat.traceback.format()[-6:]:
        print("   ", line)
```

The top entries show file and line of the allocations that grew. Other tools: `objgraph.show_growth()` for counts by type, `memray` for flame graphs of a whole process, `py-spy dump` to see where a stuck worker is.

### Common leak sources in Django and Celery

```python
# 1. Unbounded module-level cache: grows per tenant / per key forever
_rates = {}
def fx_rate(tenant_id, currency):
    key = (tenant_id, currency)
    if key not in _rates:
        _rates[key] = fetch_rate(tenant_id, currency)
    return _rates[key]

# Fix: bound it
from functools import lru_cache

@lru_cache(maxsize=4096)
def fx_rate(tenant_id, currency):
    return fetch_rate(tenant_id, currency)
```

| Source | Symptom | Fix |
|---|---|---|
| `DEBUG = True` in a worker | `connection.queries` holds up to 9000 SQL strings per connection | Never run workers with `DEBUG=True` |
| `@lru_cache` on a method | Caches `self`, so instances never die | Cache a module function, or use `cached_property` |
| Huge queryset in a task | RSS jumps by GBs and stays | Stream with `.iterator()` and batch |
| Signal receivers / callbacks registered per call | Receiver list grows | Register once at import, use `dispatch_uid` |
| Exceptions stored in a list | Traceback keeps every frame's locals alive | Store `str(exc)` or clear `__traceback__` |
| Global HTTP sessions per tenant | Connection pools accumulate | One shared client, bounded |

### Containment: recycle workers

You can't always find every leak, so cap the damage.

```bash
# Gunicorn: restart each worker after ~1000 requests, jitter avoids
# all workers restarting at once
gunicorn app.wsgi --workers 4 --max-requests 1000 --max-requests-jitter 100

# Celery: replace a child after N tasks or when it exceeds ~512 MB RSS
celery -A app worker --max-tasks-per-child 500 \
  --max-memory-per-child 524288        # in KiB
```

`--max-memory-per-child` is checked **after** a task finishes, so a single task that allocates 4 GB still OOMs. Fix those by streaming.

### gc.freeze for prefork servers

Gunicorn with `--preload` and Celery prefork load the app in the parent and `fork()` children. Pages are shared copy-on-write, but the cycle collector writes to every object header it scans, and refcount changes touch them too, so pages get copied anyway. Calling `gc.freeze()` in the parent just before forking moves all existing objects to a permanent generation the collector ignores, keeping more memory shared.

```python
# gunicorn.conf.py
import gc

def pre_fork(server, worker):
    gc.freeze()
```

Some teams also call `gc.disable()` and run `gc.collect()` manually between requests to remove GC pauses. That works only if you are confident your code doesn't create many cycles; measure first.

## Pitfalls

- **Treating high RSS as a leak.** Check whether memory plateaus after the first big task. Steady growth per task is a leak; a step and a plateau is fragmentation.
- **Cycles with `__del__`.** Since 3.4 these are collected, but finalizer order is not guaranteed. Don't release locks or close connections in `__del__`; use context managers.
- **Holding querysets across a loop.** `qs = Order.objects.all(); for o in qs:` fills `qs._result_cache` with every instance. The whole list stays alive until `qs` goes out of scope.
- **Celery result backend bloat.** Storing big return values in Redis isn't process memory, but it fills Redis. Return IDs, not payloads, and set `result_expires` or `ignore_result=True`.
- **OOM killer with no traceback.** The kernel sends SIGKILL. Celery logs `WorkerLostError` / "exited with signal 9"; Kubernetes shows `OOMKilled`. Graph container memory per worker, not just per pod.

## Checklist

- Bound every cache (`lru_cache(maxsize=...)`, TTL, or an external cache).
- Never run long-lived processes with `DEBUG=True`.
- Stream large querysets; return IDs from tasks.
- Set `--max-requests` (Gunicorn) and `--max-tasks-per-child` / `--max-memory-per-child` (Celery).
- Use `tracemalloc` snapshot diffs before guessing.
- Consider `gc.freeze()` before fork in preloaded servers.

## Interview angle

**How does Python free memory?** Reference counting frees most objects immediately when the count drops to zero. A generational (incremental in 3.14) cycle collector handles reference cycles.

**Why does my worker's memory keep growing even though nothing is leaking?** pymalloc returns arenas to the OS only when completely empty, so a peak allocation leaves a high-water mark. Stream data and recycle workers.

**When would you use `weakref`?** Caches, observer registries and parent back-pointers, where you don't want the reference to keep the object alive.
