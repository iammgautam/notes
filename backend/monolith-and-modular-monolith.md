---
title: Monoliths and modular monoliths
section: backend
order: 1
phase: "Phase 1 · Backend architecture"
summary: What a monolith really is, how a modular monolith enforces boundaries in one deployable, and how to migrate to services with a strangler fig.
---

Every backend starts as one codebase, one deployable and one database, and most should stay that way far longer than conference talks suggest. The real question is not "monolith or microservices" but whether the boundaries inside the code are real. A monolith with enforced module boundaries can carry hundreds of engineers and be split later along those seams. A monolith without them becomes a big ball of mud that can't be split at all, and services carved out of it become a distributed monolith.

## Monolithic architecture

A monolith is a system built, tested and deployed as a single unit: one artifact (a Docker image, a wheel, a JAR), one release pipeline and usually one primary database. Components talk through in-process function calls and can share one database transaction. "Monolith" describes the deployment unit, not code quality. A monolith can be cleanly modular or a tangle.

Sam Newman distinguishes shapes worth naming in an interview:

| Shape | Deployment | Internal boundaries | Typical outcome |
|---|---|---|---|
| Single-process monolith | One unit | Weak or none | Fast early, slows as the team grows |
| Modular monolith | One unit | Enforced modules, owned data | Scales to many teams |
| Distributed monolith | Many units | Coupled anyway | Costs of both, benefits of neither |

### How it works underneath

You scale a monolith by cloning it: N identical replicas behind a load balancer, all using the same database (the X axis of the scale cube from *The Art of Scalability*). Process roles give you more control than people assume. The same image runs as web workers, as Celery workers on separate queues and as scheduled jobs, and each role scales independently.

```text
                 +------------------+
   clients ----> |  load balancer   |
                 +--------+---------+
                          |
          +---------------+---------------+
          |               |               |
     +----v----+     +----v----+     +----v----+
     | web (1) |     | web (2) |     | web (N) |   same image,
     +----+----+     +----+----+     +----+----+   every module
          |               |               |
          +---------------+---------------+
                          |
           +--------------v---------+    +-------------------------+
           | PostgreSQL (one DB)    |<---| workers: same image,    |
           +------------------------+    | queues: default, heavy  |
                                         +-------------------------+
```

What you get for free:

- **ACID across features.** Placing an order, reserving stock and writing an invoice commit or roll back together.
- **Cheap calls.** A function call costs nanoseconds. A network hop costs a millisecond or more and can fail halfway.
- **One thing to operate.** One deploy, one set of dashboards, one stack trace that shows the whole request.
- **Cheap refactoring.** Moving a function across a boundary is an IDE operation, not an API migration with versioning.

### When to use it

- A new product or an unclear domain. You will draw the wrong boundaries at first, and moving a boundary inside one process is cheap.
- A small to mid-size engineering org (a rough heuristic: under 20–30 engineers, or a handful of teams).
- Workloads that need strong consistency across most features: ledgers, inventory with reservations, bookings.
- Limited platform maturity: no tracing, no per-team on-call, no paved road for new services.

### When it starts to hurt

| Pain | Symptom | First fix, before splitting |
|---|---|---|
| Build and test time | CI takes 40+ minutes | Test selection, parallel shards |
| Deploy contention | Merge queues, weekly release trains | Trunk-based development, feature flags |
| Blast radius | One bad deploy or leak takes everything down | Process roles, canary deploys |
| Uneven scaling | Whole app scaled for one hot endpoint | Separate deployment of the same image |
| Ownership blur | "Who owns this table?" | Modules plus CODEOWNERS |
| Runtime lock-in | Need a GPU or another language | Extract that one piece |

Most have fixes short of microservices. The exceptions are different runtime needs (GPU inference), hard isolation requirements (PCI scope) and more teams than one pipeline can serve.

Real-world examples: Shopify has publicly described modularizing one of the largest Ruby on Rails monoliths rather than breaking it into services. Stack Overflow has publicly described serving its traffic from a monolithic .NET application on a small number of web servers. Amazon Prime Video publicly described (2023) moving a video-quality monitoring service from a distributed serverless design into a single process, cutting its infrastructure cost by about 90%.

### The big ball of mud

The failure mode is not the monolith, it's the lack of structure inside it (Foote and Yoder named it in 1997). Symptoms: any module imports any other, an `Account` model with foreign keys from 60 tables, circular imports patched with function-level imports, and a `utils/` package full of business rules. There are no seams left to cut along, which is what makes extraction impossible later.

