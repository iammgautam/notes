---
title: Multi-tenancy patterns
section: postgresql
order: 17
summary: Compare shared tables with tenant_id, schema-per-tenant and database-per-tenant, enforce isolation with row-level security, and index for tenant-first access.
---

A multi-tenant SaaS has to answer three questions at the database layer: how strongly tenants are isolated, how cost and operations scale with tenant count, and how one noisy tenant affects the rest. The three classic models trade these off differently, and most systems end up with a shared-table core plus an escape hatch for the few largest or most regulated tenants.

## How it works

| Model | Isolation | Tenants per cluster | Migrations | Cross-tenant queries | Noisy neighbor |
|---|---|---|---|---|---|
| Shared tables + `tenant_id` | Logical (app or RLS) | Millions | Once | Easy | Highest risk |
| Schema per tenant | Namespace | Hundreds to low thousands | Per schema | Awkward (`UNION`) | Shared resources |
| Database per tenant | Strong | Tens to hundreds per cluster | Per database | Hard | Separable |

### Shared tables with tenant_id

Every tenant-owned table has a `tenant_id` column, every query filters on it, and every index starts with it.

- Cheapest to run: one schema, one migration, connection pools shared by all tenants.
- Isolation is only as good as your query discipline, so enforce it in the ORM and in the database with RLS.
- Large tenants skew statistics and plans; one tenant with 40% of rows needs different plans than one with 0.01%.

### Schema per tenant

Each tenant gets a schema with an identical set of tables; requests set `search_path` (this is how `django-tenants` works).

- Natural per-tenant backup/restore and deletion (`DROP SCHEMA ... CASCADE`).
- Catalog bloat: 2,000 tenants times 80 tables, each with indexes, TOAST tables and sequences, is hundreds of thousands of relations. Backends cache catalog entries per relation touched, so memory per connection grows, `pg_dump` slows down, and migrations run 2,000 times.
- `search_path` is session state, which interacts badly with transaction pooling (see [Connection pooling]({{ '/postgresql/connection-pooling/' | relative_url }})).

### Database per tenant

Each tenant gets its own database (or cluster).

- Strongest isolation; per-tenant restore, encryption keys, region, and version upgrades.
- Connections can't be shared across databases, so pooling multiplies. Operations need automation for provisioning, migrations and monitoring.
- Fits enterprise or regulated tenants, or a small number of large ones.

## In practice

### Shared-table schema done right

```sql
CREATE TABLE tenants (
  id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  slug  text NOT NULL UNIQUE,
  plan  text NOT NULL
);

CREATE TABLE projects (
  tenant_id bigint NOT NULL REFERENCES tenants(id),
  id        bigint GENERATED ALWAYS AS IDENTITY,
  name      text NOT NULL,
  slug      text NOT NULL,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, slug)
);

CREATE TABLE tasks (
  tenant_id  bigint NOT NULL,
  id         bigint GENERATED ALWAYS AS IDENTITY,
  project_id bigint NOT NULL,
  status     text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, id)
);

CREATE INDEX tasks_tenant_project_status_idx
  ON tasks (tenant_id, project_id, status);
```

Key points:

- **Composite foreign keys including `tenant_id`** make it impossible for a task in tenant A to reference a project in tenant B. A plain `project_id` FK doesn't prevent that.
- **Unique constraints are per tenant**: `(tenant_id, slug)`, not `slug`.
- **Indexes start with `tenant_id`.** Almost every query has `tenant_id = $1`, so a leading `tenant_id` lets the index jump to that tenant's slice. It also keeps one tenant's index entries together, which helps cache locality.
- **Composite PKs** are optional. They make tenant-scoped lookups and sharding (Citus distributes by `tenant_id`) natural, at the cost of ORM friction. Many Django apps keep a global `id` PK and add `tenant_id` to indexes and constraints instead.

### Row-level security

RLS makes the database enforce the tenant filter, so a forgotten `.filter(tenant=...)` returns nothing instead of another tenant's data.

```sql
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks FORCE ROW LEVEL SECURITY;    -- apply to the table owner too

CREATE POLICY tenant_isolation ON tasks
  USING (tenant_id = current_setting('app.tenant_id')::bigint)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::bigint);
```

Set the tenant per transaction (safe with transaction pooling):

```python
from contextlib import contextmanager

from django.db import connection, transaction

@contextmanager
def tenant_txn(tenant_id):
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SELECT set_config('app.tenant_id', %s, true)",
                        [str(tenant_id)])
        yield
```

