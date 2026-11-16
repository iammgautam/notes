---
title: Multi-tenant architecture
section: backend
order: 8
phase: "Phase 1 · Backend architecture"
summary: Silo, pool and bridge models, tenant isolation and noisy neighbors, context propagation, per-tenant limits and caching, onboarding and fleet migrations.
---

A SaaS product serves many customers (tenants) from one codebase. The architecture has to answer four questions at every layer: can tenant A ever see tenant B's data, can tenant A's traffic spike slow down tenant B, how does cost scale with the number of tenants, and how do you operate (deploy, migrate, back up, delete) thousands of them. The answers differ for the database, the compute tier, caches, queues and observability, and a mature system usually mixes models: shared infrastructure for the long tail and dedicated resources for the few tenants who pay for it. The PostgreSQL side of this is covered in depth in [Multi-tenancy patterns]({{ '/postgresql/multi-tenancy/' | relative_url }}); this note is about the whole system.

## Multi-tenant architecture

### Silo, pool and bridge models

AWS's SaaS guidance popularized three names for how much is shared:

| Model | What's shared | Isolation | Cost per tenant | Ops at 5,000 tenants | Fits |
|---|---|---|---|---|---|
| Silo | Nothing (own DB, often own compute or account) | Strongest | Highest | Fleet automation required | Enterprise, regulated, data residency |
| Pool | Everything; rows carry `tenant_id` | Logical only | Lowest | One deploy, one migration | Self-serve, long tail, freemium |
| Bridge | Some layers shared, some dedicated | Mixed | Middle | Two paths to maintain | Most real products |

Bridge is less a third model than an acknowledgment that the choice is **per layer** and often **per tier**:

- Shared compute, schema per tenant in a shared database.
- Shared everything for free and standard tiers; dedicated database for enterprise.
- Shared app tier, dedicated queue workers for tenants with heavy batch jobs.

```text
               +---------------- tenant directory -----------------+
               | tenant -> tier, cell, db dsn, region, limits      |
               +---------------------------------------------------+
                         ^ lookup on every request (cached)
  request --> edge: resolve tenant (subdomain / JWT claim / API key)
                         |
          +--------------+-------------------+
          v                                  v
   pooled cell (free, standard)       silo (enterprise tenant 42)
   +----------------------------+     +---------------------------+
   | shared app pods            |     | dedicated app pods        |
   | shared Postgres, tenant_id |     | own Postgres, own KMS key |
   | shared Redis, keyed by     |     | own Redis                 |
   |   tenant                   |     |                           |
   +----------------------------+     +---------------------------+
```

The **tenant directory** (or catalog) is the core of a mixed model: a small, highly available service or table mapping tenant to tier, cell, database, region and limits. Everything routes through it, so it is cached aggressively and changes rarely.

