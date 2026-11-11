---
title: Connection pooling
section: postgresql
order: 15
summary: Why Postgres connections are expensive, how to size max_connections, how PgBouncer session and transaction modes differ, and what breaks in Django.
---

Every Postgres connection is a full operating system process. That design is simple and robust, but it means connection count is a scarce resource. A Django deployment with 20 pods, each running 4 Gunicorn workers with a few threads, plus Celery or task workers, can easily open 500+ connections to a database that performs best with a few dozen active ones. Pooling is how you decouple "clients that might query" from "backends actually running queries".

## How it works

### Cost of a connection

- **Process per connection.** `fork()` of the postmaster, then authentication (and TLS handshake if enabled). Typical setup cost is a few milliseconds; with TLS and SCRAM over a network, 5–30 ms.
- **Memory.** Each backend uses a few MB baseline, plus catalog and plan caches that grow with the number of tables and prepared statements touched (10–20 MB per backend is common in large schemas), plus `work_mem` per sort/hash node while executing.
- **Snapshot cost.** Every snapshot scans the list of active backends. PG 14 improved this a lot, but thousands of connections still add overhead and lock contention.
- **Context switching.** More active backends than CPU cores means they compete; throughput peaks around 2–4x core count active queries and degrades beyond.

`max_connections` (default 100) needs a restart to change. Setting it to 2,000 "just in case" is a common mistake: memory is reserved per slot, and the database falls over under load instead of queuing.

### Where pooling can happen

| Layer | Example | Pools across |
|---|---|---|
| In-process | Django `CONN_MAX_AGE`, Django 5.1 `pool` option (psycopg 3), SQLAlchemy pool | One process |
| External pooler | PgBouncer, PgCat, Odyssey, Supavisor, RDS Proxy | All app instances |

In-process pooling avoids reconnect cost but doesn't reduce total connections across many processes. An external pooler does.

## PgBouncer modes

| Mode | Server connection assigned for | Session features | Efficiency |
|---|---|---|---|
| `session` | Whole client session | All work | Low; same count as clients |
| `transaction` | One transaction | Many break (below) | High; the usual choice |
| `statement` | One statement | Multi-statement transactions forbidden | Highest; rarely used |

In **transaction mode**, after `COMMIT`/`ROLLBACK` the server connection goes back to the pool, and your next transaction may run on a different backend. Anything that relies on session state breaks:

| Feature | Problem in transaction mode | Workaround |
|---|---|---|
| `SET` / `SET search_path` | Leaks to other clients or disappears | `SET LOCAL` inside the transaction |
| Session advisory locks | Held on someone else's connection | `pg_advisory_xact_lock` |
| `LISTEN` | Notifications go to a random backend | Direct connection for listeners |
| Temp tables / `WITH HOLD` cursors | Gone or visible to others | Avoid, or use session pool |
| Server-side prepared statements | Prepared on a different backend | PgBouncer 1.21+ `max_prepared_statements`, or client-side binding |
| Server-side cursors | Lost between transactions | Disable or use inside one transaction |

Key PgBouncer settings:

```text
[pgbouncer]
pool_mode = transaction
max_client_conn = 5000          ; clients PgBouncer accepts
default_pool_size = 20          ; server conns per (db, user) pair
reserve_pool_size = 5
server_idle_timeout = 60
query_wait_timeout = 30         ; fail fast instead of queueing forever
max_prepared_statements = 200   ; 1.21+, transaction-mode prepared stmts
```

Sizing: total server connections across poolers should stay well under `max_connections`, leaving room for admin, migrations, replication and monitoring. A good start is `default_pool_size` around 2–4x CPU cores of the database, divided by the number of PgBouncer instances.

## In practice: Django

### Settings for PgBouncer in transaction mode

