---
title: Memory management and CPU profiling
section: backend
order: 59
phase: "Phase 8 · Performance and reliability"
summary: How CPython allocates and frees memory, how to find leaks in long-running workers, why containers get OOM-killed, and how to profile CPU.
---

Two production symptoms send you here. A Celery worker's memory climbs for three days until Kubernetes kills it with exit code 137. Or an endpoint got slower after a release and nobody can say which function is to blame. Both have the same answer: stop guessing and measure, with the right tool for the question. You need enough of the runtime's memory model to read the measurements, and enough profiler theory to trust them.

## Memory management

### The Python memory model

In CPython everything is a heap object with a header holding a reference count and a type pointer. Variables are names bound to references, so `a = b` copies a pointer, not data.

- **Small objects** (512 bytes or less) come from `pymalloc`, which carves fixed-size blocks out of pools inside larger arenas. Bigger allocations go to the system `malloc`.
- An arena goes back to the operating system only when **every** object in it is free. A long-running process that allocated a million small objects and freed 90% of them often keeps most of that memory: RSS stays high without a leak. This is fragmentation, and it is why "memory never goes down" alone proves nothing.
- `sys.getsizeof` is shallow: it reports the container, not what it references. A list of 1,000 dicts is mostly the dicts.
- Rough sizes on 64-bit CPython: a small `int` is 28 bytes, an empty `dict` 64 bytes, a five-key `dict` about 180 bytes before counting its keys and values. Millions of model instances or row dicts add up fast; `__slots__`, tuples or columnar data help.

### Reference counting and the cycle collector

CPython frees most objects immediately. When a reference count drops to zero, the object is deallocated and the counts of everything it referenced drop too. That makes memory use predictable and finalization prompt.

Reference counting can't free cycles (`a.peer = b; b.peer = a`). A separate **cycle collector** periodically walks container objects, finds groups reachable only from each other, and frees them. It is generational, so new objects are checked often and survivors rarely, and CPython 3.14 made it incremental to shorten pauses. You can trigger it with `gc.collect()`, but rarely need to.

A prefork detail with real impact: gunicorn `--preload` and Celery prefork load the app in the parent, then fork. Children share those pages copy-on-write, but merely reading an object updates its refcount, which writes to the page and forces a private copy. Shared memory erodes over time. Instagram publicly described this and the fix, `gc.freeze()` (Python 3.7+), which moves everything allocated so far into a permanent generation so the collector stops touching those pages. Python 3.12's immortal objects (PEP 683) help for singletons like `None` and small ints.

### Leaks in long-running workers

Web and queue workers live for days, so anything that grows per request eventually kills them.

| Cause | Typical code | Fix |
|---|---|---|
| Unbounded module-level cache | `_cache[key] = value` keyed by user or request | `lru_cache(maxsize=...)`, TTL cache, or Redis |
| `lru_cache` on a method | `@lru_cache` on an instance method keeps `self` alive | Cache at module level on hashable args, or `cached_property` |
| Growing global list | Metrics or events appended "temporarily" | Bounded `deque(maxlen=...)` or flush |
| `DEBUG=True` in a worker | Django keeps recent queries per connection | Never run workers with `DEBUG=True` |
| Loading whole result sets | `list(Model.objects.all())` over a million rows | `.iterator(chunk_size=2000)`, pagination, `values_list` |
| Thread-locals and contextvars not reset | Request state stays on pooled threads | Reset in `finally` |
| Native allocations | C extension or library leak, invisible to Python tools | `memray --native`, upgrade, isolate in a subprocess |

The pragmatic safety net is recycling: gunicorn `max_requests` with jitter, Celery `worker_max_tasks_per_child` and `worker_max_memory_per_child` (in KiB). Recycling caps the damage from a slow leak. It does not fix it, so keep it and investigate.

### Finding leaks with tracemalloc

`tracemalloc` (stdlib) records the traceback of every allocation made through Python's allocators. The workflow: start it, warm up, take a snapshot, run the suspect workload, take another snapshot, and diff by line. The lines whose net allocation grows with the number of iterations are your leak. It adds noticeable CPU and memory overhead, so use it in a reproduction or behind a flag rather than always-on.

Other tools: **memray** (Bloomberg) tracks native allocations too and renders flame graphs of where memory was allocated. **objgraph** shows which object types are growing and who references them. `gc.get_referrers()` answers "why is this still alive?" in a pinch.

