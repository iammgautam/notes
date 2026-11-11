---
title: "Replacing Celery and Redis with a Postgres task executor"
summary: "How I built background jobs on Postgres with SKIP LOCKED, leases, fan-out/join and idempotency, and when that stops being the right call."
tags: [postgresql, django, background-jobs]
---

Most Django teams reach for Celery and Redis the moment they need background work. I have done it myself: at my previous job I built Celery workflows and they did their job. At Gainfront I went the other way and built a PostgreSQL-native distributed task executor instead. It handles database-backed queuing, task isolation, fan-out/join workflows and idempotent execution, all without Celery or Redis.

This post covers why that was a reasonable call, how the core pieces work, and the point at which I would stop defending it.

## The problem

Gainfront runs Django services in a multi-tenant microservice architecture. Like most SaaS backends, a lot of work does not belong in the request/response cycle: anything slow, anything that talks to a third party, anything that fans out over many records. We needed asynchronous background processing that was reliable enough that nobody had to think about it.

> **TODO (author):** name one or two concrete workloads that motivated this (kinds of jobs, rough volume per day, typical runtime), without client details.

The obvious answer was "add Celery with a Redis broker." Before doing that, I wrote down what we actually needed from a queue.

## Constraints

- **Transactional enqueue.** When a request writes rows and schedules follow-up work, either both happen or neither does. With an external broker you get the classic dual-write bug: the transaction commits but the enqueue fails, or the job runs before the transaction commits and cannot find its rows.
- **Workflows, not just jobs.** Some work splits into many independent pieces and then needs a single step that runs once all of them finish. That is fan-out/join.
- **At-least-once is fine, duplicate effects are not.** Workers crash. Deploys kill processes. A task will sometimes run twice, so running it twice must be harmless.
- **Isolation.** One bad task should fail on its own. It should not poison a batch, block the queue, or leak state into the next task.
- **Operational surface.** Every extra stateful component is something to provision, monitor, back up and debug at 3am. Postgres was already there and already well understood.

> **TODO (author):** add any constraint I am missing here (for example infra or hosting limits, or team preference) if it genuinely drove the decision.

## Options I considered

| Option | Why it was attractive | Why I passed |
|---|---|---|
| Celery + Redis | Familiar, huge ecosystem, I had shipped it before | Dual-write problem, a second stateful service, chords/groups are hard to reason about when they fail |
| Celery + RabbitMQ | Stronger delivery guarantees than Redis | Same dual-write problem, even more infrastructure |
| A managed cloud queue | No servers to run | Still outside the database transaction, adds vendor coupling |
| Postgres as the queue | Enqueue in the same transaction, one system to operate, SQL to inspect state | Throughput ceiling, vacuum and connection pressure (covered below) |

The deciding factor was transactional enqueue. If the job row is inserted in the same transaction as the business data, the dual-write problem disappears instead of being managed.

## What I built

What follows is a simplified version to show the mechanics. It is not the production schema or code; names and columns are generic.

### The task table

```sql
CREATE TABLE task (
    id               bigserial PRIMARY KEY,
    tenant_id        bigint      NOT NULL,
    kind             text        NOT NULL,
    payload          jsonb       NOT NULL DEFAULT '{}',
    status           text        NOT NULL DEFAULT 'queued',
    -- queued | running | done | failed | dead
    priority         smallint    NOT NULL DEFAULT 100,
    run_after        timestamptz NOT NULL DEFAULT now(),
    attempts         int         NOT NULL DEFAULT 0,
    max_attempts     int         NOT NULL DEFAULT 5,
    locked_by        text,
    lease_expires_at timestamptz,
    parent_id        bigint REFERENCES task(id),
    pending_children int         NOT NULL DEFAULT 0,
    idempotency_key  text,
    last_error       text,
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- Only queued rows are ever scanned by the dequeue query.
CREATE INDEX task_dequeue_idx
    ON task (priority, run_after, id)
    WHERE status = 'queued';

CREATE UNIQUE INDEX task_idem_idx
    ON task (idempotency_key)
    WHERE idempotency_key IS NOT NULL;
```

The partial index matters. The table grows with history, but the dequeue path only touches the small set of rows that are actually waiting.

### Dequeue with SKIP LOCKED

The heart of any Postgres queue is `FOR UPDATE SKIP LOCKED`. Many workers run the same query at the same time; each one locks the rows it picks and silently skips rows another worker already holds. No two workers get the same task, and nobody waits on anybody.

```sql
WITH next AS (
    SELECT id
    FROM task
    WHERE status = 'queued'
      AND run_after <= now()
    ORDER BY priority, run_after, id
    LIMIT %(batch)s
    FOR UPDATE SKIP LOCKED
)
UPDATE task t
SET status           = 'running',
    locked_by        = %(worker)s,
    lease_expires_at = now() + interval '5 minutes',
    attempts         = t.attempts + 1
FROM next
WHERE t.id = next.id
RETURNING t.id, t.kind, t.payload, t.attempts, t.max_attempts;
```

The claim commits immediately. The worker does **not** hold a row lock while the task runs; it holds a *lease* instead.

### Leasing instead of long locks

Holding a transaction open for the full runtime of a task is a bad idea: it pins a connection, blocks vacuum from cleaning up dead tuples, and a hung worker holds its lock forever. A lease is just a timestamp. While a worker is alive it extends the lease periodically; if it dies, the lease runs out and a reaper puts the task back.

