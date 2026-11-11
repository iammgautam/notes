---
title: Rollups and materialized views
section: postgresql
order: 13
summary: Turn slow analytics scans into fast lookups with precomputed aggregates, incremental rollup tables, and materialized views refreshed concurrently.
---

Indexes make it cheaper to find rows; they don't make it cheaper to aggregate millions of them. A dashboard that counts events per day per tenant over a year reads every one of those events on every page load. The fix is to aggregate once and read the result many times. Done well, this turns multi-minute queries into sub-second ones, because you read hundreds of pre-aggregated rows instead of hundreds of millions of raw ones.

## How it works

Three options, in increasing order of control:

| Approach | Freshness | Refresh cost | Complexity |
|---|---|---|---|
| Materialized view + `REFRESH` | As of last refresh | Full recompute | Lowest |
| Rollup table, incremental upsert | Seconds to minutes | Only new data | Medium |
| Trigger-maintained summary | Real time | Per write, contention | High, risky |

### Materialized views

A materialized view stores a query's result as a table.

```sql
CREATE MATERIALIZED VIEW daily_event_counts AS
SELECT tenant_id,
       date_trunc('day', created_at AT TIME ZONE 'UTC')::date AS day,
       event_type,
       count(*) AS events,
       count(DISTINCT user_id) AS users
FROM events
GROUP BY 1, 2, 3;

CREATE UNIQUE INDEX daily_event_counts_key
  ON daily_event_counts (tenant_id, day, event_type);
```

Refresh modes:

- `REFRESH MATERIALIZED VIEW daily_event_counts;` rebuilds it under an `ACCESS EXCLUSIVE` lock. Reads block for the whole refresh.
- `REFRESH MATERIALIZED VIEW CONCURRENTLY daily_event_counts;` builds the new result in a temp table, diffs it against the current contents, and applies inserts/updates/deletes. It takes an `EXCLUSIVE` lock, which allows reads but blocks concurrent refreshes. It **requires a unique index** on the view (no `WHERE`, plain columns), and is slower than a plain refresh when most rows change.

The core limitation: **both modes recompute the whole query**. Refreshing a view over 500M events still scans 500M events. Materialized views fit when the source is modest, or the view is over an already-aggregated table.

### Incremental rollup tables

A rollup table is a regular table you maintain yourself, aggregating only new or changed data and upserting it.

```sql
CREATE TABLE event_rollup_hourly (
  tenant_id   bigint NOT NULL,
  hour        timestamptz NOT NULL,
  event_type  text NOT NULL,
  events      bigint NOT NULL,
  revenue_cents bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, hour, event_type)
);
```

The key design question: **which raw rows haven't been rolled up yet?**

- **Time windows**: recompute the last N closed hours each run. Simple and idempotent, but late-arriving events older than N are missed.
- **High-water mark on a sequence**: track the last processed `events.id`. Catches every row, but `id` order isn't commit order: a transaction that took id 1000 may commit after one that took 1001. Process only up to a safe point, such as ids older than a few seconds, or use the transaction-safe approach below.
- **Change queue**: write affected `(tenant_id, hour)` keys to a small "dirty" table in the same transaction as the event, then recompute just those buckets.

### Additive vs non-additive measures

`count`, `sum`, `min`, `max` merge across batches. `count(DISTINCT user_id)`, medians and percentiles don't: you can't add two distinct counts. Options: recompute affected buckets fully, store HyperLogLog sketches (`postgresql-hll` extension) and merge them, or store the distinct pairs in a narrower table.

## In practice

### Idempotent bucket recompute

Recompute whole buckets rather than adding deltas. Re-running it is harmless, which makes retries and backfills safe:

```sql
INSERT INTO event_rollup_hourly AS r
  (tenant_id, hour, event_type, events, revenue_cents)
SELECT tenant_id, date_trunc('hour', created_at), event_type,
       count(*), coalesce(sum((properties->>'amount_cents')::bigint), 0)
FROM events
WHERE created_at >= $1 AND created_at < $2      -- e.g. last 3 closed hours
GROUP BY 1, 2, 3
ON CONFLICT (tenant_id, hour, event_type) DO UPDATE
SET events = EXCLUDED.events,
    revenue_cents = EXCLUDED.revenue_cents;
```

