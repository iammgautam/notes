---
title: Design a distributed job scheduler
section: hld
order: 19
summary: Run one-off, delayed, cron and fan-out/join jobs reliably with leases, SKIP LOCKED, retries and idempotent handlers.
---

## Requirements

**Functional**
- Submit jobs: run now, run at time T, or on a cron schedule.
- Priorities and per-queue/per-tenant concurrency limits.
- Retries with backoff, max attempts, dead-letter state.
- Workflows: fan-out (N child jobs) and join (continue when all finish).
- Query status, cancel, and re-run.

**Non-functional**
- At-least-once execution; a job is never lost after submit returns.
- A crashed worker's job is picked up again within ~1 min.
- Scheduling delay p99 < 1 s for due jobs.
- Scales to ~1K–10K jobs/s; tenants isolated from each other's floods.

## Estimates

- 50 M jobs/day ≈ 600/s avg, ~5K/s peak.
- Row ~1 KB → 50 GB/day if kept; move finished jobs to an archive (partitioned by day) and drop old partitions.
- A Postgres-backed queue handles this range; beyond ~10K/s or for long retention, use a dedicated broker.

## API

```http
POST /v1/jobs
{"queue": "embeddings", "type": "embed_doc", "payload": {"doc_id": 9},
 "run_at": "2026-11-12T03:00:00Z", "priority": 5, "max_attempts": 8,
 "dedupe_key": "embed:doc:9"}
-> 201 {"job_id": "j_123"}

POST /v1/schedules   {"cron": "0 3 * * *", "tz": "Asia/Kolkata", "job": {...} }
GET  /v1/jobs/{id}   POST /v1/jobs/{id}/cancel
```

## Data model

```sql
CREATE TABLE jobs (
  id            bigserial PRIMARY KEY,
  tenant_id     bigint NOT NULL,
  queue         text NOT NULL,
  type          text NOT NULL,
  payload       jsonb NOT NULL,
  status        text NOT NULL DEFAULT 'queued', -- queued|running|done|failed|cancelled
  priority      int  NOT NULL DEFAULT 5,
  run_at        timestamptz NOT NULL DEFAULT now(),
  attempts      int  NOT NULL DEFAULT 0,
  max_attempts  int  NOT NULL DEFAULT 8,
  locked_by     text,
  locked_until  timestamptz,
  parent_id     bigint,
  dedupe_key    text,
  last_error    text
);
CREATE INDEX jobs_ready ON jobs (queue, priority, run_at) WHERE status = 'queued';
CREATE UNIQUE INDEX jobs_dedupe ON jobs (dedupe_key)
  WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running');

CREATE TABLE schedules (
  id bigserial PRIMARY KEY, cron text, tz text,
  job_template jsonb, next_run_at timestamptz
);
CREATE TABLE job_groups (  -- for fan-out/join
  id bigserial PRIMARY KEY, pending int, on_complete jsonb
);
```

## High-level design

```text
 producers --submit--> API --insert--> Postgres (jobs, schedules)
                                          ^    ^
           cron ticker (leader) ----------+    |  claim (SKIP LOCKED)
           reaper (expired leases) -------+    |  heartbeat / complete
                                               |
                         worker pool A  worker pool B  ...
                         (per queue, autoscaled on queue depth/age)
```

Optional: `LISTEN/NOTIFY` on insert to wake idle workers instead of tight polling.

## Deep dives

### Claiming with SKIP LOCKED

```sql
UPDATE jobs SET status = 'running', attempts = attempts + 1,
       locked_by = $worker, locked_until = now() + interval '60 seconds'
WHERE id IN (
  SELECT id FROM jobs
  WHERE queue = $q AND status = 'queued' AND run_at <= now()
  ORDER BY priority, run_at
  LIMIT $batch
  FOR UPDATE SKIP LOCKED
)
RETURNING id, type, payload;
```

- `SKIP LOCKED` lets many workers claim concurrently without blocking on each other's rows.
- Claim in batches to cut round trips. Keep the claim transaction short; do the work outside it.

### Leasing and crash recovery

- `locked_until` is a lease. Long jobs heartbeat (`UPDATE ... SET locked_until = now() + 60s WHERE id = ? AND locked_by = ?`).
- A reaper requeues `running` jobs whose lease expired.
- A worker that lost its lease must not commit results: check `locked_by` on completion (fencing).

### Retries

- On failure: `status='queued', run_at = now() + backoff(attempts)` with jitter; after `max_attempts` → `failed` (dead letter) and alert.
- Distinguish retryable (timeout, 429, deadlock) from permanent (validation) errors.

### Idempotency

- At-least-once means handlers can run twice (lease expiry, crash after side effect).
- Handlers must be idempotent: upserts, state checks (`WHERE status = 'pending'`), idempotency keys to external APIs.
- `dedupe_key` stops duplicate enqueues of the same logical job.
- Enqueue jobs in the **same transaction** as the business write (a big advantage of a Postgres queue; no outbox needed).

### Fan-out / join

- Parent creates a `job_group` with `pending = N` and inserts N child jobs in one transaction.
- Each child on success: `UPDATE job_groups SET pending = pending - 1 WHERE id = ? RETURNING pending`. The child that sees `0` enqueues the continuation.
- Make the decrement idempotent (record child completion with a unique row first) so retries do not double-decrement.

### Cron

- One ticker (leader via `pg_try_advisory_lock` or a lease row) scans `schedules WHERE next_run_at <= now()`.
- For each: insert job with `dedupe_key = schedule_id:next_run_at`, compute next run in the schedule's time zone, update `next_run_at`, all in one transaction.
- Missed runs after downtime: policy per schedule (run once, run all, skip).

### Fairness and limits

- Per-tenant concurrency: count running jobs per tenant in the claim query, or use separate queues/partitions for big tenants.
- Weighted round robin across queues so one flood cannot starve others.

## Bottlenecks and trade-offs

- Postgres queue: transactional enqueue, simple ops, one source of truth. Limits: write amplification, dead tuples (tune autovacuum, keep the hot table small), ~10K jobs/s ceiling.
- Polling interval vs latency vs DB load: use `LISTEN/NOTIFY` plus a slow poll fallback.
- Dedicated broker (SQS/RabbitMQ/Kafka): higher throughput, but needs an outbox for transactional enqueue and a separate store for status.
- Exactly-once is not achievable end to end; at-least-once + idempotent handlers is the design.

## Likely follow-ups

- How do you prevent a job running twice? You do not fully; leases + fencing + idempotent handlers.
- Long-running jobs (hours)? Heartbeats, checkpoints, or split into chunks.
- Priority inversion? Separate worker pools per priority.
- How would you migrate to Kafka/SQS? Keep the `jobs` table as status store, publish via outbox, consumers update status.
