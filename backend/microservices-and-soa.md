---
title: Microservices and SOA
section: backend
order: 2
phase: "Phase 1 · Backend architecture"
summary: How microservices differ from SOA, why each service must own its data, how to query and write across services, and how to spot a distributed monolith.
---

Microservices trade in-process calls and a shared database for independent deployability. That trade only pays off when services are actually independent: each owns its data, ships on its own schedule and survives the others failing. When they aren't, you pay for the network, the operational overhead and eventual consistency, and you still coordinate every release. Most interview questions on this topic are really about that line: what makes a service independent, and what it costs.

## Microservices architecture

Lewis and Fowler's 2014 description is still the reference: an application built as a suite of small services, each running in its own process, communicating over lightweight mechanisms (HTTP, gRPC, messaging), built around business capabilities, independently deployable through automation, with decentralized data management and minimal central governance.

The properties that matter in practice:

- **Independently deployable.** You can ship service A without touching B. If you can't, nothing else on this list helps.
- **Owns its data.** No other service reads or writes its tables.
- **Organized around a business capability** (payments, catalog, fulfillment), not a technical layer or a noun with CRUD.
- **Owned by one team.** Conway's law says your architecture mirrors your communication structure, so draw team and service boundaries together.
- **Smart endpoints, dumb pipes.** Logic lives in services, while the transport (HTTP, a broker) only moves bytes.
- **Designed for failure.** Every remote call has a timeout, a retry policy and a fallback.

### How it works underneath

```text
  clients
     |
  +--v--------------+
  |  API gateway    |  authn, routing, rate limits
  +--+---------+----+
     |         |
  +--v-----+ +-v--------+  events   +-------------+
  | orders | | payments |---------->|   broker    |
  +--+-----+ +--+-------+           |  (Kafka)    |
     |          |                   +------+------+
  [orders    [payments                     |
    DB]        DB]                  +------v------+
                                    |  inventory  |--[inventory DB]
                                    +-------------+
```

Each box is its own repo or directory, pipeline, container image, database and on-call rotation. Around them you now need machinery a monolith never needed: an [API gateway]({{ '/backend/api-gateway-and-bff/' | relative_url }}), [service discovery]({{ '/backend/service-discovery-and-mesh/' | relative_url }}), [distributed tracing]({{ '/backend/distributed-tracing-and-opentelemetry/' | relative_url }}), centralized logs, contract tests, [retries with backoff]({{ '/backend/retries-and-backoff/' | relative_url }}), [circuit breakers]({{ '/backend/fault-tolerance-patterns/' | relative_url }}) and [idempotency]({{ '/backend/idempotency/' | relative_url }}) everywhere.

Services talk in two styles:

| | Synchronous (REST, gRPC) | Asynchronous (events, commands on a broker) |
|---|---|---|
| Caller waits | Yes | No |
| Temporal coupling | Both must be up | Consumer can be down |
| Latency | Adds per hop | Off the request path |
| Consistency | Immediate read of the owner | Eventual |
| Failure handling | Timeouts, retries, breakers | Redelivery, DLQ, idempotent consumers |
| Good for | Queries, user-facing reads | Side effects, fan-out, integration |

### When to use it

- Many teams whose deploys block each other even with trunk-based development and feature flags. The driver is usually organizational.
- Parts with very different scaling or runtime profiles: GPU inference, video transcoding, a high-QPS read path next to a low-QPS admin path.
- Isolation needs: limit the blast radius, keep PCI or HIPAA scope small, give a critical path a stricter availability tier.
- A component that needs a different language or ecosystem.

### When not to

- A small team or an unclear domain. Boundaries you get wrong are expensive to move across a network. Start with a [modular monolith]({{ '/backend/monolith-and-modular-monolith/' | relative_url }}).
- Most operations need strong consistency across what would become several services.
- There's no platform: no CI/CD per service, no tracing, no on-call ownership.

The fallacies of distributed computing are the bill: the network is not reliable, latency is not zero, bandwidth is not infinite, the network is not secure, topology changes, there are many administrators, transport has a cost, and the network is not homogeneous.

Real-world examples: Amazon's move to services owned by small teams is widely cited, and Netflix publicly described moving from a monolithic datacenter application to microservices on AWS after a major database corruption incident in 2008. Uber publicly described growing to thousands of microservices and then grouping them into "domains" (DOMA) to tame the complexity. Segment publicly described consolidating over a hundred destination microservices back into one service in 2018, because the operational cost outweighed the isolation benefit.