### Container memory limits and OOM

```text
 container memory limit (cgroup memory.max), e.g. 1 GiB
 +----------------------------------------------------------+
 | gunicorn | worker 1 | worker 2 | worker 3 | page cache    |
 | master   | 260 MiB  | 270 MiB  | 300 MiB  | (reclaimable) |
 +----------------------------------------------------------+
   usage hits the limit and nothing can be reclaimed
   -> kernel OOM killer SIGKILLs a process -> exit 137
```

- Kubernetes memory limits become a cgroup `memory.max`. When usage can't be reclaimed below it, the kernel OOM killer sends `SIGKILL`: no exception, no traceback, no graceful shutdown. Kubernetes reports `OOMKilled` and exit code 137 (128 + 9).
- Unlike CPU, memory isn't throttled; it is fatal. A CPU limit only slows you down.
- `kubectl top` and the `container_memory_working_set_bytes` metric show usage minus inactive file cache, which is closer to what triggers OOM than raw usage.
- The total is per container: master plus every worker plus their threads. Size workers from measured per-worker RSS at steady state, plus headroom for the largest request.
- Python doesn't know the limit. Read `/sys/fs/cgroup/memory.max` if you need it (cgroup v2), and watch out for `cpu_count()` reporting host cores, which inflates worker counts and memory.
- Many teams set memory limit equal to request, so the scheduler places pods on nodes that really have the memory.

## CPU profiling

### Sampling vs instrumenting

| | Instrumenting (deterministic) | Sampling (statistical) |
|---|---|---|
| How | Hooks every function call and return | Captures stacks N times per second |
| Python tools | `cProfile`, `profile` | py-spy, Austin, pyinstrument, Scalene, Linux `perf` |
| Overhead | High for call-heavy code (often 2× or worse) | Low, tunable by sample rate |
| Accuracy | Exact call counts; inflates tiny functions | Approximate; misses very short functions |
| Production | Rarely | Yes; can attach to a running process |
| Best for | Exact call counts in a reproduction, micro-optimizing | "Where does the time go?" on real traffic |

### cProfile

`python -m cProfile -o out.prof script.py` or `cProfile.Profile()` around the suspect code. In `pstats` output:

- `ncalls`: how many times called. A surprise here (40,000 calls to a helper that should run 50 times) is often the whole story.
- `tottime`: time spent in the function itself, excluding callees. Sort by this to find hot spots.
- `cumtime`: time including callees. Sort by this to find expensive subtrees.

Visualize with snakeviz, or convert to a flame graph. Because instrumentation adds a constant cost per call, functions called millions of times look worse than they are; confirm a fix with a plain timer.

### py-spy

py-spy is a sampling profiler that reads a Python process's memory from outside. No code changes, no restart, low overhead:

```bash
py-spy top --pid 4312                                  # live 'top' by function
py-spy record --pid 4312 --duration 30 -o cpu.svg      # flame graph SVG
py-spy record --pid 4312 --idle --format speedscope -o wall.json
py-spy dump --pid 4312                                 # stack of every thread now
```

`py-spy dump` is the first command for a hung worker: it shows exactly which line each thread is stuck on. In Docker and Kubernetes the container needs the `SYS_PTRACE` capability (or run py-spy from a privileged debug container). By default py-spy shows only threads that are running; `--idle` includes waiting threads, which turns it into a wall-clock profile. On Linux, Python 3.12+ also supports `perf` directly with `-X perf`.

### Flame graphs

```text
            +---------+
            | _encode |
 +----------+---------+--+ +----------------+
 | to_representation     | | cursor.execute |
 +-----------------------+-+----------------+-----+
 | OrderListView.list                             |
 +------------------------------------------------+--------+
 | gunicorn worker / django handler                        |
 +---------------------------------------------------------+
  x: share of samples (sorted, NOT time order)   y: stack depth
```

Each box is a function. Its width is the fraction of samples in which it appeared on the stack, and boxes above it are its callees. Read wide plateaus at the top: a function that is wide and has nothing above it is burning CPU itself. The x-axis is sorted alphabetically to merge identical stacks, so left-to-right is not a timeline. Differential flame graphs (before vs after a release) color what grew. Brendan Gregg introduced the visualization, and most profilers now emit it.

