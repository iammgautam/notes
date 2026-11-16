---
title: Connection pooling
section: backend
order: 16
phase: "Phase 2 · Databases"
summary: Why connections are expensive, app pools vs PgBouncer, sizing with Little's law, transaction vs session pooling, timeouts, leaks, exhaustion and HTTP pools.
---

Opening a connection to a database or another service costs far more than the query you send over it, and the server can only hold so many. Pooling keeps a small set of open connections and lends them out, so a request pays for a checkout instead of a handshake, and the database sees a bounded, steady number of clients. Most "the database is slow" incidents in a scaled-out fleet are really pool problems: too many connections, connections held too long, or connections that never come back.

## Why connections are expensive

A new PostgreSQL connection costs:

- **Network setup.** A TCP handshake, usually a TLS handshake, and SCRAM authentication: several round trips, roughly 5 to 30 ms across a network versus well under a millisecond for a simple indexed query.
- **A server process.** Postgres forks one backend process per connection, a few MB baseline and often 10 MB or more once catalog and plan caches warm up. MySQL uses a thread per connection, cheaper but not free.
- **Contention.** Throughput peaks when active queries are around 2 to 4 times the CPU core count; beyond that, backends fight over CPU, locks and memory, and latency rises for everyone.
- **A hard cap.** `max_connections` (default 100) needs a restart to change, and raising it to thousands moves the failure from "connection refused" to "database thrashing".

The fleet multiplies this. Twenty pods with 4 Gunicorn workers and a few Celery workers each can open several hundred connections to a database that does its best work with a few dozen active ones.

## Connection pooling

A pool keeps open connections and lends them out:

1. **Checkout.** Take an idle connection, or open one if under `max_size`, or wait up to an acquire timeout.
2. **Validate.** Optionally check liveness on borrow (a cheap `SELECT 1`, "pre-ping") so a connection killed by a failover or firewall is not handed out.
3. **Use.** Run the queries or transaction.
4. **Return and reset.** Roll back any open transaction and clear session state (PgBouncer runs `DISCARD ALL` in session mode), then put it back.
5. **Retire.** Close connections idle too long or older than a maximum lifetime, so the pool shrinks after spikes and picks up DNS or failover changes.

### App-side pools vs PgBouncer

| Aspect | In-process pool | External pooler |
|---|---|---|
| Examples | SQLAlchemy `QueuePool`, HikariCP, psycopg_pool, Django 5.1+ `pool` option | PgBouncer, PgCat, Odyssey, RDS Proxy; ProxySQL for MySQL |
| Scope | One process | All clients of a database |
| Bounds total connections | No: pods × processes × pool size | Yes |
| Extra network hop | No | Yes, a fraction of a millisecond |
| Multiplexing across processes | No | Yes (transaction mode) |
| Breaks session features | No | In transaction mode, yes |
| Single point of failure | No | Must be run redundantly |