## Modular monolith

A modular monolith keeps the single deployable but splits the code into modules aligned with business capabilities (bounded contexts, see [Domain-driven design]({{ '/backend/domain-driven-design/' | relative_url }})). Each module has:

1. **A public API**: functions or service classes plus plain data types (DTOs). Other modules import only this.
2. **Private internals**: models, queries, helpers. Off limits to everyone else.
3. **Its own data**: every table has exactly one owning module. Others never read or write it directly. They call the API or keep their own copy fed by events.
4. **Declared dependencies**: modules form a directed acyclic graph, checked in CI.

### How boundaries are enforced

Without enforcement, boundaries decay within months. The tools:

| Layer | Tool | What it catches |
|---|---|---|
| Code imports | import-linter (Python), Packwerk (Ruby), ArchUnit (Java), Spring Modulith | Imports of another module's internals, cycles |
| Data | Table prefix or Postgres schema per module, review rules | Cross-module joins and foreign keys |
| Ownership | CODEOWNERS per module directory | Unreviewed changes to another team's code |
| Runtime | Separate process roles for heavy modules | One module starving others of CPU or memory |

Modules communicate in two ways. **Synchronous in-process calls** to another module's public API are fine and cheap. **In-process events** decouple side effects: `orders` publishes `OrderPlaced` and `notifications` reacts, without `orders` knowing it exists. For side effects that must survive a crash, write a job or outbox row in the same transaction (see [Event-driven architecture]({{ '/backend/event-driven-architecture/' | relative_url }}) and [Postgres as a job queue]({{ '/postgresql/postgres-as-a-queue/' | relative_url }})).

### When to use it

It's the right default for most teams between roughly 10 and 100 engineers. You get what people want from microservices (ownership, parallel work, explicit contracts) while keeping in-process calls, one deploy, and a cross-module transaction when you genuinely need one. It's also the best preparation for extraction: a module with a narrow API and its own tables can move out in weeks, a tangle takes years.

### When not to

- A module needs a different runtime, scaling unit or failure domain: GPU inference, video transcoding, a PCI-scoped card vault.
- Teams are still blocked on a shared deploy pipeline after trunk-based development and feature flags.
- You need hard resource isolation. A modular monolith still shares a process, so a memory leak in `reports` still kills `checkout` unless you split process roles.

### Monolith, modular monolith, microservices

| | Monolith | Modular monolith | Microservices |
|---|---|---|---|
| Deploy unit | One | One | One per service |
| Boundaries | Convention | Enforced in CI | Enforced by the network |
| Data | Shared freely | Owned per module, one DB | Owned per service, separate DBs |
| Cross-boundary call | Function call | Function call via API | Network call |
| Cross-boundary consistency | ACID | ACID, used sparingly | Sagas, eventual |
| Ops cost | Low | Low | High |
| Cost to move a boundary | Low | Medium | High |

## Migration paths

The safe path is incremental: **monolith, then modular monolith, then extract only the modules that need it**. Big-bang rewrites fail because the old system keeps changing while you rebuild it, and because the rewrite rediscovers every undocumented business rule in production.

### Monolith to modular monolith

1. **Map capabilities.** Run an event storming session or a whiteboard pass and name 5–15 modules after business capabilities (`billing`, `catalog`, `fulfillment`), not technical layers.
2. **Move code, add facades.** Move models and logic into module packages. Add an `api.py` per module that exposes what other modules actually use.
3. **Ratchet the checks.** Turn on import contracts with today's violations listed as ignored. New violations fail CI and the ignore list only shrinks.
4. **Give every table an owner.** Replace cross-module foreign keys and joins with IDs plus API calls or local read models.
5. **Route cross-module writes through the owner**, either by calling its API or by publishing an event it handles.

### Modular monolith to microservices

Extract a module only when it has a concrete reason: independent scaling, a different runtime, a separate failure or compliance domain, or a team whose release cadence is blocked. Because the module already has an API and owns its tables, extraction is mostly mechanical: put a network client behind the same interface, move the tables to a new database, flip callers. Any cross-module transactions you still have become sagas, so list them before you extract (see [Microservices and SOA]({{ '/backend/microservices-and-soa/' | relative_url }}) and [Distributed transactions]({{ '/backend/distributed-transactions/' | relative_url }})).

### Strangler fig

Martin Fowler named this pattern after the strangler fig, a vine that grows around a host tree until it replaces it. You replace a legacy system slice by slice behind a routing facade instead of rewriting it at once.

