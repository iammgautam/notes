---
title: Non-blocking I/O
section: backend
order: 58
phase: "Phase 8 · Performance and reliability"
summary: How blocking, non-blocking and async I/O differ, how epoll and kqueue power event loops and asyncio, and when ASGI beats WSGI and threads.
---

Most backend time is spent waiting: for the database, for Redis, for another service, for a slow client to send the rest of its body. With blocking I/O, every wait parks a thread, so concurrency is capped by how many threads you can afford. Non-blocking I/O lets one thread wait on thousands of sockets at once and work on whichever is ready. That is how nginx, Redis, Node.js and Python's asyncio serve huge numbers of connections from a handful of threads, and why WebSockets and long polling need an ASGI server rather than a pool of WSGI workers.

## Non-blocking I/O

### Blocking, non-blocking and asynchronous

| Model | What `read` does | The thread while waiting | Examples |
|---|---|---|---|
| Blocking | Sleeps until data arrives | Parked, one per in-flight operation | Gunicorn sync worker, `requests`, psycopg2 |
| Non-blocking | Returns `EAGAIN` immediately if no data | Free, but must retry later | Socket with `O_NONBLOCK` |
| Readiness multiplexing | `epoll_wait` reports which sockets are readable | One thread watches many sockets | nginx, Redis, asyncio, libuv |
| Completion-based async | Submit the read; get notified when it's done | Free; the kernel does the copy | io_uring, Windows IOCP |

Non-blocking on its own is just a flag that makes calls fail fast. It becomes useful when combined with a multiplexer that says *which* sockets are ready, so you never spin. "Async" usually means the programming model built on top: callbacks, futures, or `async`/`await` coroutines that suspend at each wait and resume when the event loop sees their socket is ready.

### select, poll, epoll and kqueue

| API | Platforms | Cost per wait | Notes |
|---|---|---|---|
| `select` | Everywhere | `O(n)` in watched fds | Bitmap capped by `FD_SETSIZE` (typically 1024); rebuilt every call |
| `poll` | POSIX | `O(n)` | No fd cap, but the whole array is copied and scanned every call |
| `epoll` | Linux | `O(ready)` | Register once with `epoll_ctl`; `epoll_wait` returns only ready fds |
| `kqueue` | BSD, macOS | `O(ready)` | Also delivers timers, signals, process and file events |
| `io_uring` | Linux 5.1+ | Batched submissions | Completion model; also covers disk I/O |

The jump from `select`/`poll` to `epoll`/`kqueue` is the important one: with 10,000 mostly idle connections, the old APIs scan all 10,000 on every wakeup, while the new ones hand back the 12 that are ready.

`epoll` supports two modes. **Level-triggered** (default) keeps reporting a socket while unread data remains. **Edge-triggered** (`EPOLLET`) reports only the transition to readable, so you must read until `EAGAIN` or you'll never hear about the leftover bytes. Python's `selectors.DefaultSelector` picks the best available API: `epoll` on Linux, `kqueue` on macOS.

### The event loop

```text
 +-------------------------------------------------------+
 |                      event loop                       |
 |                                                       |
 |  1. run every ready callback / resume ready coroutine |
 |  2. timeout = time until the nearest timer            |
 |  3. epoll_wait(all fds, timeout)   <- the only block  |
 |  4. coroutines waiting on ready fds become ready      |
 |  5. expired timers become ready; go to 1              |
 +-------------------------------------------------------+
      ^            ^            ^                  ^
   socket 1     socket 2     socket 3   ...   socket 10,000
```

The loop is single-threaded and cooperative. A coroutine runs until it hits `await` on something not ready, then yields. If a coroutine never yields (CPU work, `time.sleep`, a blocking driver), every other connection on that loop waits with it. That is the one rule of event-loop programming: **never block the loop**.

Underneath, a loop is little more than this (illustrative, simplified):