Use both in larger systems: a small in-process pool (or Django's persistent connections) to avoid reconnecting per request, and PgBouncer in transaction mode so hundreds of client connections share a few dozen server connections.

```text
  20 pods x 4 workers
 +--------+ +--------+       +--------+
 | pod 1  | | pod 2  |  ...  | pod 20 |
 +---+----+ +---+----+       +---+----+
     |          |                |
     +----------+-------+--------+
                        | 80 client connections
                        v
              +-------------------+
              | PgBouncer         |   pool_mode = transaction
              | default_pool = 20 |
              +---------+---------+
                        | 20 server connections
                        v
              +-------------------+
              | PostgreSQL        |   16 vCPU, max_connections = 100
              +-------------------+
```

Django specifics: `CONN_MAX_AGE` keeps one connection per worker thread open across requests (a persistent connection, not a pool), and `CONN_HEALTH_CHECKS = True` validates it before reuse. Django 5.1 added a real pool for PostgreSQL with psycopg 3 (`"OPTIONS": {"pool": True}` or a dict of `psycopg_pool` settings); leave `CONN_MAX_AGE` at 0 with it.

## Pool sizing math

Use **Little's law**: connections in use = arrival rate × time each connection is held.

- A service does 1,200 requests/s and each request holds a connection for 8 ms in total: 1,200 × 0.008 = **9.6 busy connections** on average.
- Bursts and tail latency need headroom, so provision about 2× the average: roughly 20 server connections across the whole fleet.
- If a slow query or lock wait pushes hold time to 80 ms, the same traffic needs 96 connections. Pool exhaustion is usually a latency problem, not a pool-size problem.

Then check the database side:

- **Useful concurrency** is roughly `cores × 2` plus a little for I/O waits. HikariCP's well-known starting point is `connections = (core_count × 2) + effective_spindle_count`. A 16-vCPU database: about 32 to 40 active connections.
- **Budget every client.** The sum of all pool maxima (web, workers, cron, migrations, analytics, admin) plus `superuser_reserved_connections` and replication slots must fit under `max_connections`.
- **Per-process size.** A synchronous Django worker serves one request at a time, so it needs exactly one connection; a pool of 10 per sync process is waste. A threaded or async process needs about as many connections as concurrent requests that hit the database.

> **Rule of thumb:** Size the database side from cores, the client side from Little's law, and fix slow queries before you raise a pool limit. More connections on a saturated database lowers total throughput.

## Transaction vs session pooling

PgBouncer's `pool_mode` decides when a server connection goes back to the pool:

| Feature | Session | Transaction | Statement |
|---|---|---|---|
| Server connection held for | Client's whole session | One transaction | One statement |
| Multiplexing | None, only reuse | High | Highest |
| Multi-statement transactions | Yes | Yes | No |
| `SET` (session settings) | Yes | No, use `SET LOCAL` | No |
| Session advisory locks | Yes | No, use `pg_advisory_xact_lock` | No |
| `LISTEN` / `NOTIFY` listener | Yes | No | No |
| Temp tables, `WITH HOLD` cursors | Yes | No | No |
| Prepared statements | Yes | Protocol-level ones, with `max_prepared_statements` (PgBouncer 1.21+) | Same |

Transaction mode is the default choice for web traffic. Things that break are silent: a `SET search_path` or `SET app.tenant_id` leaks to whichever client gets that server connection next, which in a multi-tenant system is a data-isolation bug. Set per-transaction state with `SET LOCAL` or `set_config(..., true)` inside the transaction. In Django, set `DISABLE_SERVER_SIDE_CURSORS = True`, because `.iterator()` otherwise uses cursors that outlive a transaction. Run migrations, `LISTEN` consumers and session-lock users through a direct or session-mode connection. More Postgres detail is in [connection pooling in PostgreSQL]({{ '/postgresql/connection-pooling/' | relative_url }}).

## Timeouts

| Timeout | Where | Purpose | Typical value |
|---|---|---|---|
| Acquire (checkout) | App pool (`pool_timeout`) | Fail fast instead of queueing forever | 1 to 5 s, below the request timeout |
| Connect | Driver (`connect_timeout`) | Detect a dead host | 2 to 5 s |
| `statement_timeout` | Postgres, per role | Kill runaway queries | 5 to 30 s for web roles |
| `idle_in_transaction_session_timeout` | Postgres | Kill forgotten open transactions | 30 to 60 s |
| `query_wait_timeout` | PgBouncer | Client waiting for a server connection | Default 120 s; lower it |
| Idle timeout | Pool | Shrink after spikes | Minutes |
| Max lifetime | Pool (`pool_recycle`, `server_lifetime`) | Rotate connections; stay under firewall and load balancer idle cutoffs | 30 to 60 min |

Order them so the innermost fails first: a statement timeout shorter than the request timeout, an acquire timeout shorter than both. Otherwise the client gives up while the database keeps working on an answer nobody will read.

## Leak detection

A leak is a connection checked out and never returned: an exception path that skips `release`, a generator holding a cursor, a transaction left open across a slow call. The pool shrinks a little at a time until requests start waiting.

- **Make returning automatic.** Context managers (`with pool.connection()`), framework-managed connections, `try/finally`.
- **Leak thresholds.** Record the stack at checkout and log it if the connection is out longer than a threshold. HikariCP has `leakDetectionThreshold`; the toy pool below does the same.
- **Server-side view.** `pg_stat_activity` rows in `idle in transaction` with an old `state_change`, grouped by `application_name`, point at the culprit service.
- **Pool metrics.** In-use connections that never drop back to baseline after traffic falls is the classic leak graph.

## Pool exhaustion symptoms

- Latency jumps while database CPU is low or normal: requests are waiting for a connection, not for the database.
- Errors such as SQLAlchemy's "QueuePool limit of size 5 overflow 10 reached, connection timed out", or HikariCP's "Connection is not available, request timed out".
- From Postgres directly: "FATAL: sorry, too many clients already" or "remaining connection slots are reserved".
- PgBouncer `SHOW POOLS` with `cl_waiting` above zero and a growing `maxwait`.

Causes, in rough order of frequency: slow queries or lock waits raising hold time, connections held across external calls, leaks, retry storms multiplying load, and only then a pool that is truly too small. Fix the hold time first.

## HTTP client pools

The same economics apply to outbound HTTP, Redis and gRPC. A new HTTPS connection costs a TCP and TLS handshake, often 50 ms or more across regions, so creating a client per request burns latency and file descriptors.

- **Reuse one client per process.** `requests.Session`, `httpx.Client` or `AsyncClient`, `aiohttp.ClientSession`, created once and shared.
- **Size per host.** requests' `HTTPAdapter(pool_maxsize=...)` defaults to 10 connections per host; with more threads, urllib3 logs "Connection pool is full, discarding connection" and you pay new handshakes. httpx defaults to 100 connections with 20 kept alive.
- **Bound and time out.** Set connect and read timeouts on every call and cap per-host connections so a slow dependency cannot absorb every socket; this is a bulkhead (see [fault tolerance patterns]({{ '/backend/fault-tolerance-patterns/' | relative_url }})).
- **Redis and gRPC.** redis-py clients share a `ConnectionPool`; gRPC multiplexes calls over one HTTP/2 channel, so reuse the channel.

## In practice

### PgBouncer (illustrative)

```text
[databases]
app = host=pg-primary port=5432 dbname=app

[pgbouncer]
listen_port = 6432
pool_mode = transaction
max_client_conn = 2000
default_pool_size = 20
reserve_pool_size = 5
query_wait_timeout = 10
server_lifetime = 3600
server_idle_timeout = 300
max_prepared_statements = 100
```

### Django and SQLAlchemy (illustrative)

```python
# settings.py: through PgBouncer in transaction mode
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": "pgbouncer",
        "PORT": 6432,
        "NAME": "app",
        "CONN_MAX_AGE": 300,
        "CONN_HEALTH_CHECKS": True,
        "DISABLE_SERVER_SIDE_CURSORS": True,
    },
}
# Timeouts live on the role, which works through any pooler:
#   ALTER ROLE web SET statement_timeout = '15s';

# Alternative without PgBouncer, Django 5.1+ and psycopg 3:
#   "CONN_MAX_AGE": 0,
#   "OPTIONS": {"pool": True},

# SQLAlchemy in a FastAPI or worker service
from sqlalchemy import create_engine

engine = create_engine(
    "postgresql+psycopg://app@pgbouncer:6432/app",
    pool_size=5, max_overflow=5, pool_timeout=3,
    pool_recycle=1800, pool_pre_ping=True,
)

# Outbound HTTP: one session per process, sized to the thread count
import requests
from requests.adapters import HTTPAdapter

http = requests.Session()
http.mount("https://", HTTPAdapter(pool_maxsize=32))
```

### Toy pool with a stress test (runnable)

A thread-safe pool with lazy creation, a hard size cap, acquire timeout, validation on borrow and leak detection. Forty threads run 1,000 queries over five connections.

```python
import itertools
import threading
import time
import traceback
from contextlib import contextmanager


class PoolTimeout(Exception):
    pass


class FakeConn:
    _ids = itertools.count(1)

    def __init__(self):
        time.sleep(0.003)               # TCP + TLS + auth + server fork
        self.id = next(self._ids)
        self.broken = False

    def ping(self):
        return not self.broken


class Pool:
    def __init__(self, connect, max_size, acquire_timeout=1.0):
        self.connect, self.max_size = connect, max_size
        self.acquire_timeout = acquire_timeout
        self._cond = threading.Condition()
        self._idle = []                 # LIFO: reuse the warmest connection
        self._open = 0                  # idle + in use + being created
        self._in_use = {}               # conn -> (checkout time, stack)

    def acquire(self):
        deadline = time.monotonic() + self.acquire_timeout
        while True:
            with self._cond:
                while not self._idle and self._open >= self.max_size:
                    left = deadline - time.monotonic()
                    if left <= 0 or not self._cond.wait(left):
                        raise PoolTimeout(f"pool of {self.max_size} exhausted")
                conn = self._idle.pop() if self._idle else None
                if conn is None:
                    self._open += 1     # reserve a slot, connect outside lock
            if conn is None:
                try:
                    conn = self.connect()
                except BaseException:
                    self._discard()
                    raise
            elif not conn.ping():       # validate on borrow (pre-ping)
                self._discard()
                continue
            with self._cond:
                stack = "".join(traceback.format_stack(limit=5))
                self._in_use[conn] = (time.monotonic(), stack)
            return conn

    def release(self, conn):
        with self._cond:
            del self._in_use[conn]
            if conn.broken:
                self._open -= 1
            else:
                self._idle.append(conn) # real pools also ROLLBACK/reset here
            self._cond.notify()

    def _discard(self):
        with self._cond:
            self._open -= 1
            self._cond.notify()

    @contextmanager
    def connection(self):
        conn = self.acquire()
        try:
            yield conn
        finally:
            self.release(conn)

    def leaks(self, older_than):
        """Stacks of connections checked out longer than older_than s."""
        now = time.monotonic()
        with self._cond:
            return [stack for since, stack in self._in_use.values()
                    if now - since > older_than]


if __name__ == "__main__":
    pool = Pool(FakeConn, max_size=5, acquire_timeout=5.0)
    lock, active, peak, done = threading.Lock(), [0], [0], [0]

    def worker():
        for _ in range(25):
            with pool.connection():
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(0.001)       # the "query"
                with lock:
                    active[0] -= 1
                    done[0] += 1

    start = time.perf_counter()
    threads = [threading.Thread(target=worker) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start
    assert done[0] == 1000 and peak[0] <= 5 and pool._open <= 5
    print(f"1000 queries, 40 threads, {pool._open} connections, "
          f"{elapsed:.2f}s (unpooled connects alone: ~3s)")

    # Exhaustion: hold every connection, the next caller times out fast
    pool.acquire_timeout = 0.05
    held = [pool.acquire() for _ in range(5)]
    t0 = time.monotonic()
    try:
        pool.acquire()
        raise AssertionError("expected PoolTimeout")
    except PoolTimeout:
        assert time.monotonic() - t0 < 0.5

    # Leak detection: the one connection never returned shows its stack
    for conn in held[1:]:
        pool.release(conn)
    time.sleep(0.02)
    leaked = pool.leaks(older_than=0.01)
    assert len(leaked) == 1 and "<module>" in leaked[0]
    pool.release(held[0])

    # Broken connections are discarded on borrow and replaced
    for conn in pool._idle:
        conn.broken = True
    with pool.connection() as conn:
        assert conn.ping()
    assert pool._open <= 5
    print("pool demo ok")
```

## Pitfalls

- **Raising `max_connections` to fix exhaustion.** It trades queueing in the app for thrashing in the database. Add a pooler and fix hold time.
- **Pool per process times many processes.** `pool_size=20` looks small until 30 pods × 4 processes make it 2,400. Do the fleet-wide arithmetic.
- **Session state in transaction mode.** `SET`, session advisory locks and `LISTEN` silently misbehave; tenant context set with `SET` leaks across clients.
- **Holding a connection across slow I/O.** An HTTP or LLM call inside a transaction holds a scarce connection for seconds. Do the slow call first, then a short transaction.
- **No max lifetime.** After a failover or DNS change, pools keep dead or stale connections; idle connections get cut by NAT or load balancers and fail on first use. Set `pool_recycle` or `maxLifetime` below those cutoffs and enable pre-ping.
- **Forked workers sharing sockets.** A pool created before Gunicorn or Celery forks shares sockets between children, causing corrupted protocol streams. Create pools after fork (SQLAlchemy: `engine.dispose(close=False)` in the child).

## Interview questions

**Why not open a new database connection per request?**
Handshakes and authentication cost milliseconds, Postgres forks a process per connection, and the server caps connections. Pooling turns that into a cheap checkout and bounds load on the database.

**How do you size a connection pool?**
Little's law: requests per second times seconds each request holds a connection, plus headroom. Cap the total across all clients near the database's useful concurrency, about twice its cores, and check the sum against `max_connections`.

**Session vs transaction pooling?**
Session mode pins a server connection for the client's session and keeps all features. Transaction mode returns it after each transaction, multiplexing many clients onto few connections, but breaks session state: `SET`, session advisory locks, `LISTEN`, temp tables and long-lived cursors.

**Users see timeouts, the database CPU is at 20%. What is happening?**
Likely pool exhaustion: requests wait for connections. Check pool wait metrics, `cl_waiting` in PgBouncer, and `pg_stat_activity` for `idle in transaction` sessions, lock waits or a slow query that raised hold time.

**How do you detect a connection leak?**
Track checkout time and caller stack, alert when a connection is out past a threshold, watch in-use connections that never return to baseline, and look for old `idle in transaction` sessions by `application_name`.

**Do HTTP clients need pooling too?**
Yes. Reuse one client per process for keep-alive connections, size per-host pools to concurrency, and set timeouts so one slow dependency cannot exhaust sockets.

## Further reading

- PgBouncer documentation, "Configuration" and "Features" (pool modes and supported features).
- HikariCP wiki, "About Pool Sizing".
- PostgreSQL documentation, "Connections and Authentication" (`max_connections`) and "Monitoring Database Activity" (`pg_stat_activity`).
- Django documentation, "Databases" (persistent connections, connection pools, transaction pooling).
- SQLAlchemy documentation, "Connection Pooling".
