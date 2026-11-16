---
title: Design a distributed job scheduler
section: backend
order: 84
phase: "Phase 10 · System design case studies"
summary: "Cron and one-off jobs at scale: time-bucketed storage, leased claims with SKIP LOCKED, retries and DLQ, tenant fairness, DAGs and exactly-once triggering."
---

Every backend ends up with a scheduler: send the reminder in 24 hours, rebuild rollups at 02:00, retry the webhook in 5 minutes, run step B after step A. A single cron box or a Celery beat process works until it is the single point of failure, fires the same job twice after a failover, or drowns at midnight when every tenant's report runs at once. This case study designs the platform version: durable, horizontally scalable, fair across tenants, and honest about at-least-once execution.

## Requirements

### Clarifying questions

- What is a job: a function in our worker fleet, an HTTP callback, or a container? Assume registered handlers in worker pools, plus HTTP callbacks for external targets.
- Precision? Seconds, not milliseconds.
- Recurring and one-off? Both, with cron expressions and time zones.
- Multi-tenant? Yes. One tenant must not starve others.
- Workflows? Simple DAGs: run C after A and B succeed.

### Functional

- Create, update, pause, resume, cancel and trigger-now for one-off and cron jobs.
- Execute each due run on a worker, with timeouts, retries with backoff and a dead letter queue.
- Run history and status per job; priorities; per-tenant concurrency limits.
- DAG workflows with dependency tracking.

### Non-functional

- 100M one-off runs/day plus 1M cron definitions.
- Trigger latency: p99 under 2 s after the scheduled time.
- No lost jobs: an accepted job is durable. Execution is at-least-once; triggering is exactly-once (one run per scheduled time).
- 99.99% availability for scheduling; workers can be degraded without losing work.
- Survive top-of-hour storms: many crons share `0 * * * *` and `0 0 * * *`.

### Out of scope

Job code packaging and sandboxing, long-running streaming jobs, and general-purpose workflow engines with human steps.

## Capacity estimates

```text
One-off       100M/day / 86,400 s        ≈ 1,160 runs/s avg
Cron          1M defs, avg hourly         ≈ 280 runs/s avg
              but ~30% at :00 → ~300k due in the same second
Total         ≈ 1,500 runs/s avg; storm peaks 100x for seconds
Workers       avg run 2 s → 1,500 x 2 = 3,000 busy slots
              (x2 headroom → ~6,000 slots)
Storage       job row 1 KB (payload ≤ 64 KB inline, larger in blob store)
              run row 0.5 KB; 100M x 1.5 KB ≈ 150 GB/day
              30-day run history ≈ 4.5 TB → partition by day, drop old
Claims        1,500/s avg in batches of 50 ≈ 30 claim queries/s
              (storm: 300k / 50 = 6,000 claim queries over a few seconds)
```

Average load is small. The design problem is the storm and the correctness of claims under concurrency, not raw throughput.

## API design

```http
POST /v1/jobs
Idempotency-Key: tenant9-nightly-rollup
Content-Type: application/json

{"tenant_id": "t9", "name": "nightly-rollup", "handler": "reports.rollup",
 "payload": {"report": "daily_sales"},
 "schedule": {"type": "cron", "expr": "0 2 * * *", "timezone": "Asia/Kolkata"},
 "priority": 5, "max_attempts": 5, "timeout_s": 600,
 "misfire_policy": "run_once", "concurrency_policy": "forbid"}
```

```json
{"job_id": "job_8f2", "status": "active",
 "next_run_at": "2026-11-17T02:00:00+05:30"}
```

A one-off job uses `{"type": "once", "run_at": "2026-11-16T18:30:00Z"}`.

| Endpoint | Purpose |
|---|---|
| `GET /v1/jobs/{id}` | Definition and next run |
| `GET /v1/jobs/{id}/runs?cursor=` | Run history, keyset paginated |
| `POST /v1/jobs/{id}:pause` / `:resume` / `:cancel` | Lifecycle |
| `POST /v1/jobs/{id}:trigger` | Run now, outside the schedule |
| `POST /v1/workflows` | DAG of tasks with `depends_on` edges |
| `POST /v1/dlq/{run_id}:redrive` | Retry a dead run |