## Service-oriented architecture (SOA)

SOA is the enterprise architecture style of the early 2000s. Business functions were exposed as reusable services with formal contracts (WSDL, SOAP over XML, the WS-* standards) and integrated through an **enterprise service bus** (ESB): a central middleware layer that routes messages, transforms formats, orchestrates multi-step processes (often in BPEL) and bridges protocols between mainframes, ERP, CRM and in-house apps.

### How it works

- **Smart pipes.** The ESB holds routing, transformation and orchestration logic. Services are often thin wrappers over existing systems.
- **Canonical data model.** One enterprise-wide schema for `Customer`, `Order` and so on, which every service maps to.
- **Reuse as the goal.** Build a `CustomerService` once and have every application in the company call it.
- **Central governance.** An architecture group owns contracts, standards and the bus.
- **Shared data is common.** Many services sit on the same enterprise databases.

### When it fits today

Integrating many off-the-shelf systems you don't control (SAP, Salesforce, legacy mainframes), especially in regulated enterprises. Modern integration platforms (iPaaS) are its descendants. For a single product built by your own teams, the microservices approach usually fits better.

### SOA vs microservices

| | SOA | Microservices |
|---|---|---|
| Scope | Enterprise-wide integration | One application or product |
| Pipes | Smart ESB with logic | Dumb transport, logic in services |
| Contracts | WSDL/SOAP, canonical model | REST, gRPC, events, per-service models |
| Data | Often shared databases | Database per service |
| Governance | Central | Decentralized, per team |
| Granularity | Coarse | Finer, around one capability |
| Deployment | Often shared app servers | Independent containers |
| Optimizes for | Reuse | Independent change |

Microservices are sometimes called "SOA done right". The defining differences are the dumb pipes and the data ownership. Watch for the modern version of the ESB mistake: business logic creeping into the API gateway, the service mesh or a shared "orchestration" service.

## Data ownership per service

The rule: **a service's persistent data is private to it**. Other services get at it only through its API or the events it publishes. A shared database means a shared schema, and a shared schema means a column rename in one team's migration breaks another team's deploy. It also means anyone can write rows that skip the owner's invariants.

Isolation comes in levels:

| Level | Isolation | Cost | Notes |
|---|---|---|---|
| Private tables, shared DB | Convention only | Lowest | Easy to cheat with a join |
| Schema per service | Enforced by DB roles | Low | Same server, shared resources |
| Database per service | Strong | Medium | Separate backups, scaling, upgrades |
| Different engine per service | Strong | Highest | Polyglot persistence, more to operate |

Schema per service on one Postgres cluster is a common, cheap starting point. Illustrative:

```sql
-- each service connects as its own role and owns one schema
CREATE ROLE orders_svc LOGIN PASSWORD 'from-secret-manager';
CREATE SCHEMA orders AUTHORIZATION orders_svc;
ALTER ROLE orders_svc SET search_path = orders;

CREATE ROLE payments_svc LOGIN PASSWORD 'from-secret-manager';
CREATE SCHEMA payments AUTHORIZATION payments_svc;
ALTER ROLE payments_svc SET search_path = payments;

-- as orders_svc:  SELECT * FROM payments.charge;
-- ERROR:  permission denied for schema payments
```

### Reading across services

- **API composition.** A composer (gateway, BFF or the calling service) calls each owner and joins in memory. Simple and always fresh. Latency is the slowest call, and availability is the product of all of them.
- **Replicated read models.** A service subscribes to the owner's events and keeps its own denormalized copy, such as a search service indexing product and stock events into Elasticsearch. Fast and resilient, eventually consistent. This is CQRS across services ([Event sourcing and CQRS]({{ '/backend/event-sourcing-and-cqrs/' | relative_url }})).
- **Snapshots as domain data.** An order should store the product name and price at purchase time anyway, so copying them is correct modeling rather than duplication.
- **Analytics.** Stream changes into a warehouse with CDC or events. Never let reporting jobs read other services' databases.

### Writing across services