In practice you wrap this in middleware that opens a transaction per request (or uses `ATOMIC_REQUESTS`) and sets the variable at the start.

RLS details that matter:

- **Superusers and roles with `BYPASSRLS` skip policies.** The app must connect as a non-owner, non-superuser role, or the table must use `FORCE ROW LEVEL SECURITY`.
- **Missing setting.** `current_setting('app.tenant_id')` errors if the setting was never defined; after a transaction-local `set_config` ends, it reads as an empty string and the `::bigint` cast errors. `current_setting('app.tenant_id', true)` returns `NULL` and the policy matches nothing. Both fail closed; the erroring form surfaces bugs faster.
- **Performance.** The policy predicate is added to every query. With an index leading on `tenant_id` it's essentially free. Policies that call non-`LEAKPROOF` functions or do subqueries can block index usage and add overhead; keep them to a simple equality.
- **Admin and cross-tenant jobs** (rollups, billing) run as a separate role with `BYPASSRLS`, ideally in a separate service or connection pool.

### Handling large tenants

- **Skewed statistics.** Raise the statistics target on `tenant_id` so the MCV list captures big tenants:

  ```sql
  ALTER TABLE tasks ALTER COLUMN tenant_id SET STATISTICS 1000;
  ```

- **Generic plan problems.** A generic plan built for a typical tenant may be awful for the largest one. Client-side binding (Django's default) or `plan_cache_mode = force_custom_plan` avoids this.
- **Partitioning.** Hash-partition the largest tables by `tenant_id`, or list-partition the few whales into their own partitions.
- **Fair scheduling** in background jobs: cap per-tenant concurrency so one tenant's import doesn't starve the queue.
- **Move out.** When a tenant outgrows the shared cluster, move it to a dedicated database. Composite keys with `tenant_id` make that extract a clean `WHERE tenant_id = $1` per table.

### Tenant deletion and export

With shared tables, deleting a tenant means batched deletes across every table (or `ON DELETE CASCADE` from `tenants`, which is one huge transaction; avoid on large tenants). Plan it as a background job with progress tracking. Data export is the same walk with `COPY (SELECT ... WHERE tenant_id = $1) TO STDOUT`.

## Pitfalls

- **Tenant filter forgotten in one query.** Use a tenant-aware manager in Django that requires a tenant, and add RLS as the backstop.
- **Global unique constraints.** `UNIQUE (email)` prevents the same user signing up to two tenants.
- **Indexes without `tenant_id` first.** An index on `(created_at)` serves cross-tenant scans, not the per-tenant dashboard.
- **Cross-tenant FKs.** Single-column FKs allow referencing another tenant's rows.
- **Schema-per-tenant at 10k tenants.** Catalog size, migration time and backend memory become the bottleneck.
- **Leaking `search_path` or tenant settings** through pooled connections. Always transaction-local.
- **Caches keyed without tenant.** Not a Postgres problem, but the most common cross-tenant leak in practice: application cache keys missing the tenant id.

> **Rule of thumb:** Start with shared tables, `tenant_id` on every row, tenant-first indexes, composite FKs, and RLS as a safety net. Move individual large or regulated tenants to dedicated databases when they need it.

## Checklist

- `tenant_id NOT NULL` on every tenant-owned table.
- Indexes and unique constraints lead with `tenant_id`.
- Composite FKs that include `tenant_id`.
- RLS with `FORCE`, a non-owner app role, and transaction-local tenant settings.
- Separate `BYPASSRLS` role for cross-tenant jobs.
- Monitor per-tenant row counts and query time to spot whales early.

## Interview angle

**Q: Compare the three multi-tenancy models.**
Shared tables are cheapest and scale to millions of tenants but rely on discipline or RLS for isolation. Schema-per-tenant gives namespace isolation but suffers catalog bloat and per-schema migrations. Database-per-tenant gives strong isolation at high operational cost.

**Q: How does row-level security enforce tenant isolation?**
A policy adds `tenant_id = current_setting('app.tenant_id')` to every query on the table. The app sets the setting per transaction and connects as a role that can't bypass RLS.

**Q: Why put `tenant_id` first in indexes?**
Nearly every query filters by tenant with equality, so leading with it lets the B-tree jump directly to that tenant's entries, then use later columns for filtering and ordering.