```text
  step 1            step 2                       step 3
  clients           clients                      clients
     |                 |                            |
 +---v------+   +------v---------+          +-------v--------+
 | monolith |   | routing facade |          | routing facade |
 +----------+   +---+--------+---+          +--+-----+-----+-+
                    |        |                 |     |     |
             /invoices/*   rest              svc A svc B svc C
                    |        |
             +------v--+ +---v------+        monolith deleted
             | billing | | monolith |
             | service | +----------+
             +---------+
```

1. Put a facade in front of the monolith: nginx, an API gateway or the Kubernetes ingress.
2. Build the replacement for one slice: a route group, a tenant cohort, a background job.
3. Shift traffic gradually, by path, by tenant or by percentage, keeping the old path as a fallback.
4. Optionally **parallel run**: call both, serve the old result, log diffs until they stop appearing.
5. Delete the old code. A strangler that never deletes leaves you running two systems forever.

Companion techniques:

- **Branch by abstraction** for logic with no HTTP edge, such as a pricing function called from 40 places: put an interface in front of it, add a second implementation backed by the new service, switch with a flag.
- **Anticorruption layer** so the new service doesn't inherit the legacy model's names and quirks.
- **Change data capture** (CDC) to keep data in sync while both sides are live.

### Moving the data

Code is the easy part. A safe sequence for moving tables:

1. The service reads through the monolith's API. The monolith still owns all writes.
2. The service gets its own database, filled by a backfill plus CDC (for example Debezium reading the Postgres WAL). The monolith is still the writer.
3. Flip the writer. The service becomes the source of truth, and the monolith reads through the service API or a replicated copy.
4. Drop the old tables after a soak period.

Avoid application-level dual writes (save to the old DB, then save to the new one). A crash between the two leaves them diverged with no record of it. Schema changes along the way follow expand and contract ([Zero-downtime migrations]({{ '/postgresql/zero-downtime-migrations/' | relative_url }})).

## In practice

### A Django project laid out as modules

```text
src/shop/
  billing/
    api.py        public: functions and DTOs other modules may call
    events.py     public: events this module publishes
    models.py     private
    services.py   private
  catalog/
  orders/
  kernel/         shared value types only (Money, TenantId), no rules
```

Django apps already give you packages and table prefixes (`billing_invoice`). What they don't give you is privacy: any app can import any model and `select_related` across apps. Make the public surface explicit and return plain data, not ORM instances. A model instance lets callers lazy-load relations and call `.save()`, which quietly bypasses the owner. Illustrative:

```python
# billing/api.py (illustrative)
from dataclasses import dataclass
from decimal import Decimal

from . import services


@dataclass(frozen=True)
class InvoiceDTO:
    id: int
    order_id: int
    total: Decimal
    status: str


def create_invoice(*, tenant_id: int, order_id: int, total: Decimal) -> InvoiceDTO:
    inv = services.create_invoice(tenant_id=tenant_id, order_id=order_id, total=total)
    return InvoiceDTO(id=inv.id, order_id=inv.order_id, total=inv.total, status=inv.status)
```

Store references to other modules' data as plain IDs, not foreign keys:

```python
# orders/models.py (illustrative)
from django.db import models


class Order(models.Model):
    tenant_id = models.BigIntegerField(db_index=True)
    customer_id = models.BigIntegerField()          # owned by accounts
    invoice_id = models.BigIntegerField(null=True)  # owned by billing
    status = models.CharField(max_length=20, default="pending")
```

You give up database-enforced referential integrity across modules. That's deliberate: it's exactly the integrity you would lose on extraction anyway, and the owner's API becomes the place that validates the reference.

### Enforcing boundaries with import-linter

Illustrative config. import-linter runs as `lint-imports` in CI, and the `ignore_imports` list is the ratchet for existing violations:

```ini
# .importlinter (illustrative)
[importlinter]
root_packages =
    orders
    billing
    catalog

[importlinter:contract:billing-private]
name = Only billing.api and billing.events are public
type = forbidden
source_modules =
    orders
    catalog
forbidden_modules =
    billing.models
    billing.services
ignore_imports =
    orders.reports -> billing.models

[importlinter:contract:module-dag]
name = Higher modules may import lower ones, never the reverse
type = layers
layers =
    orders
    billing
    catalog
```

### A runnable boundary checker

The core of what these tools do, in stdlib: parse imports with `ast` and compare them against a map of each module's public surface.