The worker protocol is internal: `claim(pool, max, lease_s)` returns runs with a lease token; `heartbeat(run_id, token)`, `complete(run_id, token, result)` and `fail(run_id, token, error, retryable)`.

## Data model

PostgreSQL to start. Claims need row locks and `SKIP LOCKED`, triggers need unique constraints, and DAG updates need transactions. At 1,500 runs/s this is comfortable on one primary; beyond that, shard by `hash(job_id)` (below). See [Postgres as a queue]({{ '/postgresql/postgres-as-a-queue/' | relative_url }}).

```sql
-- Illustrative schema (abridged)
CREATE TABLE jobs (
  job_id        bigint PRIMARY KEY,
  tenant_id     bigint NOT NULL,
  kind          text   NOT NULL,              -- once | cron
  cron_expr     text,
  timezone      text,
  handler       text   NOT NULL,
  payload       jsonb  NOT NULL,
  priority      int    NOT NULL DEFAULT 0,
  max_attempts  int    NOT NULL DEFAULT 5,
  timeout_s     int    NOT NULL DEFAULT 300,
  status        text   NOT NULL,              -- active | paused | cancelled
  next_run_at   timestamptz,
  shard         int    NOT NULL               -- hash(job_id) % 1024
);
CREATE INDEX ON jobs (shard, next_run_at) WHERE status = 'active';

CREATE TABLE runs (
  run_id        bigint      NOT NULL,
  job_id        bigint      NOT NULL,
  tenant_id     bigint      NOT NULL,
  scheduled_for timestamptz NOT NULL,
  available_at  timestamptz NOT NULL,         -- moves forward on retry
  priority      int         NOT NULL,
  status        text        NOT NULL,         -- pending | running | succeeded | dead
  attempt       int         NOT NULL DEFAULT 0,
  lease_owner   text,
  lease_until   timestamptz,
  last_error    text,
  PRIMARY KEY (run_id, scheduled_for),
  UNIQUE (job_id, scheduled_for)              -- exactly-once triggering
) PARTITION BY RANGE (scheduled_for);         -- daily partitions

CREATE INDEX ON runs (priority DESC, available_at) WHERE status = 'pending';
CREATE INDEX ON runs (lease_until) WHERE status = 'running';
```

Workflow tables add `task_runs (workflow_run_id, task, run_id, deps_remaining)` and `edges (workflow_id, from_task, to_task)`. The DLQ is runs with `status = 'dead'` plus an index, not a separate system.

## High-level design

```text
 API ──► jobs table ◄── cron materializer (per shard owner)
             │                 │ INSERT next run ON CONFLICT DO NOTHING
             ▼                 ▼
         runs table (time-partitioned, partial index on pending)
             │                                   ▲
   claim: FOR UPDATE SKIP LOCKED, lease          │ reaper: expired
             │                                   │ leases → retry/DLQ
             ▼                                   │
     worker pools (per handler / priority) ──────┘
             │ heartbeat, complete, fail(token)
             ▼
     side effects (DBs, HTTP targets) with run_id as idempotency key

 coordinator (etcd/ZooKeeper leases): shard → scheduler node
```

Separate four responsibilities so each scales and fails independently:

1. **Storage** of definitions and runs.
2. **Triggering:** turning "a cron is due" into exactly one run row. Done by the shard owner's cron materializer.
3. **Dispatch:** handing ready runs to workers. Workers pull with leased claims; at higher scale a dispatcher pushes ready runs into per-pool queues.
4. **Execution:** workers run handlers, heartbeat, and report results with their lease token.

One-off jobs skip step 2: the API inserts the run row directly with `scheduled_for = run_at`.

## Deep dives

### Cron and one-off jobs

Cron semantics are where schedulers quietly go wrong:

- **Time zones and DST.** Store the zone (`Asia/Kolkata`, `America/New_York`), compute the next fire time in local time, then convert to UTC. On spring-forward a 02:30 run does not exist; on fall-back it exists twice. Pick a documented policy (run at the next valid instant; run the ambiguous time once).
- **Misfires.** After downtime, a job may have missed several fire times. Policies: run once now, run all missed, or skip to the next. Quartz calls these misfire instructions; Kubernetes CronJob bounds catch-up with `startingDeadlineSeconds`.
- **Overlap.** If the 02:00 run is still going at 03:00: allow, forbid (skip) or replace (cancel the old one). Kubernetes CronJob exposes the same three as `concurrencyPolicy`.
- **Storms.** Thousands of `0 * * * *` jobs fire together. Spread them with a deterministic per-job offset, as Jenkins does with its `H` syntax: `H * * * *` picks a stable minute from a hash of the job name.

### Time-bucketed storage

A single B-tree on `next_run_at` works on one database. At larger scale, and on stores without ordered global indexes (Cassandra, DynamoDB), partition runs by time bucket: partition key `(shard, minute_bucket)`, clustering key `(run_at, run_id)`. A shard owner reads exactly one partition per minute, in order, so finding due work is a single-partition read regardless of total backlog. Jobs far in the future sit in cold buckets nobody touches until their minute arrives.

Two tiers keep precision high: the durable store holds everything, and each shard owner loads the next few minutes of due runs into an in-memory timer (a heap or hierarchical timing wheel, the structure Kafka uses for delayed operations) to fire within milliseconds. If the owner crashes, nothing is lost; the next owner reloads from the store.

In PostgreSQL, daily range partitions plus the partial index on pending runs give the same effect: the index stays small because only pending rows are in it, and old partitions are dropped instead of deleted.

### Leasing and SKIP LOCKED claims

Workers claim a batch atomically. `SKIP LOCKED` lets concurrent workers grab different rows instead of queueing on the same locks:

```sql
WITH picked AS (
  SELECT run_id, scheduled_for FROM runs
  WHERE status = 'pending' AND available_at <= now()
  ORDER BY priority DESC, available_at
  LIMIT 50
  FOR UPDATE SKIP LOCKED
)
UPDATE runs r
SET status = 'running', attempt = r.attempt + 1, lease_owner = $1,
    lease_until = now() + make_interval(secs => $2)
FROM picked
WHERE r.run_id = picked.run_id AND r.scheduled_for = picked.scheduled_for
RETURNING r.run_id, r.job_id, r.attempt;
```

The returned `attempt` is the **lease token**. Every later call is conditional on it:

```sql
UPDATE runs SET status = 'succeeded', lease_owner = NULL
WHERE run_id = $1 AND attempt = $2 AND status = 'running';
-- zero rows: the lease expired and someone else owns this run now
```

Long jobs heartbeat (`SET lease_until = now() + ...` with the same condition) at a third of the lease length. A reaper moves runs with `lease_until < now()` back to `pending` with backoff, or to `dead` after `max_attempts`. Always use the database clock (`now()`), never worker clocks. The token check is fencing: a worker paused by GC for two minutes cannot overwrite the result of the worker that took over. See [Distributed locking]({{ '/backend/distributed-locking/' | relative_url }}).

### At-least-once with idempotent jobs

Exactly-once execution is impossible in general: a worker can perform the side effect and crash before recording success, and the run will execute again. So the contract is at-least-once, and handlers must be idempotent:

- Pass `run_id` (or `job_id + scheduled_for`) to the handler as its idempotency key, and forward it to downstream APIs that accept one.
- Prefer upserts and conditional updates over blind inserts and increments.
- When the side effect is in the scheduler's own database, mark the run succeeded in the same transaction as the effect; that path becomes effectively exactly-once.
- For external effects, record "effect done" with the key before acknowledging, and check it on re-run.

See [Delivery semantics]({{ '/backend/delivery-semantics/' | relative_url }}) and [Idempotency]({{ '/backend/idempotency/' | relative_url }}).

### Retries, backoff and the DLQ