```python
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": "pgbouncer",
        "PORT": 6432,
        "NAME": "app",
        "CONN_MAX_AGE": 0,                       # or small; PgBouncer pools
        "CONN_HEALTH_CHECKS": True,
        "DISABLE_SERVER_SIDE_CURSORS": True,     # required for .iterator()
        "OPTIONS": {
            # psycopg 3: keep client-side binding (Django's default)
            "server_side_binding": False,
        },
    },
}
```

- **`DISABLE_SERVER_SIDE_CURSORS = True`.** `QuerySet.iterator()` uses a named cursor that spans fetches. Outside a transaction (autocommit), each fetch can land on a different backend and fail with `cursor "_django_curs_..." does not exist`.
- **Don't combine Django 5.1's native `pool` option with PgBouncer transaction mode** without thought; you get two pools. Use one or the other: native pool for a few long-lived processes connecting directly, PgBouncer for many processes.
- **`CONN_MAX_AGE`** with PgBouncer: keeping the client-to-PgBouncer connection open is cheap and fine, but leave health checks on.

### Tenant context in transaction mode

Schema-per-tenant (`django-tenants`) sets `search_path` per request. Under transaction pooling, a plain `SET search_path` can leak to the next client's transaction on that backend. Use `SET LOCAL` inside an explicit transaction or make sure the search path is set at the start of every transaction. The same applies to RLS session variables:

```sql
BEGIN;
SELECT set_config('app.tenant_id', '42', true);   -- true = transaction-local
-- queries
COMMIT;
```

### Migrations and admin tasks

Run migrations against Postgres directly (or a session-mode pool): they use advisory locks, `SET lock_timeout`, and sometimes long transactions.

### Workers

Background workers that hold a connection while idle (polling loops, `LISTEN`) should connect directly or use a small session-mode pool. Size them separately so a worker scale-up can't starve the web tier.

## Pitfalls

- **`too many connections for role` / `sorry, too many clients already`.** You exhausted `max_connections`. Add a pooler; don't just raise the limit.
- **Pool wait latency.** With PgBouncer, a saturated pool shows up as clients waiting (`SHOW POOLS;` column `cl_waiting`, and `maxwait`), not as database load. Usually caused by long transactions holding server connections, not by too small a pool.
- **Idle in transaction through the pooler.** A transaction left open pins a server connection. Set `idle_in_transaction_session_timeout` in Postgres and keep `atomic()` blocks free of network calls.
- **Prepared statement errors** (`prepared statement "_pg3_0" does not exist`). Use client-side binding or enable `max_prepared_statements`.
- **Leaked session state.** `SET statement_timeout` without `LOCAL` changes it for whoever gets that backend next.
- **One pooler as a single point of failure.** Run at least two, behind a load balancer or as a sidecar per app pod.

> **In production:** Watch `SHOW POOLS` (`cl_waiting`, `sv_active`, `sv_idle`) and `SHOW STATS` (`avg_wait_time`) in PgBouncer, and `count(*) GROUP BY state` from `pg_stat_activity` in Postgres. Many `idle in transaction` rows point at application code, not pool size.

## Checklist

- Keep `max_connections` modest (a few hundred at most) and pool in front.
- PgBouncer in transaction mode for the web tier; direct or session mode for migrations and listeners.
- `DISABLE_SERVER_SIDE_CURSORS = True` in Django behind transaction pooling.
- Use `SET LOCAL` / `set_config(..., true)` for per-request settings.
- Prefer transaction-scoped advisory locks.
- Monitor client wait time, not just database CPU.

## Interview angle

**Q: Why do you need a connection pooler for Postgres?**
Each connection is a process with real memory and setup cost, and throughput drops when far more backends are active than CPU cores. A pooler multiplexes many client connections onto a small set of server connections.

**Q: What breaks with PgBouncer in transaction mode?**
Anything relying on session state across transactions: `SET`, session advisory locks, `LISTEN`, temp tables, server-side cursors and (before 1.21) prepared statements.

**Q: How would you size the pool?**
Start near 2–4x the database's CPU cores for active connections in total, measure client wait time and database CPU, and fix long transactions before growing the pool.