### CPU time vs wall-clock time

A CPU profile of an IO-bound endpoint shows almost nothing, because the time is spent waiting for Postgres or an HTTP call. Use a wall-clock profile (`--idle`) or, better, a trace with spans per query and outbound call; see [Distributed tracing and OpenTelemetry]({{ '/backend/distributed-tracing-and-opentelemetry/' | relative_url }}). Also check for CPU throttling: a container at its CFS quota shows modest CPU use but long pauses. The signal is `container_cpu_cfs_throttled_periods_total` climbing.

## In practice

### tracemalloc and cProfile, runnable

```python
import cProfile
import gc
import io
import linecache
import pstats
import time
import tracemalloc
import weakref

# --- refcounting vs the cycle collector --------------------------------------
class Node:
    pass


def cycle_survives_refcounting():
    gc.disable()
    a, b = Node(), Node()
    a.peer, b.peer = b, a                  # a reference cycle
    probe = weakref.ref(a)
    del a, b
    alive_before = probe() is not None     # refcounts never reached zero
    gc.collect()
    alive_after = probe() is not None
    gc.enable()
    return alive_before, alive_after


# --- a slow leak, found with tracemalloc --------------------------------------
_report_cache = {}


def handle_request(i):
    key = f"report:{i}"                    # unique key per request: grows forever
    _report_cache[key] = "x" * 10_000
    return len(_report_cache[key])


def find_leak():
    tracemalloc.start()
    for i in range(100):                   # warm up so one-time allocations settle
        handle_request(i)
    before = tracemalloc.take_snapshot()
    for i in range(100, 1100):
        handle_request(i)
    after = tracemalloc.take_snapshot()
    tracemalloc.stop()
    top = after.compare_to(before, "lineno")[0]
    frame = top.traceback[0]
    return top.size_diff, linecache.getline(frame.filename, frame.lineno).strip()


# --- a CPU hot spot, found with cProfile --------------------------------------
VIP_LIST = list(range(0, 20_000, 7))
VIP_SET = frozenset(VIP_LIST)


def is_vip(customer_id, vips):
    return customer_id in vips             # O(n) for a list, O(1) for a set


def render_row(row, vips):
    tag = "VIP" if is_vip(row["customer_id"], vips) else "-"
    return f"{row['id']},{tag}"


def build_report(rows, vips):
    return "\n".join(render_row(r, vips) for r in rows)


def hottest_function(rows):
    prof = cProfile.Profile()
    prof.enable()
    build_report(rows, VIP_LIST)
    prof.disable()
    out = io.StringIO()
    pstats.Stats(prof, stream=out).sort_stats("tottime").print_stats(3)
    funcs = pstats.Stats(prof).get_stats_profile().func_profiles
    return max(funcs, key=lambda name: funcs[name].tottime), out.getvalue()


def timed(fn, *args):
    start = time.perf_counter()
    fn(*args)
    return time.perf_counter() - start


if __name__ == "__main__":
    assert cycle_survives_refcounting() == (True, False)

    grew, line = find_leak()
    print(f"top growth: {grew / 1e6:.1f} MB at: {line}")
    assert grew > 9_000_000 and "_report_cache[key]" in line

    rows = [{"id": i, "customer_id": i * 3} for i in range(3000)]
    name, report = hottest_function(rows)
    print(next(line for line in report.splitlines() if f"({name})" in line))
    assert name == "is_vip"
    slow, fast = timed(build_report, rows, VIP_LIST), timed(build_report, rows, VIP_SET)
    print(f"list lookup {slow * 1000:.0f} ms, set lookup {fast * 1000:.1f} ms")
    assert build_report(rows, VIP_LIST) == build_report(rows, VIP_SET)
    assert slow > 5 * fast
```

### Worker settings that contain leaks (illustrative)

```python
# celery config
worker_max_tasks_per_child = 500        # replace the child process after 500 tasks
worker_max_memory_per_child = 400_000   # KiB: replace after a task if RSS > ~400 MB
```

```python
# gunicorn.conf.py
import gc

gc.disable()                  # parent: don't free 'holes' into preloaded pages
preload_app = True            # load the app once in the master, share it via COW
max_requests = 2000
max_requests_jitter = 200

def pre_fork(server, worker):     # runs in the master just before each fork
    gc.freeze()                   # preloaded objects move to a permanent generation

def post_fork(server, worker):    # runs in the new worker
    gc.enable()                   # collect normally; frozen objects are skipped
```

