---
title: Replication and sharding
section: hld
order: 6
summary: Leader-follower and multi-leader replication, replication lag, choosing shard keys, resharding and fixing hotspots.
---

**Replication** copies the same data to multiple nodes (availability, read scale). **Sharding** (partitioning) splits data across nodes so each holds a subset (write scale, data size). Real systems use both: each shard is a replicated group.

## Replication models

| Model | Writes go to | Pros | Cons | Examples |
|---|---|---|---|---|
| Leader-follower | One leader | Simple, no write conflicts | Leader is write bottleneck; failover needed | Postgres, MySQL, Redis |
| Multi-leader | Any of several leaders | Local writes per region, offline clients | Write conflicts need resolution | Multi-region MySQL, CouchDB |
| Leaderless | Any N replicas (quorum) | No failover, high write availability | Conflicts, read repair, tunable consistency | Cassandra, DynamoDB |

### Sync vs async

- **Synchronous:** leader waits for follower ack. No data loss on failover, higher write latency, a slow follower stalls writes.
- **Asynchronous:** leader acks immediately. Fast, but recent writes are lost if the leader dies.
- **Semi-sync:** wait for at least one follower (Postgres `synchronous_standby_names` with `ANY 1`).

### Replication lag problems

| Anomaly | Example | Fix |
|---|---|---|
| Read-your-writes | User edits profile, refresh shows old | Read from leader for N s after a write, or wait for replica LSN |
| Monotonic reads | Refresh shows new, then old | Pin a user to one replica |
| Consistent prefix | Reply visible before the question | Keep causally related writes on the same partition |

### Failover

- Detect leader failure (heartbeat timeout), promote the most up-to-date follower, repoint clients.
- Risks: **split brain** (two leaders accept writes), lost async writes, timeouts too short cause flapping.
- Use fencing (epoch numbers) and a consensus-based manager (Patroni + etcd, managed RDS).

### Multi-leader conflict resolution

- Last-write-wins (timestamp): simple, silently drops writes.
- Merge per field, or CRDTs (counters, sets).
- Avoid conflicts: route each record's writes to a home region.

## Sharding

### Strategies

| Strategy | How | Pros | Cons |
|---|---|---|---|
| Range | Key ranges per shard (A–F, G–M) | Range scans | Hotspots on sequential keys (timestamps) |
| Hash | `hash(key) % N` or hash ranges | Even spread | No range scans; naive mod-N reshuffles all on resize |
| Consistent hash | Ring with virtual nodes | Moves ~1/N keys on resize | More moving parts |
| Directory / lookup | Table maps key → shard | Flexible, move tenants individually | Lookup service is a dependency |
| Geo | By region | Data locality, compliance | Uneven region sizes |

### Choosing a shard key

- High cardinality and even distribution.
- Matches the dominant query: most queries should hit **one** shard.
- Stable: changing a row's shard key means moving the row.
- Multi-tenant SaaS: `tenant_id` is the natural key. All tenant data co-locates, joins stay local. Big tenants need special handling.

### Resharding

- Pre-split into many logical shards (e.g. 1024) mapped to fewer physical nodes; move logical shards, not rows.
- Online move: copy snapshot → stream changes (logical replication / CDC) → brief write freeze → flip routing → verify.
- Use consistent hashing or a directory so a resize does not remap everything.

### Hotspots

- One huge tenant or celebrity key overloads a shard.
- Fixes: give big tenants dedicated shards (directory), split hot keys with a suffix (`key#0..9`) and fan-in on read, cache the hot reads.
- Time-based keys: prefix with a hash bucket to spread writes.

## Trade-offs

| Concern | Cost of sharding |
|---|---|
| Cross-shard queries | Scatter-gather, slower, harder pagination |
| Transactions | Need 2PC or sagas across shards |
| Unique constraints | Only enforceable within a shard; global IDs need a generator |
| Operations | Backups, migrations, schema changes × N |

## Gotchas

- Shard as late as possible. First: indexes, vertical scale, replicas, caching, partitioning a big table (Postgres declarative partitioning), archiving.
- Postgres options: Citus (distributed tables by tenant), or app-level routing with a tenant → DB directory.
- Auto-increment IDs collide across shards. Use UUIDv7 / Snowflake-style IDs.
- Replicas are not backups. A bad `DELETE` replicates instantly.

> **Interview tip:** When you shard, always state the shard key, why most queries hit one shard, and what you do with the biggest tenant or user.