**Cells** generalize this: a cell is a complete copy of the stack serving a subset of tenants. Cells cap blast radius (an incident hits one cell's tenants), cap the size of any one database, and make capacity planning per cell. AWS has publicly described cell-based architecture as a core resilience practice, and Slack has publicly described moving to cells.

### Tenant isolation

Isolation has several dimensions, and a model can be strong on one and weak on another.

**Data isolation**: can a bug leak rows across tenants?

- Pool: every query must filter by `tenant_id`. Enforce it in two places: the application (a tenant-scoped manager or repository that can't be bypassed casually) and the database (PostgreSQL row-level security as a backstop). Put `tenant_id` first in composite keys and indexes.
- Silo: isolation by connection string; the risk moves to routing (connecting to the wrong tenant's database).
- Per-tenant encryption keys (envelope encryption with a key per tenant in KMS) allow crypto-shredding on deletion and satisfy customers who want to control their own key.

**Compute isolation**: do tenants share processes, memory and CPU? Shared pods mean a tenant's pathological request (a 2 GB export) can starve others in the same worker. Dedicated pods or worker pools for heavy tenants fix this at a cost.

**Noisy neighbors**: in pooled systems, one tenant's load degrades everyone. Defenses, roughly in order of cost:

1. **Per-tenant rate limits and quotas** at the edge, sized by plan.
2. **Per-tenant concurrency caps** on expensive operations (exports, reports, bulk imports).
3. **Fair scheduling of background work**: round-robin or weighted fair queuing across tenants instead of one FIFO queue where a 100,000-job import blocks everyone's password reset emails.
4. **Database guardrails**: `statement_timeout`, per-tier connection pools, and read replicas for analytics queries.
5. **Shuffle sharding**: give each tenant a random small subset of workers (say 2 of 8, giving 28 possible combinations), so a poison tenant takes down only its own subset, and few other tenants share both of its workers. The Amazon Builders' Library publicly describes this technique.
6. **Move the tenant**: to a dedicated shard, worker pool or silo.

**Failure isolation**: does one tenant's failure spread? Poison messages from one tenant shouldn't stop a shared consumer; send them to a dead letter queue (see [Ordering and dead letter queues]({{ '/backend/ordering-and-dead-letter-queues/' | relative_url }})). Per-tenant circuit breakers stop one broken webhook endpoint from tying up the outbound HTTP pool.

### Tenant context propagation

Every unit of work must know its tenant, and that knowledge must travel across every boundary without being re-derived from untrusted input.

1. **Resolve once at the edge**: from subdomain (`acme.app.com`), a verified JWT claim, or the API key's owner. Never from a client-controlled header or body field alone. If the user belongs to several tenants, check membership.
2. **Hold it in request-scoped context**: Python `contextvars` (safe across threads and asyncio tasks), not a global or a thread-local in async code.
3. **Push it into the database session**: `set_config('app.tenant_id', ..., true)` inside the transaction so row-level security policies can see it.
4. **Pass it explicitly across process boundaries**: as a task argument for Celery jobs, a message header and field for Kafka events, a header on internal HTTP and gRPC calls (set by the caller's middleware, trusted only on authenticated internal traffic).
5. **Attach it to telemetry**: as a structured log field and trace attribute.

> **Rule of thumb:** missing tenant context should fail closed. A query with no tenant raises, it doesn't return all rows. Cross-tenant operations (admin tooling, billing rollups) use a separate, explicit, audited code path.

### Per-tenant configuration and limits

Tenants differ in plan, features and limits. Model it as data, not code:

- **Entitlements** per plan (`max_seats`, `api_rps`, `storage_gb`, `sso_enabled`) with per-tenant overrides for negotiated contracts.
- **Feature flags** targeted by tenant, used for gradual rollouts (internal tenants first, then 1%, then everyone).
- **Settings** (timezone, currency, retention period, data region).

Config is read on nearly every request, so cache it in-process for seconds to a minute, and invalidate on change with a pub/sub message. Avoid `if tenant.slug == "acme":` in code; it's a per-tenant fork that no one will remember to remove.

### Tenant-aware caching

Two failure modes: leaking data across tenants through a shared key, and one tenant evicting everyone else's hot data.

- **Always include the tenant in the key**: `t:{tenant_id}:dashboard:v3`. A key built only from an object ID is a cross-tenant leak waiting for an ID collision or a mistaken lookup.
- **Per-tenant invalidation** with a generation counter: keys include `g{n}`; bumping the tenant's generation makes all old keys unreachable at once and they age out by TTL. This avoids `SCAN`-and-delete over millions of keys.
- **Fairness**: in a shared Redis, one tenant's huge reports can push out everyone's sessions. Cap value sizes, give large tenants shorter TTLs or their own cache, and watch hit ratio per tenant.
- **Redis Cluster hash tags** like `{tenant_42}:...` co-locate a tenant's keys on one slot, which makes multi-key operations possible but creates hot slots for big tenants. Use them deliberately.

See [Caching patterns]({{ '/backend/caching-patterns/' | relative_url }}) for the general patterns.

### Onboarding

Signup should create a working tenant in seconds for pooled tiers and minutes for silos, reliably, even when a step fails halfway.

- Model provisioning as an **idempotent workflow** with a status (`provisioning`, `active`, `failed`): create the tenant row, seed defaults (roles, templates, settings), create the admin user, configure the identity provider, register the subdomain, and for silos, create the database and infrastructure through infrastructure as code. Each step checks whether it already happened, so a retry continues instead of duplicating. See [Distributed transactions]({{ '/backend/distributed-transactions/' | relative_url }}) for the saga pattern behind this.
- For silos, keep a **warm pool** of pre-provisioned databases so onboarding doesn't wait for a 10-minute instance create.
- **Offboarding** is onboarding in reverse and is just as important: data export, a retention period, then deletion across every store (database, object storage, caches, search indexes, backups policy, analytics), with an audit record.

### Migrations across tenants

Schema changes and data moves get harder with each step away from the pool model.

- **Pool**: one migration, but tables are huge and shared, so every migration must be online (no long locks, batched backfills). See [Zero-downtime migrations]({{ '/postgresql/zero-downtime-migrations/' | relative_url }}).
- **Schema or database per tenant**: the migration runs N times. Track the schema version per tenant, run in batches with bounded concurrency, start with internal and canary tenants, and make the runner resumable. The fleet will be on **mixed versions** for minutes to hours, so code must work against both (expand, migrate, contract); see [Database migrations]({{ '/backend/database-migrations/' | relative_url }}).
- **Moving a tenant** between shards, cells or into a silo: copy a snapshot, stream changes (logical replication or change data capture filtered by `tenant_id`), verify counts and checksums, briefly block writes for that tenant, flip the tenant directory entry, and resume. Design `tenant_id` into every table and globally unique IDs (UUIDs or Snowflake-style IDs) from day one, or moves require rewriting keys.

## In practice

### Context, scoped access, cache keys and fair queuing

A runnable stdlib model of four core mechanisms: ambient tenant context that fails closed, a repository that always scopes by tenant, a cache with tenant generations, and a fair queue across tenants.

```python
import contextvars
from collections import OrderedDict, deque
from contextlib import contextmanager

_tenant = contextvars.ContextVar("tenant_id", default=None)


@contextmanager
def tenant_context(tenant_id: str):
    token = _tenant.set(tenant_id)
    try:
        yield
    finally:
        _tenant.reset(token)


def current_tenant() -> str:
    tenant_id = _tenant.get()
    if tenant_id is None:
        raise PermissionError("no tenant in context")   # fail closed
    return tenant_id


class TenantRepo:
    """Every read and write is scoped by the ambient tenant; callers cannot opt out."""

    def __init__(self):
        self.rows = []

    def add(self, **fields):
        self.rows.append({**fields, "tenant_id": current_tenant()})

    def all(self):
        tid = current_tenant()
        return [r for r in self.rows if r["tenant_id"] == tid]


class TenantCache:
    """Keys carry the tenant id plus a per-tenant generation for O(1) flushes."""

    def __init__(self):
        self.data, self.generation = {}, {}

    def _key(self, key: str) -> str:
        tid = current_tenant()
        return f"t:{tid}:g{self.generation.get(tid, 0)}:{key}"

    def get(self, key):
        return self.data.get(self._key(key))

    def set(self, key, value):
        self.data[self._key(key)] = value   # real cache: TTL so old gens age out

    def invalidate_tenant(self):
        tid = current_tenant()
        self.generation[tid] = self.generation.get(tid, 0) + 1


class FairQueue:
    """Round-robin across tenants so one tenant's backlog can't starve the rest."""

    def __init__(self):
        self.queues = OrderedDict()

    def put(self, tenant_id: str, job):
        self.queues.setdefault(tenant_id, deque()).append(job)

    def get(self):
        if not self.queues:
            return None
        tenant_id, jobs = next(iter(self.queues.items()))
        job = jobs.popleft()
        self.queues.pop(tenant_id)
        if jobs:
            self.queues[tenant_id] = jobs       # back of the line
        return tenant_id, job


if __name__ == "__main__":
    repo, cache = TenantRepo(), TenantCache()
    with tenant_context("acme"):
        repo.add(id=1, total=100)
        cache.set("dashboard", "acme-v1")
    with tenant_context("globex"):
        repo.add(id=1, total=999)
        assert [r["total"] for r in repo.all()] == [999]
        assert cache.get("dashboard") is None             # no cross-tenant hit
    with tenant_context("acme"):
        assert [r["total"] for r in repo.all()] == [100]
        assert cache.get("dashboard") == "acme-v1"
        cache.invalidate_tenant()
        assert cache.get("dashboard") is None
    try:
        repo.all()
        raise AssertionError("expected PermissionError")
    except PermissionError:
        pass

    q = FairQueue()
    for i in range(5):
        q.put("bigco", f"export-{i}")                     # noisy neighbor
    q.put("tiny", "invoice-1")
    q.put("small", "report-1")
    order = [q.get()[0] for _ in range(4)]
    assert order == ["bigco", "tiny", "small", "bigco"]
    print("tenancy demo ok")
```

### Django middleware and a tenant-scoped manager

Illustrative: resolve the tenant from the subdomain, verify membership, then set the context and the Postgres variable for row-level security. The middleware opens the transaction itself so `set_config(..., true)` lasts exactly that transaction, which also keeps it safe behind PgBouncer in transaction mode. Django's `ATOMIC_REQUESTS` wraps only the view, not middleware, so a `set_config` issued from middleware in autocommit mode would vanish immediately.

```python
# tenancy/middleware.py
from django.db import connection, transaction
from django.http import HttpResponseForbidden, HttpResponseNotFound

from .context import tenant_context
from .models import Tenant


class TenantMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        slug = request.get_host().split(".")[0]
        tenant = Tenant.objects.filter(slug=slug, status="active").first()
        if tenant is None:
            return HttpResponseNotFound()
        user = request.user
        if user.is_authenticated and not user.memberships.filter(tenant=tenant).exists():
            return HttpResponseForbidden()
        request.tenant = tenant
        with transaction.atomic(), tenant_context(tenant.id):
            with connection.cursor() as cur:
                cur.execute("SELECT set_config('app.tenant_id', %s, true)",
                            [str(tenant.id)])
            return self.get_response(request)


# tenancy/models.py
from django.db import models
from .context import current_tenant


class TenantManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(tenant_id=current_tenant())


class TenantModel(models.Model):
    tenant = models.ForeignKey("tenancy.Tenant", on_delete=models.PROTECT)
    objects = TenantManager()          # default: scoped
    all_tenants = models.Manager()     # explicit, grep-able escape hatch

    class Meta:
        abstract = True
```

The database backstop, so a raw SQL query that forgets the filter still can't leak:

```sql
ALTER TABLE invoices ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoices FORCE ROW LEVEL SECURITY;   -- applies to the table owner too
CREATE POLICY tenant_isolation ON invoices
  USING (tenant_id = current_setting('app.tenant_id')::bigint)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::bigint);
```

### Background jobs and events

Context doesn't cross process boundaries by itself. Pass the tenant explicitly and restore it in the worker (illustrative Celery):

```python
import functools
from celery import shared_task
from tenancy.context import tenant_context


def tenant_task(fn):
    @functools.wraps(fn)
    def wrapper(tenant_id, *args, **kwargs):
        with tenant_context(tenant_id):
            return fn(*args, **kwargs)
    return wrapper


@shared_task(queue="exports")
@tenant_task
def export_invoices(month):
    ...


export_invoices.delay(request.tenant.id, "2026-10")
```

For Kafka, put `tenant_id` in the message payload and a header, and use it as the key only when you need per-tenant ordering, since a large tenant then becomes a hot partition.

### Per-tenant rate limit in Redis

Illustrative fixed-window limit keyed by tenant, with the limit read from the tenant's plan:

```python
import time
import redis

r = redis.Redis()


def allow(tenant_id: int, limit_per_min: int) -> bool:
    window = int(time.time() // 60)
    key = f"rl:{tenant_id}:{window}"
    with r.pipeline() as p:
        p.incr(key)
        p.expire(key, 120)
        count, _ = p.execute()
    return count <= limit_per_min
```

## Pitfalls

- **The forgotten filter.** One `Invoice.objects.raw(...)` or an analytics query without `tenant_id` leaks data. Symptoms: support tickets about seeing someone else's records, the worst kind of incident. Fix with fail-closed context, scoped managers, RLS as a backstop, and tests that run every endpoint as two tenants and assert no overlap.
- **Tenant ID from the client.** Trusting `X-Tenant-Id` from the browser lets any user pick a tenant. Derive it from the verified identity and check membership.
- **Context lost in async work.** A thread pool or a Celery task runs with no tenant, or worse, with the previous task's tenant from a leaked thread-local. Use `contextvars`, reset in `finally`, and pass tenant IDs explicitly across processes.
- **Tenant ID as a metric label.** 20,000 tenants times every metric explodes Prometheus cardinality. Put `tenant_id` in logs and traces; in metrics, label by tier or only the top N tenants.
- **Large tenants skew everything.** One tenant with 40% of rows changes query plans, partition sizes and cache hit ratios. Watch per-tenant resource use and have a playbook to move a tenant.
- **Schema-per-tenant at scale.** Thousands of schemas mean very large catalogs, slow migrations and per-connection memory growth. It works for hundreds of tenants, not hundreds of thousands.
- **Global uniqueness by accident.** `UNIQUE (email)` instead of `UNIQUE (tenant_id, email)` stops two tenants from inviting the same consultant.
- **Deletion that misses stores.** Tenant deleted from Postgres but still in S3, Elasticsearch and the cache. Keep a registry of every store that holds tenant data and drive deletion from it.

## Interview questions

**Silo vs pool: how do you choose?** By isolation requirements, cost per tenant and tenant count. Pool is cheapest and easiest to operate for many small tenants; silo gives strong isolation, per-tenant keys and residency at higher cost. Most products are bridge: pooled by default, silo as an enterprise tier, routed by a tenant directory.

**How do you prevent cross-tenant data leaks in a pooled database?** Resolve the tenant from verified identity, carry it in request context that fails closed, scope every query through a tenant-aware data layer, back it with database row-level security, include tenant in cache keys, and test with two tenants.

**How do you handle a noisy neighbor?** Detect with per-tenant usage metrics, then contain: per-tenant rate limits and concurrency caps, fair queuing for background jobs, statement timeouts, and if it persists, move the tenant to dedicated resources. Shuffle sharding limits how many others a bad tenant can affect.

**How does tenant context reach a background job?** Explicitly: pass the tenant ID as a task argument or message header, and restore it in the worker before running the task. Don't rely on ambient context crossing process boundaries.

**How do you run a schema migration across 2,000 tenant databases?** A resumable runner that tracks version per tenant, runs in batches with bounded concurrency, starts with canary tenants, and halts on error rates. Code must support both old and new schema during the rollout (expand and contract).

**How would you move a large tenant to its own database with no downtime?** Snapshot plus change data capture filtered by tenant, verify, a short per-tenant write freeze, flip the tenant directory entry, and keep the source read-only for rollback. Globally unique IDs make this possible without rewriting keys.

**What are cells and why use them?** Independent copies of the full stack, each serving a subset of tenants. They limit blast radius, bound database size, and let you scale by adding cells. The cost is a routing layer and cross-cell operations (global admin, cross-tenant analytics).

## Further reading

- AWS Well-Architected Framework, SaaS Lens, and the AWS whitepaper "SaaS Tenant Isolation Strategies".
- Amazon Builders' Library, "Workload isolation using shuffle-sharding".
- Microsoft Azure Architecture Center, "Architect multitenant solutions on Azure".
- PostgreSQL documentation, "Row Security Policies".
- Citus documentation on multi-tenant applications and tenant isolation.