```python
import selectors
import socket

sel = selectors.DefaultSelector()            # epoll on Linux, kqueue on macOS

def accept(server):
    conn, _ = server.accept()
    conn.setblocking(False)
    sel.register(conn, selectors.EVENT_READ, echo)

def echo(conn):
    data = conn.recv(4096)                   # reported ready, so this won't block
    if data:
        conn.send(data)                      # real loops buffer partial writes
    else:
        sel.unregister(conn)
        conn.close()

server = socket.create_server(("0.0.0.0", 8000))
server.setblocking(False)
sel.register(server, selectors.EVENT_READ, accept)

while True:                                  # the event loop
    for key, _ in sel.select(timeout=1.0):   # one syscall waits on every socket
        key.data(key.fileobj)                # dispatch the registered callback
```

### asyncio

asyncio packages that loop with coroutines, tasks, timers and stream APIs:

- `async def` defines a coroutine; `await` suspends it until the awaited thing completes.
- `asyncio.create_task` schedules concurrent work; `asyncio.gather` or `asyncio.TaskGroup` (3.11+, cancels siblings on failure) waits for several.
- `asyncio.timeout(2)` (3.11+) or `wait_for` puts deadlines on awaits.
- `asyncio.Semaphore(n)` bounds concurrency toward a downstream that has limits.
- `asyncio.to_thread` or `loop.run_in_executor` runs blocking code in a thread so the loop stays free.

Some I/O is never truly non-blocking. Regular files always report "ready" to `epoll`, so asyncio offloads file I/O (and libraries like `aiofiles` use a thread pool). `getaddrinfo` DNS lookups also run in the default executor. Drop-in loops like `uvloop` (built on libuv) speed up the loop itself but don't change these rules.

### The C10K problem

In 1999 Dan Kegel's "The C10K problem" asked how a single server could handle 10,000 concurrent clients. Thread-per-connection designs hit limits in memory (a stack per thread), scheduler overhead and context switches, and `select`/`poll` spent most of their time scanning idle descriptors. The answers were the event-driven architecture plus `epoll`/`kqueue`, which nginx, and later Node.js, made mainstream. Since then, single machines have been pushed far beyond: WhatsApp publicly described around two million connections per server on FreeBSD and Erlang in 2012. The bottleneck moved from the I/O API to memory per connection, file descriptor limits (`ulimit -n`), and the application's own work.

### ASGI vs WSGI

| | WSGI | ASGI |
|---|---|---|
| Spec | PEP 3333 | ASGI specification (from the Django Channels project) |
| Interface | `app(environ, start_response)` | `async app(scope, receive, send)` |
| Concurrency | One request per worker thread or process | Many requests per event loop |
| Protocols | HTTP request/response | HTTP, WebSocket, lifespan events |
| Long-lived connections | Each one ties up a worker | Cheap while idle |
| Servers | Gunicorn, uWSGI, mod_wsgi | Uvicorn, Hypercorn, Daphne, Granian |
| Frameworks | Django, Flask | Django (async views), FastAPI/Starlette, Quart |

Switching Django to ASGI does not make a sync codebase faster. Sync views run in threads via `sync_to_async`, and Django's docs warn each sync/async switch costs on the order of a millisecond. As of Django 5.x the async ORM methods (`aget`, `acount`, `async for`) still wrap synchronous database calls in a thread, so database concurrency is still bounded by threads and connections. ASGI pays off when views fan out to several slow HTTP services, hold WebSockets or SSE streams, or wait on LLM APIs for seconds.

### When non-blocking I/O doesn't help

- **CPU-bound work.** The loop runs one coroutine at a time; use processes or a task queue.
- **A narrower downstream.** Ten thousand coroutines sharing a 20-connection database pool still execute 20 queries at a time.
- **Sync-only libraries.** Every blocking call must be wrapped in a thread, which erodes the benefit.
- **Moderate concurrency.** For a few hundred concurrent requests of mostly database work, threads are simpler and fast enough.