There's no ACID transaction across services. Use a **saga**: a sequence of local transactions with compensating actions, either orchestrated by a coordinator or choreographed through events. Publish events with a **transactional outbox** so state changes and events never disagree, and make every consumer idempotent. See [Distributed transactions]({{ '/backend/distributed-transactions/' | relative_url }}) and [Event-driven architecture]({{ '/backend/event-driven-architecture/' | relative_url }}).

## The distributed monolith anti-pattern

A distributed monolith is a set of separately deployed services so coupled that they must change, deploy and fail together. It has every cost of microservices (network, ops, eventual consistency) and keeps every cost of a monolith (coordination, shared blast radius).

| Symptom | What it looks like | Root cause |
|---|---|---|
| Lockstep deploys | "Deploy users, then orders, then billing" | Unversioned, breaking contract changes |
| Shared database | Services join each other's tables | No data ownership |
| Synchronous chains | A calls B calls C calls D per request | Boundaries cut through one workflow |
| Shared domain library | `common-models` bumped in every service | Shared model instead of contracts |
| Chatty calls | N+1 over HTTP, 40 calls per page | Services split by entity, not capability |
| Entity services | `CustomerService` that is just CRUD | Logic lives in some orchestrator |
| Cascading failures | One slow service takes down all | No timeouts, bulkheads or async paths |

The arithmetic of synchronous chains is unforgiving. If a request needs five services in series and each is available 99.9% of the time, the request is available `0.999^5` ≈ 99.5%: about 3.6 hours of failure a month instead of 43 minutes. Tail latency compounds too. If each call has a 1% chance of being slow, a request that fans out to 100 calls is slow about 63% of the time (Dean and Barroso, "The Tail at Scale").

Fixes, roughly in order of payoff:

1. **Merge services that always change together.** Merging is a valid fix, not a defeat.
2. **Redraw boundaries around capabilities** using bounded contexts ([Domain-driven design]({{ '/backend/domain-driven-design/' | relative_url }})).
3. **Take the request path off synchronous chains.** Consume events into local read models so the service can answer alone.
4. **Version contracts.** Make additive changes, use expand and contract, and run consumer-driven contract tests such as Pact in CI.
5. **Remove shared database access** service by service.
6. **Keep shared libraries technical**: logging, tracing, auth middleware. No domain models.

## In practice

### Runnable: availability and tail-latency math

The numbers above are worth being able to derive on a whiteboard. The demo checks the formulas against a simulation.

```python
import math
import random


def serial_availability(availabilities: list[float]) -> float:
    """All calls on the request path must succeed."""
    return math.prod(availabilities)


def p_any_slow(fanout: int, p_slow: float) -> float:
    """Chance that at least one of `fanout` calls lands in the slow tail."""
    return 1 - (1 - p_slow) ** fanout


def downtime_minutes_per_month(availability: float) -> float:
    return (1 - availability) * 30 * 24 * 60


def simulate(availabilities: list[float], trials: int, seed: int = 7) -> float:
    rng = random.Random(seed)
    ok = sum(all(rng.random() < a for a in availabilities) for _ in range(trials))
    return ok / trials


if __name__ == "__main__":
    chain = [0.999] * 5
    a = serial_availability(chain)
    assert abs(a - 0.99501) < 1e-5
    assert abs(simulate(chain, 200_000) - a) < 0.002

    assert round(downtime_minutes_per_month(0.999)) == 43
    assert round(downtime_minutes_per_month(a)) == 216

    # Make payments and inventory async: only orders is on the request path.
    assert serial_availability([0.999]) > a

    assert round(p_any_slow(100, 0.01), 2) == 0.63
    print(f"5-hop chain: {a:.5f}, ~{downtime_minutes_per_month(a):.0f} min/month down")
```

### A service client with timeouts and bounded retries

