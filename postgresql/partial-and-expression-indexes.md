---
title: Partial and expression indexes
section: postgresql
order: 4
summary: Index only the rows or computed values your queries need, so indexes stay small, hot in cache, and actually match the predicate.
---

Most tables have a skewed access pattern: the app queries the 2% of rows that are pending, active or unprocessed, and almost never the 98% that are done. Partial indexes let you index only the slice you query. Expression indexes let you index a computed value, like `lower(email)` or a JSONB field, so the planner can match predicates that wrap columns in functions.

## How it works

### Partial indexes

A partial index has a `WHERE` clause. Only rows that satisfy it get index entries.

```sql
CREATE INDEX CONCURRENTLY jobs_pending_idx
  ON jobs (queue, run_at)
  WHERE status = 'pending';
```

The planner uses it only when it can **prove** that the query's `WHERE` clause implies the index predicate. The proof is syntactic and conservative:

| Query predicate | Index predicate `status = 'pending'` usable? |
|---|---|
| `status = 'pending'` | Yes |
| `status = 'pending' AND queue = 'email'` | Yes |
| `status = $1` (generic plan) | No, the value is unknown at plan time |
| `status IN ('pending', 'retry')` | No |
| `status <> 'done'` | No |

For range predicates it can do simple implication: index `WHERE amount > 100` is usable for query `WHERE amount > 500`.

> **Gotcha:** With a parameterized query, a partial index on `status = 'pending'` is only usable in a custom plan where `$1` is known. Hardcode the literal in the SQL (Django ORM does this when you write `filter(status="pending")` with client-side binding) or use `plan_cache_mode = force_custom_plan`.

Benefits:

- **Size.** Indexing 200k pending rows instead of 50M total jobs: tens of MB instead of GB. It stays in `shared_buffers`.
- **Write cost.** Rows outside the predicate never touch the index. Updating a `done` job does not maintain it.
- **Partial uniqueness.** Enforce a constraint on a subset of rows.

### Expression indexes

An expression index stores the result of an expression instead of a raw column:

```sql
CREATE INDEX CONCURRENTLY users_email_lower_idx
  ON users (lower(email));
```

The query must use the **same expression** to match:

```sql
SELECT id FROM users WHERE lower(email) = lower($1);  -- uses index
SELECT id FROM users WHERE email ILIKE $1;            -- does not
```

Rules:

- The function must be `IMMUTABLE`. `now()`, `random()`, and most timezone-dependent casts are not. `date_trunc('day', timestamptz_col)` is only `STABLE` because it depends on the session `TimeZone`; use `date_trunc('day', col AT TIME ZONE 'UTC')` which is immutable.
- `ANALYZE` collects statistics on the expression, which fixes selectivity estimates for queries using it. That alone can fix plans.
- Each insert or update evaluates the expression, so keep it cheap.

## In practice

### Job queue: index only work that is waiting

```sql
CREATE INDEX CONCURRENTLY jobs_ready_idx
  ON jobs (queue, priority DESC, run_at)
  WHERE status = 'pending';

SELECT id FROM jobs
WHERE status = 'pending' AND queue = 'email' AND run_at <= now()
ORDER BY priority DESC, run_at
LIMIT 10
FOR UPDATE SKIP LOCKED;
```

As jobs finish and move to `done`, their entries disappear from the index's live set. The index stays proportional to backlog, not history.

### Soft deletes

```sql
CREATE INDEX CONCURRENTLY documents_tenant_updated_live_idx
  ON documents (tenant_id, updated_at DESC)
  WHERE deleted_at IS NULL;
```

Every Django manager that filters `deleted_at__isnull=True` matches this predicate.

### Partial unique constraints

One active subscription per tenant; historical ones allowed:

```sql
CREATE UNIQUE INDEX CONCURRENTLY subscriptions_one_active_per_tenant
  ON subscriptions (tenant_id)
  WHERE status = 'active';
```

Unique email per tenant among non-deleted users, case-insensitive:

```sql
CREATE UNIQUE INDEX CONCURRENTLY users_tenant_email_live_key
  ON users (tenant_id, lower(email))
  WHERE deleted_at IS NULL;
```

In Django:

```python
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

class User(models.Model):
    tenant = models.ForeignKey("Tenant", on_delete=models.CASCADE)
    email = models.EmailField()
    deleted_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                "tenant", Lower("email"),
                condition=Q(deleted_at__isnull=True),
                name="users_tenant_email_live_key",
            ),
        ]
```

### JSONB field extraction

When one JSONB key is queried constantly with equality or ranges, a B-tree expression index beats GIN:

```sql
CREATE INDEX CONCURRENTLY events_tenant_plan_idx
  ON events (tenant_id, (properties->>'plan'));

SELECT count(*) FROM events
WHERE tenant_id = 42 AND properties->>'plan' = 'enterprise';
```

Note the extra parentheses required around a non-function expression in the index definition.

### Analytics: index the bucket you group by

```sql
CREATE INDEX CONCURRENTLY events_tenant_day_idx
  ON events (tenant_id, (date_trunc('day', created_at AT TIME ZONE 'UTC')));
```

## Pitfalls

- **Predicate mismatch.** Index on `WHERE status = 'pending'`, query on `WHERE status IN ('pending')`. Postgres usually normalizes single-element `IN`, but `status = ANY($1)` with an array param won't match. Read the plan; don't assume.
- **Mutable functions.** `CREATE INDEX ... (date(created_at))` on a `timestamptz` fails with `functions in index expression must be marked IMMUTABLE`. Convert with an explicit time zone.
- **Changing predicate values.** If product adds a new status `'retrying'` that also needs fast lookups, the partial index silently stops covering it. Review partial indexes when enums change.
- **Too many tiny partial indexes.** One per tenant or per queue is an anti-pattern: planning time grows and you lose track. Use a composite with the discriminator as a key column.
- **Expression drift in the ORM.** The ORM may generate `UPPER(...)` while the index uses `lower(...)`, or `::date` vs `date_trunc`. Compare the SQL from `str(qs.query)` with the index definition.
- **Bloat on churny partial indexes.** A queue index sees constant insert and delete. Autovacuum must keep up; monitor with `pgstattuple` and `REINDEX CONCURRENTLY` if it grows.

> **In production:** If a partial index isn't used, run the query with literal values in `EXPLAIN`. If the literal version uses it and the app version doesn't, you have a generic-plan problem, not an index problem.

## Rules of thumb

- Use a partial index when queries target a small, stable subset (pending, active, not deleted).
- Put the subset filter in `WHERE` and the query's other filters and sort in the key columns.
- Use partial unique indexes for "at most one X per Y" business rules.
- Use expression indexes when the query wraps a column in a function; match the expression exactly.
- Check `pg_stat_user_indexes.idx_scan` after deploy to confirm the index is used.

## Interview angle

**Q: When would you use a partial index?**
When queries consistently target a small subset of rows, like pending jobs or non-deleted records. The index is smaller, cheaper to maintain, and stays cached.

**Q: Why isn't my partial index used with a prepared statement?**
The planner must prove the query predicate implies the index predicate at plan time. With a generic plan the parameter value is unknown, so the proof fails.

**Q: How do you enforce "one active subscription per tenant"?**
`CREATE UNIQUE INDEX ... ON subscriptions (tenant_id) WHERE status = 'active'`.