## In practice

### Fan-out in a Django async view (illustrative)

```python
import asyncio
import httpx
from django.http import JsonResponse

client = httpx.AsyncClient(timeout=2.0)      # one per process: reuses connections
limit = asyncio.Semaphore(20)                # cap concurrency toward each upstream

async def fetch(url):
    async with limit:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()

async def dashboard(request):
    tid = request.tenant_id
    async with asyncio.timeout(3):           # Python 3.11+
        billing, usage = await asyncio.gather(
            fetch(f"{BILLING_URL}/tenants/{tid}/summary"),
            fetch(f"{USAGE_URL}/tenants/{tid}/summary"),
        )
    plan = await Plan.objects.aget(tenant_id=tid)
    return JsonResponse({"plan": plan.name, "billing": billing, "usage": usage})
```

Two 400 ms upstream calls take about 400 ms together instead of 800 ms in sequence, without holding two threads. Serve with an ASGI server, for example `uvicorn myproject.asgi:application --workers 4`.

### asyncio vs threads, runnable

```python
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

N, DELAY = 500, 0.05


async def waits_asyncio():
    start = time.perf_counter()
    await asyncio.gather(*(asyncio.sleep(DELAY) for _ in range(N)))
    return time.perf_counter() - start


def waits_threads(workers):
    start = time.perf_counter()
    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(lambda _: time.sleep(DELAY), range(N)))
    return time.perf_counter() - start


async def worst_loop_lag(blocking):
    """How late does a 10 ms timer fire while a 'handler' runs on the same loop?"""
    async def ticker():
        worst = 0.0
        for _ in range(10):
            t = time.perf_counter()
            await asyncio.sleep(0.01)
            worst = max(worst, time.perf_counter() - t - 0.01)
        return worst

    async def handler():
        if blocking:
            time.sleep(0.2)                           # blocks every coroutine
        else:
            await asyncio.to_thread(time.sleep, 0.2)  # loop stays responsive

    worst, _ = await asyncio.gather(ticker(), handler())
    return worst


async def echo_server(clients=100):
    async def handle(reader, writer):
        line = await reader.readline()
        await asyncio.sleep(0.05)                     # slow upstream per request
        writer.write(line.upper())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def client(i):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"hello {i}\n".encode())
        await writer.drain()
        reply = await reader.readline()
        writer.close()
        await writer.wait_closed()
        return reply

    start = time.perf_counter()
    async with server:
        replies = await asyncio.gather(*(client(i) for i in range(clients)))
        threads = threading.active_count()
    return time.perf_counter() - start, replies, threads


if __name__ == "__main__":
    t_async = asyncio.run(waits_asyncio())
    t_pool = waits_threads(workers=50)
    print(f"{N} waits: asyncio {t_async:.2f}s, 50-thread pool {t_pool:.2f}s")
    assert t_pool > 5 * t_async      # the pool caps you at 50 waits in flight

    lag_blocking = asyncio.run(worst_loop_lag(blocking=True))
    lag_offloaded = asyncio.run(worst_loop_lag(blocking=False))
    print(f"worst timer lag: blocking {lag_blocking * 1000:.0f} ms, "
          f"offloaded {lag_offloaded * 1000:.0f} ms")
    assert lag_blocking > 0.15 and lag_offloaded < 0.1

    elapsed, replies, threads = asyncio.run(echo_server())
    print(f"100 connections x 50 ms each: {elapsed:.2f}s on {threads} thread(s)")
    assert replies[7] == b"HELLO 7\n"
    assert elapsed < 1.0 and threads == 1   # serially this would take 5 s
```

## Pitfalls