Every remote call needs a connect and read timeout. Without them a slow dependency holds your worker threads until the whole pool is stuck. Retry only idempotent requests, and only a small number of times. Illustrative (`requests` with urllib3's `Retry`; parameter names are version-sensitive):

```python
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

retry = Retry(
    total=2,
    backoff_factor=0.2,
    status_forcelist=(502, 503, 504),
    allowed_methods=["GET"],  # never blindly retry a POST
)
session = requests.Session()  # one per process: reuses TCP connections
session.mount("http://", HTTPAdapter(max_retries=retry, pool_maxsize=20))


def get_stock(sku: str, request_id: str) -> dict:
    resp = session.get(
        f"http://inventory.internal/v1/skus/{sku}/stock",
        timeout=(0.5, 2.0),  # connect, read in seconds
        headers={"X-Request-ID": request_id},
    )
    resp.raise_for_status()
    return resp.json()
```

### Extracting a service from a Django monolith

1. Modularize first, so the target module has an `api.py` and owns its tables.
2. Stand up the new service behind the gateway and route one slice of traffic to it ([strangler fig]({{ '/backend/monolith-and-modular-monolith/' | relative_url }})).
3. Swap the monolith's in-process calls to that module for an HTTP or gRPC client with the same interface.
4. Move the data: backfill, CDC while the monolith still writes, flip the writer, drop the old tables.
5. Turn each former cross-module transaction into a saga with an outbox.

## Pitfalls

- **Microservices on day one.** Boundaries chosen before the domain is understood end up wrong, and moving them across a network is a migration project. Symptom: most features touch three or more services.
- **Nano-services.** One service per entity or per endpoint. The call graph explodes, and so do latency and on-call load.
- **No timeouts.** A slow dependency exhausts thread or connection pools upstream and the outage cascades. Set explicit timeouts on every client and keep them shorter than the caller's own timeout.
- **Retry storms.** Retries at every layer multiply: three layers each making three attempts send up to 27 calls to the bottom service while it is already struggling. Retry at one layer, with jitter and a budget.
- **No correlation IDs or tracing.** An error in service D can't be tied to the request in service A. Propagate trace context everywhere ([Observability and tracing]({{ '/llm/observability-and-tracing/' | relative_url }})).
- **Breaking API changes.** A renamed field in a response breaks consumers you didn't know about. Make additive changes, version the API and run contract tests.
- **Tenant context lost between hops.** In multi-tenant SaaS, every call and event must carry the tenant ID from the authenticated token, never from client input ([Multi-tenant architecture]({{ '/backend/multi-tenant-architecture/' | relative_url }})).
- **Read-your-writes surprises.** A user creates something and the list page, fed by an eventually consistent read model, doesn't show it. Read from the owner right after a write, or show the pending state in the UI.

## Interview questions

**How do microservices differ from SOA?**
SOA integrates an enterprise through a smart ESB with canonical models, central governance and often shared databases, aiming for reuse. Microservices put the logic in services, use dumb pipes, give each service its own data and its own team, and aim for independent change.

**How does a service get data owned by another service?**
Call the owner's API (API composition), or subscribe to the owner's events and keep a local read model. Never read its database. Choose by freshness needs: composition is fresh but adds latency and failure modes, while a read model is fast and resilient but eventually consistent.

**What is a distributed monolith and how do you fix it?**
Services that must deploy and fail together: shared database, lockstep releases, synchronous call chains, shared domain libraries. Merge services that always change together, redraw boundaries around capabilities, replace synchronous chains with events and local data, and version contracts.

**When would you not use microservices?**
A small team, an early product, an unclear domain, workloads that need cross-entity strong consistency, or no platform for CI/CD, tracing and on-call. A modular monolith gets most of the benefit at a fraction of the cost.

**How do you keep data consistent across services without 2PC?**
Sagas of local transactions with compensations, a transactional outbox for reliable events, idempotent consumers, and reconciliation jobs to catch drift.

**A request passes through six services and p99 is bad. What do you do?**
Trace it to find the critical path. Parallelize independent calls, cache or replicate data so fewer hops are needed, move side effects to async events, set per-hop timeouts that fit inside the overall budget, and consider hedged requests for idempotent reads. If the six services always change together, merge them.

**How big should a service be?**
Big enough to own one business capability end to end with its data, small enough for one team to own. Use bounded contexts as the first cut. If a feature routinely needs synchronized changes in several services, the boundaries are wrong.

## Further reading

- James Lewis and Martin Fowler, "Microservices" (martinfowler.com, 2014).
- Sam Newman, *Building Microservices*, 2nd edition (O'Reilly, 2021).
- Chris Richardson, *Microservices Patterns* (Manning, 2018).
- Jeffrey Dean and Luiz André Barroso, "The Tail at Scale" (Communications of the ACM, 2013).
- Thomas Erl, *SOA: Principles of Service Design* (Prentice Hall, 2007).
