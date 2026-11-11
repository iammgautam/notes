---
title: Memory basics
section: os-concurrency
order: 2
summary: Virtual memory, stack vs heap, the page cache, the OOM killer and what RSS really means inside a container, with commands to measure each.
---

Most memory incidents in a Python service look the same from the outside: a container restarts with exit code 137, Celery reports "Worker exited prematurely: signal 9", or the dashboard shows memory climbing all day. To fix them you need to know which memory number you are looking at, what the kernel counts against your limit, and who decided to kill your process.

## How it works

### Virtual memory

Every process sees its own flat **virtual address space**. The kernel maps virtual pages (4 KiB) to physical frames via page tables, lazily: `malloc` of 1 GB reserves address space, but physical memory is only used when pages are first touched (a page fault allocates them). That is why virtual size (VSZ) is often huge and meaningless, and why Linux can overcommit.

Key numbers per process:

| Metric | Meaning |
|---|---|
| VSZ / VIRT | all mapped address space, touched or not |
| RSS / RES | physical pages currently resident, including shared ones |
| Shared | resident pages shared with other processes (libs, CoW pages after fork) |
| PSS | RSS with shared pages divided among sharers; sums correctly |
| USS | pages only this process uses; what you'd free by killing it |

Summing RSS across 8 gunicorn workers double-counts shared pages and overstates usage. PSS is the honest per-process figure.

### Stack vs heap

- **Stack**: per thread, holds call frames and local primitives. Fixed max size (default 8 MiB virtual on Linux for the main thread, configurable for others). Overflow means deep recursion; Python raises `RecursionError` at about 1000 frames before the C stack overflows.
- **Heap**: dynamically allocated memory (`malloc`). In CPython almost every object lives on the heap; the "variable" on the stack is just a reference.

CPython uses its own allocator (pymalloc) for small objects (512 bytes or less) in 256 KiB arenas. An arena is returned to the OS only when **completely** empty. So after you load 2 million rows into Python objects and drop them, RSS often stays high because a few live objects pin each arena. That is fragmentation, not a leak, and it explains why "memory never goes down" after a big queryset.

### Page cache

Linux uses free RAM to cache file contents (the page cache). Reading the same file twice hits RAM the second time. This memory is reclaimable, so `free` shows little "free" but plenty "available". Postgres relies on it heavily: `shared_buffers` plus the OS page cache hold the working set, which is why `effective_cache_size` is set to ~50–75% of RAM.

```bash
free -h
#               total   used   free  shared  buff/cache  available
# Mem:           31Gi   9.1Gi  1.2Gi  412Mi    21Gi       21Gi
```

Look at **available**, not free.

### Swap

When RAM is tight, the kernel can move anonymous pages to swap. Many container platforms run without swap. With it, a leaking service slows to a crawl instead of dying, which can be worse because health checks keep passing.

### The OOM killer and cgroups

When memory can't be reclaimed, the kernel's OOM killer picks a victim by `oom_score` (mostly proportional to memory use, adjustable by `oom_score_adj`) and sends `SIGKILL`. No cleanup, no exception, no traceback.

Containers add a cgroup memory limit. When the cgroup exceeds `memory.max`, the kernel reclaims page cache inside the cgroup first, then OOM-kills a process **inside that cgroup**, even if the host has plenty of free RAM. Docker and Kubernetes report this as exit code 137 (128 + 9) and `OOMKilled: true`.

What counts against the cgroup limit: anonymous memory (heap, stacks), page cache from files the container read or wrote, tmpfs (`/dev/shm`, `emptyDir: medium: Memory`), and kernel memory for sockets. Page cache is reclaimable, so it rarely causes a kill by itself, but it inflates the "memory usage" graph.

## In practice

### Find out what is using memory