- Classify failures: retryable (timeouts, 5xx, deadlocks) versus permanent (validation errors, missing records). Permanent failures go straight to `dead`.
- Backoff with full jitter: `delay = random(0, min(cap, base × 2^attempt))`, so a downstream outage does not produce synchronized retry waves. See [Retries and backoff]({{ '/backend/retries-and-backoff/' | relative_url }}).
- A run's timeout is its lease. A hung handler loses its lease and is retried, so set timeouts per job type.
- After `max_attempts`, mark `dead` with the last error. The DLQ is a query over dead runs with tooling to inspect, fix, and redrive (reset to `pending`, attempt counter optionally reset). Alert on DLQ inflow rate, not just size. See [Ordering and dead letter queues]({{ '/backend/ordering-and-dead-letter-queues/' | relative_url }}).
- Poison runs that crash the worker process itself count as attempts too (the lease expires), so they also reach the DLQ instead of looping forever.

### Priorities and fairness across tenants

Strict priority starves low-priority work, and FIFO lets one tenant that enqueues a million runs delay everyone else for hours.

| Mechanism | What it does |
|---|---|
| Per-tenant concurrency cap | At most N running per tenant; excess waits |
| Round-robin across tenants | Each claim takes the oldest due run per tenant in turn |
| Weighted fair queuing | Tenants or tiers get shares (enterprise 4, free 1) |
| Separate pools per priority class | Interactive jobs never queue behind batch jobs |
| Aging | A waiting run's effective priority rises over time |

In SQL, fairness usually means claiming per tenant: pick tenants with due work and spare capacity (from a `tenant_running` counter table), then claim a few runs for each. The demo below shows round-robin claiming, leases, fencing, backoff and the DLQ together:

```python
import random
from collections import deque
from dataclasses import dataclass


@dataclass
class Run:
    run_id: str
    tenant: str
    available_at: float
    priority: int = 0
    attempt: int = 0
    status: str = "pending"      # pending | running | succeeded | dead
    lease_until: float = 0.0


class Scheduler:
    def __init__(self, max_attempts=3, base=1.0, cap=60.0, lease=30.0, seed=7):
        self.runs, self.dlq = {}, []
        self.max_attempts, self.base, self.cap, self.lease = max_attempts, base, cap, lease
        self.rng = random.Random(seed)
        self.tenants = deque()          # round-robin order

    def submit(self, run_id, tenant, at, priority=0):
        if run_id in self.runs:         # unique run id: exactly-once trigger
            return False
        self.runs[run_id] = Run(run_id, tenant, at, priority)
        if tenant not in self.tenants:
            self.tenants.append(tenant)
        return True

    def _retry_or_dead(self, r, now, retryable=True):
        if not retryable or r.attempt >= self.max_attempts:
            r.status = "dead"
            self.dlq.append(r.run_id)
        else:
            delay = self.rng.uniform(0, min(self.cap, self.base * 2 ** r.attempt))
            r.status, r.available_at = "pending", now + delay

    def claim(self, now, n):
        for r in self.runs.values():    # reaper: expired leases
            if r.status == "running" and r.lease_until <= now:
                self._retry_or_dead(r, now)
        # O(runs) scan for clarity; the real store uses the partial index
        due = {t: sorted((r for r in self.runs.values()
                          if r.tenant == t and r.status == "pending"
                          and r.available_at <= now),
                         key=lambda r: (-r.priority, r.available_at))
               for t in self.tenants}
        out = []
        while len(out) < n and any(due.values()):
            t = self.tenants[0]
            self.tenants.rotate(-1)
            if due[t]:
                r = due[t].pop(0)
                r.status, r.lease_until = "running", now + self.lease
                r.attempt += 1
                out.append((r.run_id, r.attempt))   # attempt = lease token
        return out

    def complete(self, run_id, token):
        r = self.runs[run_id]
        if r.status != "running" or r.attempt != token:
            return False                # fenced: lease was lost
        r.status = "succeeded"
        return True

    def fail(self, run_id, token, now, retryable=True):
        r = self.runs[run_id]
        if r.status != "running" or r.attempt != token:
            return False
        self._retry_or_dead(r, now, retryable)
        return True


if __name__ == "__main__":
    s = Scheduler()
    for i in range(100):
        s.submit(f"a{i}", "tenant_a", at=0)
    s.submit("b0", "tenant_b", at=0)
    s.submit("b1", "tenant_b", at=0, priority=9)
    assert not s.submit("b0", "tenant_b", at=0)          # duplicate trigger

    batch = s.claim(now=1, n=4)                            # fairness
    tenants = [s.runs[rid].tenant for rid, _ in batch]
    assert tenants.count("tenant_b") == 2, tenants
    assert [rid for rid, _ in batch if rid.startswith("b")] == ["b1", "b0"]

    rid, token = batch[0]                                  # lease expiry + fencing
    s.claim(now=1 + s.lease, n=0)                          # reaper runs
    assert s.runs[rid].status == "pending"
    later = s.runs[rid].available_at
    assert 1 + s.lease <= later <= 1 + s.lease + s.base * 2
    again = [t for r, t in s.claim(now=later, n=200) if r == rid]
    assert again == [2]
    assert not s.complete(rid, token)                      # stale worker loses
    assert s.complete(rid, 2)

    d = Scheduler()                                        # retries then DLQ
    d.submit("p", "tenant_c", at=0)
    now = 0.0
    for attempt in range(1, d.max_attempts + 1):
        assert d.claim(now=now, n=10) == [("p", attempt)]
        assert d.fail("p", attempt, now)
        now += d.cap + 1
    assert d.runs["p"].status == "dead" and d.dlq == ["p"]

    d.submit("v", "tenant_c", at=now)                      # permanent error
    assert d.claim(now=now, n=10) == [("v", 1)]
    d.fail("v", 1, now, retryable=False)
    assert d.runs["v"].status == "dead" and d.dlq == ["p", "v"]
    print("scheduler ok")
```

