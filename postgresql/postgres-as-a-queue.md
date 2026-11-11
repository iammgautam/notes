---
title: Postgres as a job queue
section: postgresql
order: 9
summary: Build a reliable job queue on Postgres with SKIP LOCKED, leases, retries, fan-out/join, idempotency and LISTEN/NOTIFY, and know when it stops scaling.
---

If your app already runs on Postgres, a table-backed queue gives you something Redis or a broker can't: jobs enqueued in the **same transaction** as the business write. Either the order and its "send receipt" job both commit or neither does. No dual-write problem, no outbox relay, one system to back up and monitor. With `SKIP LOCKED` it handles thousands of jobs per second on modest hardware, which covers most SaaS workloads.

## How it works

The core is a table and one dequeue query. Workers claim rows with `FOR UPDATE SKIP LOCKED`, so concurrent workers never block on or double-claim the same job.

```text
 enqueue (in app txn)       dequeue (worker)            finish
 INSERT jobs ... ---------> UPDATE ... SET status=     UPDATE status='done'
 status='pending'           'running', lease=now()+5m  or 'failed' + retry
                            WHERE id IN (SELECT ...
                            FOR UPDATE SKIP LOCKED)
```

Two designs for holding a job while processing:

1. **Hold the row lock** for the whole job inside one transaction. Simple, and a crashed worker releases the lock automatically. But it keeps a transaction open for the job's duration, holding back vacuum and pinning a connection. Fine for sub-second jobs.
2. **Lease** (visibility timeout): claim in a short transaction by setting `status = 'running'` and `locked_until`, commit, process outside any transaction, then mark done. A reaper or the dequeue query itself reclaims expired leases. This is the production default.

## In practice

### Schema

```sql
CREATE TABLE jobs (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id       bigint NOT NULL,
  queue           text NOT NULL,
  kind            text NOT NULL,
  payload         jsonb NOT NULL DEFAULT '{}',
  status          text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','running','done','failed','dead')),
  priority        smallint NOT NULL DEFAULT 0,
  run_at          timestamptz NOT NULL DEFAULT now(),
  attempts        int NOT NULL DEFAULT 0,
  max_attempts    int NOT NULL DEFAULT 5,
  locked_by       text,
  locked_until    timestamptz,
  idempotency_key text,
  parent_id       bigint REFERENCES jobs(id),
  last_error      text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz
) WITH (fillfactor = 80);

CREATE INDEX jobs_ready_idx ON jobs (queue, priority DESC, run_at)
  WHERE status = 'pending';
CREATE INDEX jobs_expired_lease_idx ON jobs (locked_until)
  WHERE status = 'running';
CREATE UNIQUE INDEX jobs_idem_key ON jobs (tenant_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

ALTER TABLE jobs SET (autovacuum_vacuum_scale_factor = 0.01,
                      autovacuum_vacuum_cost_limit = 2000);
```

Partial indexes keep the hot path proportional to backlog, not history.

### Dequeue with a lease

```sql
WITH next AS (
  SELECT id FROM jobs
  WHERE status = 'pending' AND queue = $1 AND run_at <= now()
  ORDER BY priority DESC, run_at
  LIMIT $2
  FOR UPDATE SKIP LOCKED
)
UPDATE jobs j
SET status = 'running',
    attempts = attempts + 1,
    locked_by = $3,
    locked_until = now() + interval '5 minutes'
FROM next
WHERE j.id = next.id
RETURNING j.id, j.kind, j.payload, j.attempts;
```

Commit immediately. Claiming a batch (`LIMIT 10`) amortizes round trips.

### Complete, fail, retry with backoff

```sql
-- success (guard with locked_by so a worker whose lease expired can't clobber)
UPDATE jobs SET status = 'done', finished_at = now(), locked_until = NULL
WHERE id = $1 AND locked_by = $2 AND status = 'running';

-- failure: exponential backoff with jitter, dead-letter after max_attempts
UPDATE jobs
SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'pending' END,
    run_at = now() + least(interval '1 hour',
             interval '1 second' * power(2, attempts) * (0.5 + random())),
    last_error = $3, locked_by = NULL, locked_until = NULL
WHERE id = $1 AND locked_by = $2;
```

### Reclaim expired leases

```sql
UPDATE jobs
SET status = 'pending', locked_by = NULL, locked_until = NULL
WHERE status = 'running' AND locked_until < now();
```

Long jobs should heartbeat by extending `locked_until`. If the heartbeat update returns 0 rows, the lease was lost and the worker should stop.

### Enqueue transactionally with idempotency

```python
from django.db import transaction

with transaction.atomic():
    order = Order.objects.create(tenant_id=tid, total_cents=4200)
    Job.objects.bulk_create(
        [Job(tenant_id=tid, queue="email", kind="send_receipt",
             payload={"order_id": order.id},
             idempotency_key=f"receipt:{order.id}")],
        ignore_conflicts=True,
    )
```

Delivery is **at least once**: a worker can finish the side effect and crash before marking done. Handlers must be idempotent: check-and-set on a domain row, unique constraints on effects, or pass an idempotency key to the external API (Stripe, email provider).