```sql
-- Heartbeat from a live worker
UPDATE task
SET lease_expires_at = now() + interval '5 minutes'
WHERE id = %(id)s AND locked_by = %(worker)s AND status = 'running';

-- Reaper: recover tasks whose worker vanished
UPDATE task
SET status    = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
    locked_by = NULL,
    run_after = now()
WHERE status = 'running'
  AND lease_expires_at < now();
```

The `locked_by` check on heartbeat and completion is what stops a slow-but-alive worker from overwriting a task that has already been re-leased to someone else.

### The worker loop

In Django the worker is a management command. A simplified version looks like this:

```python
import time
from django.db import connection, transaction

HANDLERS = {}  # kind -> callable(payload)

def run_worker(worker_id, batch=10):
    while True:
        tasks = claim_batch(worker_id, batch)
        if not tasks:
            time.sleep(1)  # polling; see trade-offs below
            continue
        for task in tasks:
            execute(task, worker_id)

def execute(task, worker_id):
    try:
        with transaction.atomic():
            HANDLERS[task.kind](task.payload)
            mark_done(task.id, worker_id)       # same transaction as the work
            notify_parent(task.id)              # fan-in, see below
    except Exception as exc:
        schedule_retry(task, worker_id, exc)    # backoff, or 'dead' after max_attempts
```

Each task runs in its own transaction, with its own error handling and its own row recording what went wrong. That is most of what "task isolation" means in practice: a failure is contained to one row, it is visible with a plain `SELECT`, and the worker moves on.

> **Gotcha:** wrapping the handler in `transaction.atomic()` is right for short, database-only work. For long tasks or ones that call external services, commit in smaller steps and rely on the lease and idempotency instead of one giant transaction.

### Fan-out and join

A parent task creates N children and records how many are outstanding. Each child, when it finishes, decrements the parent's counter in the same transaction that marks the child done. The child that brings the counter to zero enqueues the join step.

```sql
-- Inside the child's completion transaction
UPDATE task
SET pending_children = pending_children - 1
WHERE id = %(parent_id)s
RETURNING pending_children;
```

```python
def notify_parent(child_id):
    parent_id = get_parent_id(child_id)
    if parent_id is None:
        return
    remaining = decrement_pending(parent_id)
    if remaining == 0:
        enqueue("join", parent_id=parent_id,
                idempotency_key=f"join:{parent_id}")
```

The `UPDATE` takes a row lock on the parent, so concurrent children serialize on that one row and exactly one of them observes zero. The idempotency key on the join task is a second guard: even if something replays, the join is enqueued once.

> **TODO (author):** describe one real fan-out/join workflow shape (how many children typically, what the join step does) in generic terms.

### Idempotency

There are two layers.

1. **Enqueue idempotency.** Callers can pass a key; the unique partial index plus `ON CONFLICT DO NOTHING` makes "enqueue this if it is not already queued" a single statement.

    ```sql
    INSERT INTO task (tenant_id, kind, payload, idempotency_key)
    VALUES (%(tenant)s, %(kind)s, %(payload)s, %(key)s)
    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
    DO NOTHING;
    ```

2. **Execution idempotency.** Delivery is at-least-once, so handlers must tolerate replays. For database-only work the cleanest trick is to commit the effect and the `done` status in the same transaction: either both are there or neither is. For external calls, pass a stable key derived from the task (for example the task id) to the downstream system, or record the external result and check for it before calling again.

## Results

The CV-level outcome is straightforward: asynchronous background processing with database-backed queuing, task isolation, fan-out/join workflows and idempotent execution, running on the Postgres we already operated, with no Celery and no Redis in that path.

> **TODO (author):** add real numbers if you have them: tasks per day, p50/p95 queue latency, worker count, anything measured before/after. Do not estimate.

The less measurable win was debuggability. "Why didn't this run?" became a SQL query against a table, with the error, the attempt count and the parent right there.

## Trade-offs, and when this stops being the right call

A Postgres queue is a good default for a Django app, not a universal answer. These are the signals I would watch:

- **Throughput.** Every claim is an `UPDATE`, every completion another. Thousands of tasks per second on a busy primary will compete with your real workload. If the queue becomes a top entry in `pg_stat_statements`, it is time to look at a dedicated broker.
- **Bloat and vacuum.** Queue tables churn. Keep finished rows out of the hot table (archive or delete on a schedule), keep autovacuum aggressive on it, and keep transactions short so vacuum can actually reclaim space.
- **Connections.** Each worker holds a database connection. Many workers across many services can exhaust `max_connections`; a pooler helps, but it is a real cost.
- **Latency.** Polling with a one-second sleep means up to a second of idle latency. `LISTEN/NOTIFY` can wake workers sooner, but treat notifications as hints and still poll.
- **Cross-service fan-out.** If many services need to consume the same events, you want a log or a pub/sub system, not a task table.
- **Rich scheduling.** Cron-like schedules, rate limits per downstream and priorities across tenants are all buildable, but each one is code you now own.

## What I would do differently

- **Design the retention policy on day one.** Deciding later how long `done` rows live is harder than deciding up front.
- **Ship observability with the first version.** Queue depth by kind, oldest queued age, dead-task count and lease expirations are four queries. They should be on a dashboard before the first incident, not after.
- **Make idempotency a handler contract, not a hope.** Document it, and test handlers by running them twice.

> **TODO (author):** add one honest lesson from operating this in production (something that surprised you, or a design choice you later changed).

The broader lesson: before adding infrastructure, check whether the database you already trust can do the job. Postgres is a better queue than its reputation suggests, as long as you know where its limits are.
