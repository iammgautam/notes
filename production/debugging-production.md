---
title: Debugging production issues
section: production
order: 4
summary: A repeatable method for live issues (scope, timeline, recent changes, hypotheses, reproduce), a slow-request playbook, and the Postgres views to check.
---

Production bugs are different from local bugs: you can't attach a debugger, the data is real, the clock is running and the first plausible theory is often wrong. What helps most is a method you follow even when you're sure you already know the answer.

## How it works

### The method

```text
 1. Scope      who/what is affected, how badly
 2. Timeline   when did it start, is it getting worse
 3. Changes    what changed right before
 4. Hypotheses list them, rank by likelihood x cost to check
 5. Test       cheapest discriminating check first
 6. Reproduce  locally or in staging with prod-like data
 7. Fix        mitigate first, then root cause
```

**1. Scope.** Answer before theorizing:

- All users or some? One tenant, one plan, one region, one browser?
- All endpoints or one? Reads or writes? API, workers, or both?
- Errors, slowness or wrong data? Wrong data is the most dangerous; stop writes before investigating further if it is spreading.

Use tag facets in Sentry, `group by tenant_id, route` in logs, and per-route RED metrics.

**2. Timeline.** Find the start time to the minute from metrics. A step change points to a deploy, config change or flag toggle. A gradual ramp points to growth, a leak, table bloat or a queue backing up. A daily pattern points to a cron job or traffic peak.

**3. Recent changes.** Line up the start time against:

- Deploys (app, workers, infrastructure), migrations.
- Feature flag and config changes.
- Data changes: a large tenant import, a backfill, a new customer onboarding.
- Dependencies: provider status pages, certificate expiry, a library auto-upgrade in a rebuilt image.

Most incidents are caused by a change. "Nothing changed" usually means "nothing I know of changed".

**4. Hypotheses.** Write them down. For each, note what evidence would confirm or kill it. Prefer checks that split the space in half ("does it happen on worker pool B too?").

**5. Test.** Read before you write. Dashboards, logs, traces, read-only queries. Avoid restarting things before capturing state (thread dumps, `pg_stat_activity`), since a restart destroys evidence.

**6. Reproduce.** A reproduction turns guessing into engineering. Copy the failing request (method, path, payload, tenant, user role, flags), run it against staging or a local snapshot with anonymized data. If you can't reproduce, add targeted logging and wait for the next occurrence rather than shipping speculative fixes.

## In practice

### Slow request playbook

A user says "the reports page takes forever".

1. **Find the request.** Get the `X-Request-ID` or time window, find the trace or log line. Note `duration_ms`, `db_queries`, `db_ms`.
2. **Split the time.** Where does it go?

```text
total 9.4s
  db        8.7s   (412 queries)   -> N+1 or one bad query
  external  0.3s
  python    0.4s
```

3. **If many queries:** N+1. Look for a loop over a queryset touching a relation. Fix with `select_related` / `prefetch_related` / annotations. Check with `django-debug-toolbar` or `assertNumQueries` in a test.
4. **If few but slow queries:** grab the SQL, run `EXPLAIN (ANALYZE, BUFFERS)` on a replica or a prod-sized copy. Look for sequential scans on big tables, row estimate errors of 100x+, sorts spilling to disk, nested loops over large sets.
5. **If DB time is low but wall time is high:** waiting. Connection pool exhaustion, lock waits, a slow upstream call, GIL-bound CPU work, or the worker queue in front of the app (gunicorn backlog).
6. **Only one tenant slow?** Data skew. That tenant has 50x the rows, so the planner picks a different plan or a query that is fine at 10k rows dies at 5M.

### Database-side checks

What is running right now, and what is waiting:

```sql
SELECT pid, now() - query_start AS runtime, state,
       wait_event_type, wait_event, left(query, 120) AS query
FROM pg_stat_activity
WHERE state <> 'idle' AND pid <> pg_backend_pid()
ORDER BY runtime DESC
LIMIT 20;
```

Who is blocking whom:

```sql
SELECT blocked.pid AS blocked_pid,
       blocking.pid AS blocking_pid,
       left(blocked.query, 80) AS blocked_query,
       left(blocking.query, 80) AS blocking_query,
       now() - blocking.xact_start AS blocking_xact_age
FROM pg_stat_activity blocked
JOIN pg_stat_activity blocking
  ON blocking.pid = ANY (pg_blocking_pids(blocked.pid));
```

Idle-in-transaction sessions hold locks and block vacuum:

```sql
SELECT pid, now() - xact_start AS xact_age, left(query, 80)
FROM pg_stat_activity
WHERE state = 'idle in transaction'
ORDER BY xact_age DESC;
```

Which queries cost the most in total (needs the `pg_stat_statements` extension):

```sql
SELECT calls,
       round(total_exec_time::numeric, 0) AS total_ms,
       round(mean_exec_time::numeric, 1) AS mean_ms,
       rows,
       left(query, 100) AS query
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 15;
```

Total time catches the 3 ms query called 2 million times an hour; mean time catches the single 40 s report query. Check both. Reset with `pg_stat_statements_reset()` before and after a change to compare.

Connection pressure:

```sql
SELECT state, count(*) FROM pg_stat_activity GROUP BY state;
SHOW max_connections;
```

If you must stop a runaway query: `SELECT pg_cancel_backend(pid);` first (cancels the query), `pg_terminate_backend(pid)` only if cancel doesn't work (kills the session).

### Background jobs checklist

- Queue depth and age of oldest pending task. Growing depth with flat throughput means workers are stuck or slower.
- Tasks stuck in `running` with an expired lease or heartbeat (crashed worker).
- Retry storms: one failing dependency causing every task to retry and starve healthy ones.
- Poison messages: the same task failing on every attempt and blocking a FIFO lane.

## Pitfalls

- **Anchoring.** The first theory feels right; you look only for confirming evidence. Write down what would disprove it.
- **Fixing forward under pressure** with an untested patch, causing a second incident. Prefer rollback or a flag flip.
- **Changing several things at once.** You won't know which fixed it, or which made it worse.
- **Debugging on the primary.** Heavy `EXPLAIN ANALYZE` runs the query for real. Use a replica, and never `EXPLAIN ANALYZE` a write without wrapping it in a transaction you roll back.
- **Trusting the averages.** p50 looks fine while one tenant sits at 30 s.
- **Losing the evidence** by restarting first. Snapshot `pg_stat_activity`, thread dumps (`py-spy dump --pid`) and recent logs before you restart.

## Checklist

- Scope: who, what, how bad. Wrong data → stop the bleeding first.
- Exact start time, then diff against deploys, flags, config, data loads.
- Hypotheses written down with the check that would kill each.
- Read-only evidence before any restart.
- Reproduce before fixing; add a regression test with the fix.

## Interview angle

- **"Walk me through debugging a slow endpoint."** Find a specific slow request, split time into DB/external/app, then N+1 vs a single slow query vs waiting; use `EXPLAIN ANALYZE`, `pg_stat_statements`, and check data skew per tenant.
- **"The site got slow after a deploy but the diff looks harmless."** Check migrations (lost index, table rewrite, lock), changed query plans, new dependencies, config, and roll back first if impact is high.

Related: [Performance profiling]({{ '/production/performance-profiling/' | relative_url }}), [Incidents and postmortems]({{ '/production/incidents-and-postmortems/' | relative_url }}).