### Fan-out and join

Split a big task into children and run a finalizer when all finish. The parent job creates a group row and its children in one transaction, then completes:

```sql
CREATE TABLE job_groups (
  parent_id bigint PRIMARY KEY REFERENCES jobs(id),
  remaining int NOT NULL
);

-- fan-out, inside the parent's transaction ($2 = parent job id)
WITH kids AS (
  INSERT INTO jobs (tenant_id, queue, kind, payload, parent_id)
  SELECT $1, 'embed', 'embed_chunk', jsonb_build_object('chunk_id', c.id), $2
  FROM document_chunks c WHERE c.document_id = $3
  RETURNING 1
)
INSERT INTO job_groups (parent_id, remaining)
SELECT $2, count(*) FROM kids;
```

Each child decrements the counter in the same transaction that marks it done; the child that brings it to zero enqueues the finalizer:

```sql
-- in the child's completion transaction
UPDATE jobs SET status = 'done', finished_at = now()
WHERE id = $child AND locked_by = $worker AND status = 'running';
-- only if that updated 1 row:
UPDATE job_groups SET remaining = remaining - 1
WHERE parent_id = $parent
RETURNING remaining;     -- if 0: INSERT the finalize job in the same txn
```

The row lock on `job_groups` serializes concurrent completions, so exactly one child sees `0`. Guarding the decrement on the child's own transition to `done` makes a retried completion a no-op instead of a double decrement.

### Wake workers with LISTEN/NOTIFY

Polling every 1s is fine at low volume; for lower latency, notify on enqueue:

```sql
CREATE FUNCTION jobs_notify() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('jobs_' || NEW.queue, '');
  RETURN NULL;
END $$;

CREATE TRIGGER jobs_notify_trg AFTER INSERT ON jobs
FOR EACH ROW WHEN (NEW.status = 'pending') EXECUTE FUNCTION jobs_notify();
```

Workers `LISTEN jobs_email` and dequeue on wake-up, but still poll on a timeout because notifications are lost if no listener is connected. Notifications are delivered at commit and deduplicated within a transaction. `LISTEN` needs a dedicated session connection; it doesn't work through PgBouncer transaction pooling.

### Archive finished jobs

Keep the hot table small. Delete or move `done` rows in batches, or partition by `created_at` and drop old partitions:

```sql
DELETE FROM jobs
WHERE id IN (SELECT id FROM jobs
             WHERE status = 'done' AND finished_at < now() - interval '7 days'
             LIMIT 5000);
```

## When it stops scaling

| Symptom | Cause | Mitigation |
|---|---|---|
| Dequeue latency creeps up | Dead tuples in the ready index | Aggressive autovacuum, short txns, archive done rows |
| Bloat never shrinks | Long transaction holds xmin horizon | `idle_in_transaction_session_timeout`, lease design |
| CPU in `SKIP LOCKED` scans | Many workers racing for the same head rows | Batch claims, shard by queue or tenant |
| WAL volume high | Every state change is a write | Fewer state transitions, `fillfactor` for HOT updates |
| Connections exhausted | One connection per worker thread | PgBouncer, fewer pollers, LISTEN |

Rough ceiling: low thousands to around 10k jobs/s on a well-tuned primary, depending on payload size and transitions per job. Beyond that, or for fan-out pub/sub to many consumers, streaming replay, or cross-service event buses, use Kafka, SQS or RabbitMQ and keep Postgres as the outbox.

> **In production:** Track queue depth by status, oldest pending `run_at` age, attempts distribution, and `dead` count. Oldest-pending age is the metric that pages someone.

## Pitfalls

- **Processing inside the claim transaction** for slow jobs: holds vacuum back and pins a connection.
- **No lease expiry:** a crashed worker leaves jobs `running` forever.
- **Non-idempotent handlers** with at-least-once delivery: double emails, double charges.
- **One giant `jobs` table with years of history:** every index and vacuum pass pays for it.
- **Tenant starvation:** one tenant enqueues 1M jobs and everyone else waits. Add per-tenant fairness (round-robin by tenant, or per-tenant concurrency caps via advisory locks).

## Checklist

- Partial index on ready jobs, `SKIP LOCKED` dequeue, batch claims.
- Leases with heartbeat and reaper; guard completion with `locked_by`.
- Exponential backoff with jitter and a dead-letter state.
- Idempotency keys enforced by a unique index.
- Enqueue in the same transaction as the business write.
- Archive or partition finished jobs; tune autovacuum per table.

## Interview angle

**Q: Why build a queue on Postgres instead of Redis/Celery?**
Transactional enqueue with the business write, one datastore to operate, durable by default, queryable state. Good up to thousands of jobs per second.

**Q: How does `SKIP LOCKED` prevent double processing?**
Each worker locks the rows it selects; other workers' queries skip locked rows instead of waiting, so each row is claimed by exactly one transaction.

**Q: What happens if a worker dies mid-job?**
With leases, `locked_until` expires and the job is reclaimed and retried. That's why handlers must be idempotent.