Buckets that dropped to zero need a delete; for append-only events that rarely happens.

### Dirty-bucket pattern for late data

```sql
CREATE TABLE rollup_dirty (
  tenant_id bigint NOT NULL,
  hour      timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, hour)
);

-- on event insert (trigger or app code, same transaction)
INSERT INTO rollup_dirty VALUES ($1, date_trunc('hour', $2))
ON CONFLICT DO NOTHING;
```

A periodic job claims dirty keys with `DELETE ... RETURNING` (or `FOR UPDATE SKIP LOCKED`), recomputes those buckets from raw events, and upserts. Late data, corrections and deletes all just mark a bucket dirty.

### Layering

Hourly rollup from raw events, daily rollup from hourly, monthly from daily. Each layer is small, so higher layers can even be materialized views refreshed concurrently in seconds:

```sql
CREATE MATERIALIZED VIEW event_rollup_daily AS
SELECT tenant_id, (hour AT TIME ZONE 'UTC')::date AS day, event_type,
       sum(events) AS events, sum(revenue_cents) AS revenue_cents
FROM event_rollup_hourly
GROUP BY 1, 2, 3;
```

> **Gotcha:** Bucket in UTC and convert at read time only if every tenant shares a time zone. If tenants see "today" in their own zone, roll up at a granularity that aligns with every tenant's offset (hourly works for whole-hour zones; `+05:30` needs 30-minute buckets) and aggregate days at query time, or store per-tenant-local day buckets.

### Serving queries

The dashboard reads the rollup, plus raw events for the current open hour if real-time freshness matters:

```sql
SELECT hour, sum(events) FROM event_rollup_hourly
WHERE tenant_id = $1 AND hour >= now() - interval '30 days'
GROUP BY 1
UNION ALL
SELECT date_trunc('hour', created_at), count(*) FROM events
WHERE tenant_id = $1 AND created_at >= date_trunc('hour', now())
GROUP BY 1
ORDER BY 1;
```

720 rollup rows plus one hour of raw events, instead of 30 days of raw events.

### Scheduling

Run rollups from your job queue or `pg_cron`. Guard each tenant or bucket range with `pg_try_advisory_xact_lock` so overlapping runs don't fight, and record each run's window and duration for monitoring lag.

## Pitfalls

- **Blocking refresh.** Plain `REFRESH MATERIALIZED VIEW` blocks every reader. Use `CONCURRENTLY` for anything user-facing.
- **Refreshing big views too often.** A concurrent refresh over a large source scans everything each time and creates bloat from the diff. Move to incremental rollups.
- **Missed late events** with time-window recompute. Size the window to your real lateness, or use dirty buckets.
- **Sequence-based watermarks skipping in-flight rows.** Leave a safety margin behind the newest committed id.
- **Adding distinct counts.** Summing per-hour distinct users does not give daily distinct users.
- **Rollup drift.** Bugs in the rollup job produce silently wrong numbers. Run a periodic reconciliation comparing a sample of rollup buckets with raw aggregates.
- **Timeouts.** Rollup jobs scanning large ranges hit `statement_timeout`. Batch by tenant or time slice.

## Checklist

- Identify dashboard queries aggregating more than ~1M rows per request; those are rollup candidates.
- Pick bucket granularity (usually hourly) and dimensions from what the UI actually filters by.
- Make the rollup job idempotent: recompute whole buckets with upserts.
- Handle late data explicitly (window size or dirty buckets).
- Use `REFRESH ... CONCURRENTLY` with a unique index for materialized views.
- Monitor rollup lag and reconcile against raw data.

## Interview angle

**Q: Materialized view vs rollup table?**
A materialized view is a stored query result that is fully recomputed on refresh. A rollup table is maintained incrementally by your code, so refresh cost is proportional to new data rather than total data.

**Q: What does `REFRESH MATERIALIZED VIEW CONCURRENTLY` need and cost?**
A unique index on the view. It allows reads during refresh by computing a diff and applying it, which is slower than a plain refresh and still recomputes the full query.

**Q: How do you make dashboards over 500M events fast?**
Pre-aggregate into hourly rollups keyed by tenant and dimensions, maintain them incrementally and idempotently, layer coarser rollups on top, and add the current partial bucket from raw data at read time.