### DAG dependencies

A workflow is a DAG of tasks. On submit, validate it is acyclic (Kahn's topological sort) and create a `task_runs` row per task with `deps_remaining` set to its in-degree. Tasks with zero dependencies become pending runs immediately.

When a task succeeds, decrement its children in the same transaction that records success:

```sql
UPDATE task_runs SET deps_remaining = deps_remaining - 1
WHERE workflow_run_id = $1 AND task = ANY($2)
RETURNING task, deps_remaining;
-- children that reach 0 get a pending run inserted in this transaction
```

The atomic decrement handles fan-in races: if A and B finish at the same moment, only one of them sees C reach zero, so C is enqueued once. On permanent failure, mark downstream tasks `upstream_failed` (Airflow's trigger rules generalize this: run on all success, any failure, all done). Retries apply per task, so a flaky step does not restart the whole workflow.

### Exactly-once triggering

Triggering means creating exactly one run per `(job, scheduled time)`. Execution is still at-least-once, but you must never create two runs for the same 02:00 slot. The cron materializer does both writes in one transaction:

```sql
-- $1 run_id, $2 job_id, $3 tenant_id, $4 fire_at, $5 priority, $6 next_fire_at
BEGIN;
INSERT INTO runs (run_id, job_id, tenant_id, scheduled_for, available_at,
                  priority, status)
VALUES ($1, $2, $3, $4, $4, $5, 'pending')
ON CONFLICT (job_id, scheduled_for) DO NOTHING;

UPDATE jobs SET next_run_at = $6
WHERE job_id = $2 AND next_run_at = $4;              -- compare-and-set
COMMIT;
```

Even if two scheduler nodes both believe they own the shard (a GC pause, a network partition), the unique constraint guarantees one run, and the compare-and-set guarantees `next_run_at` advances once. Leadership improves efficiency; the constraint provides correctness.

### Sharding the scheduler

Jobs hash into a fixed number of logical shards (say 1,024) that is much larger than the node count. A coordinator (etcd or ZooKeeper leases, or a leader that writes an assignment table) maps shards to scheduler nodes. Each node triggers only its shards. On node failure, its leases expire and other nodes take over its shards, starting from the oldest unprocessed bucket so nothing is skipped. Assignments carry an epoch, and writes include it, so a former owner that wakes up cannot act on stale ownership. Workers stay stateless and are scaled by queue depth. See [Leader election and consensus]({{ '/backend/leader-election-and-consensus/' | relative_url }}).

## Scaling and bottlenecks

| Load | What breaks first | Fix |
|---|---|---|
| 10× (15k runs/s, 3M-run storms) | Claim contention and index churn on one Postgres; vacuum on the hot runs table | Shard runs across Postgres clusters by job hash; dispatcher moves ready runs into per-pool queues (Kafka, SQS) so workers stop polling the DB; storm smoothing with hashed offsets |
| 100× | Postgres as the time index | Time-bucketed wide-column store; in-memory timing wheels per shard owner; per-tenant queues with weighted dispatch |
| Huge tenants | Fairness caps throttle them | Dedicated pools or cells for top tenants |

Run history grows fastest. Keep it in daily partitions, drop or archive after 30 days, and keep a compact per-job summary (last status, last success time) for the UI. See [Partitioning]({{ '/postgresql/partitioning/' | relative_url }}).

## Reliability and failure modes

| Failure | Effect | Mitigation |
|---|---|---|
| Scheduler node dies | Its shards stop triggering | Lease expiry, reassignment, catch-up from last bucket, misfire policy |
| Worker dies mid-run | Run stuck `running` | Lease expiry, reaper, retry |
| Worker pauses, resumes after lease loss | Stale result | Attempt-number fencing on every update |
| Split-brain shard ownership | Double trigger attempt | Unique `(job_id, scheduled_for)` plus compare-and-set |
| Downstream outage | Mass failures, DLQ flood | Per-handler circuit breaker that pauses claims; backoff with jitter |
| Database failover | Claims in flight lost | Runs return via lease expiry; handlers are idempotent |
| Clock skew | Early or late firing | Use database time for due checks and leases |
| Midnight storm | Trigger latency spikes | Hashed offsets, pre-materialized runs, autoscaled workers |

## Trade-offs

| Decision | Chosen? | Why |
|---|---|---|
| PostgreSQL with SKIP LOCKED first | Yes | Transactions, constraints, simple ops at this scale |
| Push-based dispatch via a broker | At 10× | Removes DB polling; adds a second system to keep consistent |
| Exactly-once execution | No | Impossible in general; at-least-once plus idempotent handlers |
| Exactly-once triggering | Yes | Unique constraint plus compare-and-set is cheap and robust |
| Strict priority | No | Starves low priority; weighted fairness and aging instead |
| Many logical shards | Yes | Cheap rebalancing without rehashing jobs |
| Leases over locks held for the run | Yes | A crashed worker releases work automatically |

## Interview follow-ups

**How do you avoid running a cron twice after a scheduler failover?** Make triggering idempotent: `UNIQUE (job_id, scheduled_for)` on runs and a compare-and-set on `next_run_at`. Two leaders can both try; only one row results.

**Why `SKIP LOCKED` instead of `FOR UPDATE`?** Without it, every worker waits on the same first locked rows. `SKIP LOCKED` lets each claim different rows, so claims scale with workers.

**A job takes longer than its lease. What happens?** Without heartbeats the lease expires and another worker starts it; the first worker's completion is rejected by the token check. Long jobs heartbeat, and the handler is idempotent anyway.

**How do you handle 300k jobs due at midnight?** Spread cron jobs with hashed offsets when users allow it, pre-materialize runs minutes ahead, claim in batches, autoscale workers on queue depth, and accept a short trigger delay for low-priority work.

**How do you keep one tenant from starving others?** Per-tenant concurrency caps, round-robin or weighted claims across tenants, and separate pools for priority classes.

**How would you implement "run C after A and B"?** A `deps_remaining` counter per task, atomically decremented when parents succeed, in the same transaction that records success. Whoever drives it to zero enqueues C.

**Where does the DLQ live and how is it used?** Dead runs in the same table with their last error. Operators inspect, fix the cause, and redrive in bulk; alerts fire on inflow rate.
