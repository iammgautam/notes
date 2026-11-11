---
title: Scaling basics
section: hld
order: 3
summary: Vertical vs horizontal scaling, stateless services, read replicas, and the usual path from one box to a sharded system.
---

## What it is

Scaling means handling more load (requests, data, users) while keeping latency and availability targets.

- **Vertical (scale up):** bigger machine. More CPU, RAM, faster disk.
- **Horizontal (scale out):** more machines behind a load balancer, or data split across nodes.

## When to use which

| | Vertical | Horizontal |
|---|---|---|
| Effort | Change instance size | Architecture change (stateless, LB, sharding) |
| Ceiling | Largest instance available | Practically unbounded |
| Failure | Single point of failure | Survives node loss |
| Cost curve | Superlinear at the top end | Roughly linear |
| Best for | Databases early on, quick wins | Stateless app tier, caches, huge datasets |

> **Rule of thumb:** Scale the app tier horizontally from day one (it is cheap). Scale the database vertically as long as you can; sharding is expensive.

## Stateless services

A service is stateless when any instance can serve any request. State lives elsewhere:

- Sessions → Redis or signed tokens (JWT), not process memory.
- Uploads → object storage, not local disk.
- Background work → a queue, not in-process threads.
- Config → env vars / config service.

Benefits: autoscaling, rolling deploys, no sticky sessions, any instance can die.

## Read replicas

- Primary takes writes; replicas stream the WAL and serve reads.
- Good for read-heavy workloads (feeds, dashboards, reports).
- **Replication lag** means stale reads. Fix with read-your-writes: route a user's reads to the primary for a few seconds after they write, or read by LSN.
- Replicas do not help write throughput.
- See [Replication and sharding]({{ '/hld/replication-and-sharding/' | relative_url }}).

## Typical evolution of a system

```text
Stage 1   app + DB on one box
Stage 2   app box  ->  separate DB box
Stage 3   LB -> N stateless app boxes -> DB
Stage 4   + cache (Redis) + CDN for static/media
Stage 5   + read replicas, async workers via queue
Stage 6   + split by function (services / separate DBs)
Stage 7   + shard the biggest tables by tenant/user
Stage 8   + multi-region (read local, write home region)
```

```text
          +-----+       +---------------------+
client -> | CDN |  LB ->| app x N (stateless) |
          +-----+       +---------------------+
                           |      |       |
                         Redis  queue   Postgres primary
                                  |       |        \
                               workers  replica   replica
```

Each step should be triggered by a measured bottleneck: CPU on app boxes, DB CPU/IO, connection count, replication lag, storage size.

## Trade-offs

| Technique | Gains | Costs |
|---|---|---|
| Cache | Latency, DB offload | Staleness, invalidation |
| Read replicas | Read throughput | Lag, stale reads |
| Async queue | Smooths spikes, faster responses | Eventual consistency, retry logic |
| Sharding | Write throughput, data size | Cross-shard queries, rebalancing |
| Microservices | Team/deploy independence | Network failures, distributed data |

## Gotchas

- **Connection limits:** 50 app pods × 20 connections = 1000 Postgres connections. Use PgBouncer (transaction pooling).
- Autoscaling the app tier just moves the bottleneck to the DB.
- Local caches on N instances diverge; invalidate centrally or keep TTLs short.
- Cron jobs on every instance run N times. Use a leader or a scheduler with locking.

> **Interview tip:** Name the bottleneck before adding a component. "DB CPU is at 80% from reads, so add a cache for the hot keys and one read replica" beats "add Kafka".