- **Blocking the loop.** `requests`, `time.sleep`, psycopg2, the sync Django ORM, big `json.loads`, or bcrypt inside a coroutine stall every connection on that worker. Symptoms: p99 latency spikes across unrelated endpoints and health-check timeouts. Detect with asyncio debug mode (`PYTHONASYNCIODEBUG=1` logs callbacks slower than `loop.slow_callback_duration`, 100 ms by default) or a loop-lag metric like the demo's.
- **Unbounded fan-out.** `gather` over 100,000 coroutines opens 100,000 sockets, exhausts file descriptors and overwhelms the upstream. Bound with a `Semaphore` or a worker queue.
- **Lost tasks.** The loop keeps only weak references to tasks, so a `create_task` result you don't store can be garbage-collected mid-flight. Keep references (or use `TaskGroup`).
- **Swallowed cancellation.** Catching `CancelledError` (or bare `except BaseException`) without re-raising breaks timeouts and graceful shutdown.
- **Races across `await`.** No two coroutines run at once, but state can change at every `await`. Read-modify-write sequences spanning an `await` still need a lock.
- **A new HTTP client per request.** It defeats connection pooling and adds a TLS handshake per call. Create one client per process and close it on shutdown.
- **Expecting ASGI to speed up the database.** The pool size is still the limit; see [Connection pooling]({{ '/backend/connection-pooling/' | relative_url }}).

## Interview questions

**What is the difference between blocking, non-blocking and asynchronous I/O?**
A blocking call parks the thread until the operation completes. A non-blocking call returns immediately with `EAGAIN` if it can't proceed. Asynchronous I/O lets you start operations and get notified on readiness (epoll, kqueue) or completion (io_uring, IOCP), so one thread can manage many operations.

**Why is epoll faster than select?**
`select` and `poll` pass and scan the full set of descriptors on every call, `O(n)` even when few are ready, and `select` is capped at about 1024 fds. `epoll` registers interest once in the kernel and returns only ready descriptors, so cost scales with activity rather than connection count.

**What happens when a coroutine hits `await`?**
If the awaited operation isn't complete, the coroutine suspends and control returns to the event loop, which runs other ready tasks and then waits in `epoll_wait`. When the socket becomes ready or the timer fires, the loop marks the task ready and resumes it where it left off.

**What was the C10K problem and how was it solved?**
Handling 10,000 concurrent connections on one server. Thread-per-connection and `select`-based servers ran out of memory and CPU on bookkeeping. Event-driven servers using `epoll`/`kqueue` with non-blocking sockets solved it, which is the model nginx, Node.js and asyncio use.

**When would you pick asyncio over threads?**
When concurrency is high and mostly waiting: many outbound HTTP calls, WebSockets, SSE, long polling, LLM streaming. Prefer threads or processes for CPU work, sync-only libraries, or moderate concurrency where simplicity wins.

**WSGI vs ASGI: does moving Django to ASGI make it faster?**
WSGI is a synchronous call per request; ASGI is an async interface that also supports WebSockets and lifespan events. A sync Django app under ASGI runs views in threads and pays switching overhead, so it isn't faster. Gains come from async views that overlap slow I/O.

**What happens if you call `requests.get` inside an async view?**
It blocks the event loop for the whole call, so every other request on that worker stalls. Use an async client like `httpx.AsyncClient`, or wrap the call in `asyncio.to_thread`.

**Level-triggered vs edge-triggered epoll?**
Level-triggered keeps notifying while data remains unread. Edge-triggered notifies once per state change, so the handler must drain the socket until `EAGAIN` or risk hanging. Edge-triggered reduces wakeups but is easier to get wrong.

## Further reading

- Dan Kegel, "The C10K problem".
- W. Richard Stevens, *UNIX Network Programming, Volume 1*, the chapter on I/O models and multiplexing.
- Linux man pages `epoll(7)` and `io_uring(7)`; FreeBSD `kqueue(2)`.
- Python documentation: `asyncio` (including "Developing with asyncio") and `selectors`.
- PEP 3333, the ASGI specification, and Django's "Asynchronous support" documentation.