This is the sequence the `gc.freeze()` documentation recommends: disable early in the parent, freeze right before `fork()`, enable early in the child.

### Kubernetes resources (illustrative)

```yaml
resources:
  requests:
    cpu: "1"
    memory: 1Gi
  limits:
    memory: 1Gi          # equal to the request: no overcommit surprises
```

## Pitfalls

- **Reading RSS as a leak detector.** Allocator retention and fragmentation keep RSS flat or high after a spike. A leak is a trend that grows with requests or tasks over hours, not a high plateau.
- **Python tools can't see native leaks.** tracemalloc only tracks Python allocators. If RSS grows but tracemalloc doesn't, suspect a C extension and use `memray --native`.
- **Loading everything.** A one-off export that does `list(queryset)` over five million rows spikes a worker past its limit. On PostgreSQL, `.iterator()` streams with a server-side cursor, but those don't work through PgBouncer in transaction mode (set `DISABLE_SERVER_SIDE_CURSORS` and paginate instead).
- **Profiling the wrong thing.** cProfile on tiny local data, a CPU profile for an IO-bound endpoint, or profiling with `DEBUG=True` all point you at the wrong code.
- **cProfile in production.** Its overhead changes the program you are measuring. Use a sampling profiler.
- **CPU throttling mistaken for slow code.** Latency spikes with low average CPU usage and rising throttled periods: raise or remove the CPU limit, or reduce worker count.
- **OOM with no logs.** SIGKILL leaves no traceback. Check `kubectl describe pod` for `OOMKilled`, then correlate memory metrics with the task or request that was running.
- **py-spy fails with permission denied.** Add `SYS_PTRACE` or use an ephemeral debug container; on macOS it needs root.

## Interview questions

**How does Python free memory?**
Reference counting frees an object as soon as its count hits zero. A generational cycle collector periodically finds and frees reference cycles. Small objects come from pymalloc arenas, which are returned to the OS only when entirely empty, so RSS doesn't always shrink.

**A Celery worker's memory grows for days. How do you investigate?**
Confirm it's a trend per task, not a plateau, using per-worker RSS over time. Reproduce with the suspect task in a loop under tracemalloc and diff snapshots by line. If Python-level memory is flat while RSS grows, use memray with native tracking. Meanwhile, contain it with `worker_max_tasks_per_child` or `worker_max_memory_per_child`.

**Why doesn't RSS drop after you delete a big structure?**
Freed small objects return to pymalloc's free lists, and an arena is released only when every block in it is free. The memory gets reused by the process but isn't handed back to the OS.

**A pod restarts with exit code 137. What happened?**
The process was killed by SIGKILL, almost always the OOM killer when the container exceeded its memory limit (`OOMKilled` in `kubectl describe`). Check worker count × per-worker RSS against the limit, look for request-sized spikes like full result loads, and check for leaks.

**Sampling vs instrumenting profilers?**
Instrumenting profilers like cProfile hook every call, giving exact counts but large overhead that distorts small functions. Sampling profilers like py-spy capture stacks periodically, giving low overhead and statistical accuracy. They're safe to attach to production processes.

**How do you read a flame graph?**
Width is the share of samples containing that frame, height is stack depth, and the x-axis order is alphabetical rather than time. Wide frames with nothing above them are where CPU is actually spent.

**An endpoint is slow but the CPU profile is flat. Why?**
The time is spent waiting on IO or locks, not on CPU. Use wall-clock profiling (`py-spy --idle`) or distributed traces, and check for CPU throttling and connection-pool waits.

**Why does memory of preforked workers grow even without leaks?**
Copy-on-write pages shared with the parent get copied when touched, and refcount updates and GC traversal touch them constantly. `gc.freeze()` before forking keeps the collector off them.

## Further reading

- Python documentation: `tracemalloc`, `gc`, "The Python Profilers" (`cProfile`, `pstats`), and "Memory Management" in the C API docs.
- Brendan Gregg, *Systems Performance* (2nd edition), and his ACM Queue article "The Flame Graph".
- The py-spy and memray project documentation.
- Linux kernel documentation: "Control Group v2" (memory controller and OOM behavior).
- Instagram Engineering, "Dismissing Python Garbage Collection at Instagram".