```bash
# per process, sorted
ps -eo pid,ppid,rss,vsz,cmd --sort=-rss | head
# accurate shared accounting (smem package)
smem -t -k -P gunicorn
# one process in detail: Rss, Pss, Anonymous, Swap
grep -E '^(Rss|Pss|Anonymous|Swap)' /proc/<pid>/smaps_rollup

# container (cgroup v2) view
cat /sys/fs/cgroup/memory.max
cat /sys/fs/cgroup/memory.current
grep -E '^(anon|file|shmem) ' /sys/fs/cgroup/memory.stat
cat /sys/fs/cgroup/memory.events      # oom, oom_kill counters

docker stats --no-stream
kubectl top pod; kubectl describe pod <pod> | grep -A3 'Last State'

# was it the OOM killer?
dmesg -T | grep -i -E 'killed process|out of memory'
```

`memory.stat` splits `anon` (your heap, the real problem if it grows) from `file` (page cache, reclaimable). Kubernetes' "working set" metric is usage minus inactive file cache, and it is what the eviction logic watches.

### Find a Python leak

```python
import tracemalloc

tracemalloc.start(25)
snap1 = tracemalloc.take_snapshot()
# ... run the suspect code path a few hundred times ...
snap2 = tracemalloc.take_snapshot()
for stat in snap2.compare_to(snap1, "lineno")[:10]:
    print(stat)
```

Common culprits in Django apps: unbounded module-level caches or `lru_cache` without `maxsize`, `DEBUG=True` storing every SQL query in `connection.queries`, big querysets evaluated into lists, and accumulating objects in a long-lived Celery worker. For running processes, `py-spy dump` and `memray` help without code changes.

### Keep memory flat when processing large data

```python
# Bad: loads every row and caches model instances
for order in Order.objects.filter(tenant=t):
    export(order)

# Better: stream rows with a server-side cursor, no result cache
for order in Order.objects.filter(tenant=t).iterator(chunk_size=2000):
    export(order)

# Often best: only the columns you need
for row in Order.objects.filter(tenant=t).values_list("id", "total").iterator(chunk_size=5000):
    write_csv(row)
```

### Container limits

- Set memory **requests = limits** for predictable services, or at least set a limit.
- Leave 20–30% headroom over steady-state peak.
- Size workers from measured per-worker PSS (see [Processes vs threads]({{ '/os-concurrency/processes-vs-threads/' | relative_url }})).
- Recycle workers: gunicorn `--max-requests`, Celery `--max-memory-per-child`.
- For glibc fragmentation in threaded workers, `MALLOC_ARENA_MAX=2` often reduces RSS noticeably.

## Pitfalls

- **Reading VSZ as usage.** Threads reserve 8 MiB stacks each; VSZ looks enormous, RSS is small.
- **Summing RSS across forked workers** and concluding you need a bigger box.
- **"Memory never goes down"** after a big job: arena fragmentation. Process in chunks, or do big jobs in short-lived processes.
- **Silent OOM kills**: SIGKILL leaves no Python traceback. Check `dmesg`, container status, Celery's "signal 9" and `memory.events`.
- **Page cache panic**: "container at 95% memory" where most of it is `file`. Look at `anon`.
- **Big uploads into memory**: reading entire files with `request.body` or building huge JSON responses. Stream instead.
- **`/dev/shm` usage counts toward the limit** (and Docker's default is 64 MB, which breaks some libraries that use it).

## Checklist

- Watch `anon` / working set, not total usage or VSZ.
- Alert on OOM kills (container restarts with 137, `oom_kill` counter).
- Stream large querysets with `.iterator()` and `values_list`.
- Bound every in-process cache.
- Recycle long-running workers.
- `DEBUG=False` everywhere but local.

## Interview angle

- **"What is virtual memory?"** Each process gets its own address space mapped to physical memory by the kernel in pages, allocated lazily, which gives isolation, overcommit and features like copy-on-write and memory-mapped files.
- **"Stack vs heap?"** The stack holds per-thread call frames with automatic lifetime; the heap holds dynamically allocated objects that live until freed or garbage collected.
- **"A container keeps restarting with exit code 137. What do you do?"** It was SIGKILLed, usually by the OOM killer. Confirm via container status and `dmesg`, check whether `anon` memory grows (leak) or spikes (big request or job), profile with tracemalloc or memray, then fix the code or right-size limits and worker counts.