```python
import ast

# Submodules each top-level module exposes to the rest of the codebase.
PUBLIC = {
    "billing": {"api", "events"},
    "orders": {"api", "events"},
    "catalog": {"api"},
}


def imported_paths(source: str):
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if "." in node.module:
                yield node.lineno, node.module
            else:  # "from billing import models" -> billing.models
                for alias in node.names:
                    yield node.lineno, f"{node.module}.{alias.name}"


def violations(owner: str, source: str) -> list[str]:
    errors = []
    for lineno, path in imported_paths(source):
        top, _, rest = path.partition(".")
        if top == owner or top not in PUBLIC or not rest:
            continue
        if rest.split(".")[0] not in PUBLIC[top]:
            errors.append(f"{owner}:{lineno} imports private {path}")
    return errors


if __name__ == "__main__":
    good = "from billing.api import create_invoice\nfrom billing import events\n"
    bad = (
        "from billing.models import Invoice\n"
        "import catalog.pricing\n"
        "from billing import services\n"
    )
    own = "from orders.models import Order\nfrom .models import Line\nimport json\n"

    assert violations("orders", good) == []
    assert violations("orders", own) == []
    errors = violations("orders", bad)
    assert len(errors) == 3, errors
    assert "billing.models" in errors[0] and "catalog.pricing" in errors[1]
    print("\n".join(errors))
```

## Pitfalls

- **Folders, not modules.** Packages without CI checks decay back into a ball of mud. Symptom: cycles in the import graph between "modules". Fix: import contracts with a ratchet.
- **A growing shared package.** `common/` accumulates business rules and becomes the hub every module depends on. Keep the shared kernel to value types and infrastructure helpers.
- **Cross-module foreign keys and joins.** Invisible until extraction, when you find hundreds. Grep for `ForeignKey("otherapp.` and cross-app `select_related`, then replace them with IDs.
- **Django signals as hidden coupling.** A `post_save` receiver in another module runs synchronously inside the sender's transaction, and its exception rolls back the sender's write. Prefer explicit calls, explicit events, or `transaction.on_commit`.
- **Extracting too early.** Splitting along boundaries you haven't validated produces a distributed monolith.
- **Big-bang rewrite.** Two systems diverge for a year, then the cutover weekend fails. Strangle slice by slice, each with its own rollback, and track the traffic still hitting the old path until it reaches zero.
- **Dual writes during migration.** Silent divergence. Use CDC or an outbox, and run a reconciliation job that diffs both sides.

## Interview questions

**When would you choose a monolith over microservices?**
A new product, a small team, an unclear domain, strong consistency needs or low operational maturity. Start with a modular monolith and extract a module only when it has a concrete reason: a different scaling profile or runtime, an isolation or compliance requirement, or a team blocked by shared deploys.

**Can a monolith scale?**
Yes. Clone it horizontally behind a load balancer, add read replicas and caching, run separate worker pools from the same image, and partition data if needed. The limits are usually organizational (deploy contention, build time, ownership) and uneven resource needs, not raw throughput.

**How do you enforce module boundaries in a Django monolith?**
A public `api.py` per app that returns DTOs, import-linter contracts in CI with a shrinking ignore list, one owning module per table with IDs instead of cross-module foreign keys, CODEOWNERS per module, and events for side effects.

**How would you break up a large monolith?**
Modularize first. Pick a seam with clear value and few inbound dependencies. Put a routing facade in front and strangle that slice: code first, then data through backfill plus CDC, flip the writer, delete the old path. Never a big-bang rewrite.

**What's the hardest part of extracting a service?**
The data. Shared tables, joins across what are now service boundaries, transactions that must become sagas, reporting queries that read everything, and keeping both copies consistent during the migration window.

**What are the risks of the strangler fig pattern?**
The facade becomes a bottleneck or single point of failure, data sync between old and new diverges, behavior differs subtly (parallel runs catch this), and the migration stalls at 90% so you run two systems indefinitely.
## Further reading

- Sam Newman, *Monolith to Microservices* (O'Reilly, 2019).
- Martin Fowler, "MonolithFirst" and "Strangler Fig Application" (martinfowler.com).
- Brian Foote and Joseph Yoder, "Big Ball of Mud" (PLoP, 1997).
- Shopify Engineering, "Deconstructing the Monolith: Designing Software that Maximizes Developer Productivity" (2019).
- import-linter documentation (contract types: forbidden, layers, independence).
