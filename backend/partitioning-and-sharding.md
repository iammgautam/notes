---
title: Partitioning and sharding
section: backend
order: 13
phase: "Phase 2 · Databases"
summary: Split tables by range, list or hash inside one database, then across many with shard keys, routing, cross-shard queries, resharding, Vitess and Citus.
---

Eventually one table becomes too big to vacuum, index or delete from comfortably, or the dataset outgrows what one primary can write. Partitioning splits a table into pieces inside one database; sharding splits data across database servers. Partitioning is a local decision you make in a migration. Sharding changes how every query, transaction, constraint and deploy works, so it's the last scaling step, not the first.

| Term | What is split | Where the pieces live | Main goal |
|---|---|---|---|
| Horizontal partitioning | Rows | Same server | Manageability, pruning, retention |
| Vertical partitioning | Columns (or tables by domain) | Same or different server | Smaller hot rows, isolation |
| Sharding | Rows | Different servers | Write throughput and storage beyond one node |
| Replication | Nothing; full copies | Different servers | Read scaling and availability ([Replication]({{ '/backend/replication/' | relative_url }})) |

## Database partitioning

### Horizontal partitioning

Rows are divided among partitions by a **partition key**; every partition has the same columns. In PostgreSQL the parent holds no data and routes inserts; each partition is a real table with its own indexes, vacuum state and statistics.

### Vertical partitioning

Columns are split into tables that share a primary key. Move large, rarely-read columns (`raw_payload`, an embedding) off a hot table so list queries read narrow rows that fit in cache. TOAST already stores large values out of line, but a side table also lets you place them on other storage or in another service. The term also covers splitting a database by domain (billing in one database, catalog in another), a step toward [microservices]({{ '/backend/microservices-and-soa/' | relative_url }}).

### Range partitioning

Each partition owns a contiguous key range, typically time: one partition per month for `events`. It's the default for append-heavy data with retention: dropping last year is twelve instant `DROP TABLE`s instead of a `DELETE` that runs for hours and leaves bloat.

### List partitioning

Each partition owns an explicit set of values (`FOR VALUES IN ('eu')`). Useful for data residency, isolating a few large tenants, or lifecycle states (`active` vs `archived`).

### Hash partitioning

Rows go to `hash(key) mod N` (`FOR VALUES WITH (MODULUS 16, REMAINDER 0)`). It spreads data evenly and bounds each partition's size and vacuum time, e.g. 16 partitions by `tenant_id`. No pruning for ranges, and changing `N` rewrites the data.

### Partition pruning

Pruning skips partitions that can't contain matching rows.

- **Plan-time pruning** when the `WHERE` clause compares the partition key to constants.
- **Execution-time pruning** (PostgreSQL 11+) for parameters, `now()`, or values coming from a join, shown as `Subplans Removed` in `EXPLAIN`.

The query must filter on the key in a form the planner understands: `created_at >= $1 AND created_at < $2` prunes; `date_trunc('day', created_at) = $1` may not. A query without the key scans every partition, slower than one big table.

### Rules and limits

- Primary keys and unique constraints must include the partition key; there are no global indexes, so `UNIQUE (email)` on a table partitioned by `created_at` is impossible.
- Keep partition counts in the hundreds or low thousands; planning and locking cost grows with unpruned partitions.
- Create future partitions ahead of time (cron job or `pg_partman`). Avoid a `DEFAULT` partition, or keep it empty: adding a partition has to scan it.
- `DETACH PARTITION ... CONCURRENTLY` (PostgreSQL 14+) avoids blocking queries; it can't run in a transaction block or with a default partition.

Partitioning is not a general speedup; a well-indexed 500-million-row table often beats a badly partitioned one. Partition for retention, when vacuum or index builds on one table hurt, or when nearly every query filters on the key. Details in [Table partitioning]({{ '/postgresql/partitioning/' | relative_url }}).

## Sharding

Sharding is horizontal partitioning across database servers, each shard usually with its own replicas. It's how a traditional database scales **writes** and storage beyond one machine.

### What to try before sharding

1. Fix queries and indexes; "at capacity" is often a handful of bad queries.
2. Scale up: many cores, hundreds of GB of RAM and NVMe go a long way.
3. Offload reads to replicas and caches ([Caching patterns]({{ '/backend/caching-patterns/' | relative_url }})).
4. Partition big tables and archive cold data.
5. Split by domain: separate databases for unrelated parts of the system.

Shard when write throughput, storage or one primary's blast radius is still the bottleneck, or residency forces data into separate regions.

### Choosing a shard key

The shard key decides which shard owns a row and is very hard to change later. Choose it from the access patterns:

- **Query isolation:** most queries, and all hot ones, include the key, so they hit one shard. In B2B SaaS that's `tenant_id`; in a consumer app often `user_id`.
- **High cardinality, even distribution:** far more values than shards, none carrying a large share of traffic.
- **Immutable:** changing a row's key means moving it between shards.
- **Transaction scope:** rows that change atomically together share a key value, so transactions stay on one shard.

### Range sharding

Each shard owns a key range (`A–F`, `G–M`, or ID ranges). Range scans stay on one or a few shards, and ranges split as they grow (Bigtable, HBase, CockroachDB). The weakness is skew: a monotonically increasing key (timestamp, auto-increment ID) sends every new write to the last range.

### Hash sharding

Shard = `hash(key)` mapped to a shard. Load spreads evenly, but range queries must ask every shard. `hash mod N` over `N` servers is a trap: adding one server remaps almost every key. Use [consistent hashing]({{ '/backend/consistent-hashing/' | relative_url }}) (Cassandra) or a fixed number of logical shards, far more than servers, mapped to servers by a table.

### Directory-based sharding

A lookup service maps each key or logical shard to a location. It's the most flexible: move one tenant to a dedicated server, keep EU tenants in an EU region, rebalance shard by shard. Costs: an extra lookup per request (cache it in the router), and the directory must be highly available and consistent. Real systems usually combine them: hash into a logical shard, then a directory places logical shards on servers.

```text
                     application servers
                            |
                            v
          +----------------------------------+    +------------------+
          | router: VTGate, Citus node, or   |<-->| shard map        |
          | app library                      |    | tenant -> bucket |
          +---+-------------+-------------+--+    | bucket -> node   |
              |             |             |       | pins: t42 -> D   |
              v             v             v       +------------------+
         +---------+   +---------+   +---------+   +---------+
         | node A  |   | node B  |   | node C  |   | node D  |
         | 0-341   |   | 342-682 |   | 683-1023|   | tenant  |
         | +replica|   | +replica|   | +replica|   | 42 only |
         +---------+   +---------+   +---------+   +---------+
           logical shards (buckets) per node
```

### Hotspots

A hotspot is a shard with a disproportionate share of traffic, capping the system at one node's capacity.

| Cause | Example | Fix |
|---|---|---|
| Skewed key value | One enterprise tenant is 30% of load | Pin it to a dedicated shard via the directory |
| Monotonic key with range sharding | All inserts go to the newest range | Hash the key, or lead with a high-cardinality column |
| Celebrity / viral entity | Millions read or write one item | Cache reads; spread writes with a key suffix (`item#0..9`) and merge on read |
| Uneven logical shards | Bad hash, few buckets | More buckets, better hash, rebalance |

Track QPS, CPU and storage per shard and per top key; cluster averages hide hotspots.

### Cross-shard queries and joins

A query without the shard key becomes **scatter-gather**: sent to every shard, results merged. The slowest shard sets latency, load multiplies by shard count, and `ORDER BY ... LIMIT 20` needs each shard's top 20. Deep `OFFSET` pagination is especially expensive.

Joins are local only if both sides live on the same shard. Strategies:

- **Co-location:** shard related tables by the same key (`orders` and `order_items` by `tenant_id`).
- **Reference tables:** small, rarely-changing tables (plans, countries, feature flags) replicated to every shard.
- **Denormalize:** copy the few fields you need onto the sharded table ([Relational design and normalization]({{ '/backend/relational-design-and-normalization/' | relative_url }})).
- **Application-side join:** fetch from one shard, then batch-fetch by IDs from others.
- **Offload analytics:** cross-tenant reports go to a warehouse fed by CDC.

**Global uniqueness** across shards (unique email when sharding by tenant) needs a separate lookup table sharded by the unique value itself; Vitess calls this a lookup vindex. **Global IDs** can't come from per-shard sequences; use Snowflake-style IDs, UUIDv7, or embed the shard in the ID. Instagram publicly described IDs made of 41 bits of milliseconds, 13 bits of logical shard ID and 10 bits of per-shard sequence.

**Cross-shard transactions** need two-phase commit or a saga, both slower and more fragile than local commits; choose the shard key so they're rare. See [Distributed transactions]({{ '/backend/distributed-transactions/' | relative_url }}).

### Resharding

Shards fill up and load shifts. With `hash mod N`, adding a server relocates most rows; with logical shards, you move whole buckets and nothing is rehashed. Instagram publicly described thousands of logical shards (Postgres schemas) on a few physical servers for this reason.

An online move of a bucket or tenant:

1. **Copy:** snapshot to the target and stream changes from that point (logical replication, CDC, Vitess VReplication).
2. **Verify:** compare counts and checksums (Vitess VDiff).
3. **Cut over reads**, then **writes**: briefly block writes for the moved keys, let the target catch up, flip the directory. Routers must drop their cached shard map, or writes land on the old shard.
4. **Keep a rollback path:** reverse replication until you're confident.
5. **Clean up** the old copy.

### Vitess and Citus

**Vitess** shards MySQL. It started at YouTube, is a graduated CNCF project, and is publicly used by companies such as Slack; PlanetScale is built on it. Apps connect to **VTGate**, a stateless MySQL-protocol proxy that routes each query using the **VSchema**. Each table has a primary **vindex** that maps the sharding column to a keyspace ID; shards own keyspace ID ranges, named like `-80` and `80-`. A **VTTablet** sidecar manages each MySQL instance; a topology service (etcd, ZooKeeper or Consul) holds metadata. Resharding runs as VReplication workflows (`Reshard`, `MoveTables`) with reversible `SwitchTraffic` cutover.

**Citus** is a PostgreSQL extension that turns a group of Postgres servers into a distributed database. Each distributed table is split by a **distribution column** into hash-range shards (32 by default) across worker nodes. Tables distributed on the same column type and shard count are **co-located**, so joins and transactions on that column stay on one node. Small tables become **reference tables** copied to every node. Multi-shard queries run in parallel, multi-node writes use two-phase commit, and a rebalancer moves shards online. Recent versions add schema-based sharding for schema-per-tenant designs. It fits multi-tenant SaaS sharded by `tenant_id`; see [Multi-tenant architecture]({{ '/backend/multi-tenant-architecture/' | relative_url }}).

| | Vitess | Citus | App-level sharding |
|---|---|---|---|
| Database | MySQL | PostgreSQL | Any |
| Routing | VTGate proxy | Coordinator / any node | Your code |
| Resharding | VReplication workflows | Shard rebalancer | You build it |
| Cross-shard queries | Supported, scatter | Parallel distributed SQL | You build it |
| Cost | Operating a new control plane | Extension plus cluster ops | Engineering time, forever |

## In practice

Monthly range partitions for an events table (illustrative). The primary key includes the partition key, as PostgreSQL requires:

```sql
CREATE TABLE events (
  tenant_id   bigint      NOT NULL,
  id          bigint      NOT NULL,     -- app-generated, e.g. Snowflake-style
  created_at  timestamptz NOT NULL,
  kind        text        NOT NULL,
  payload     jsonb,
  PRIMARY KEY (tenant_id, created_at, id)
) PARTITION BY RANGE (created_at);

CREATE TABLE events_2026_11 PARTITION OF events
  FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE events_2026_12 PARTITION OF events
  FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');

-- prunes to one partition
EXPLAIN SELECT count(*) FROM events
WHERE tenant_id = 42
  AND created_at >= '2026-11-10' AND created_at < '2026-11-11';

-- retention: detach without blocking, then drop
ALTER TABLE events DETACH PARTITION events_2025_11 CONCURRENTLY;
DROP TABLE events_2025_11;
```

Sharding a multi-tenant schema with Citus (illustrative; check function names against your Citus version):

```sql
SELECT create_reference_table('plans');
SELECT create_distributed_table('tenants', 'id');
SELECT create_distributed_table('orders', 'tenant_id', colocate_with => 'tenants');
SELECT create_distributed_table('order_items', 'tenant_id', colocate_with => 'orders');

-- single-shard: routed to one worker
SELECT o.id, sum(i.quantity)
FROM orders o JOIN order_items i
  ON i.tenant_id = o.tenant_id AND i.order_id = o.id
WHERE o.tenant_id = 42
GROUP BY o.id;

-- after adding a worker node, move shards in the background
SELECT citus_rebalance_start();
```

The equivalent routing rule in a Vitess VSchema (illustrative):

```json
{
  "sharded": true,
  "vindexes": {
    "hash": {
      "type": "hash"
    }
  },
  "tables": {
    "orders": {
      "column_vindexes": [
        {
          "column": "tenant_id",
          "name": "hash"
        }
      ]
    }
  }
}
```

The resharding math is easy to check. This stdlib model compares `hash mod N` with a logical-shard directory when growing from four to five servers, and pins a hot tenant:

```python
import hashlib
from collections import Counter

LOGICAL_SHARDS = 1024


def stable_hash(key: str) -> int:
    # Python's hash() is randomized per process; routing needs a stable hash.
    return int.from_bytes(hashlib.blake2b(key.encode(), digest_size=8).digest(), "big")


def mod_n(key: str, nodes: list) -> str:
    return nodes[stable_hash(key) % len(nodes)]


class Directory:
    # key -> logical shard (fixed forever) -> physical node (lookup table)
    def __init__(self, nodes):
        self.nodes = list(nodes)
        self.placement = {s: self.nodes[s % len(self.nodes)]
                          for s in range(LOGICAL_SHARDS)}
        self.pinned = {}  # hot tenants moved to a dedicated node

    def node_for(self, key: str) -> str:
        if key in self.pinned:
            return self.pinned[key]
        return self.placement[stable_hash(key) % LOGICAL_SHARDS]

    def add_node(self, node: str) -> int:
        # move just enough logical shards to the new node to rebalance
        self.nodes.append(node)
        target = LOGICAL_SHARDS // len(self.nodes)
        load = Counter(self.placement.values())
        moved = 0
        for shard, owner in sorted(self.placement.items()):
            if moved == target:
                break
            if load[owner] > target:
                self.placement[shard] = node
                load[owner] -= 1
                moved += 1
        return moved


if __name__ == "__main__":
    keys = [f"tenant-{i}" for i in range(20_000)]

    # Naive modulo: going from 4 to 5 nodes relocates ~80% of keys.
    four, five = ["db0", "db1", "db2", "db3"], ["db0", "db1", "db2", "db3", "db4"]
    moved = sum(mod_n(k, four) != mod_n(k, five) for k in keys)
    assert moved / len(keys) > 0.75

    # Logical shards + directory: only shards assigned to db4 move (~20%).
    d = Directory(four)
    before = {k: d.node_for(k) for k in keys}
    shards_moved = d.add_node("db4")
    after = {k: d.node_for(k) for k in keys}
    changed = [k for k in keys if before[k] != after[k]]
    assert shards_moved == LOGICAL_SHARDS // 5
    assert 0.15 < len(changed) / len(keys) < 0.25
    assert all(after[k] == "db4" for k in changed)  # data only flows to db4
    load = Counter(d.placement.values())
    assert max(load.values()) - min(load.values()) <= 1  # balanced

    # Hotspot isolation: pin one huge tenant to its own node.
    d.pinned["tenant-42"] = "db-dedicated"
    assert d.node_for("tenant-42") == "db-dedicated"
    print("percent of keys moved: mod-N", round(100 * moved / len(keys)),
          "directory", round(100 * len(changed) / len(keys)))
```

It prints 80 for modulo and 20 for the directory: the minimum, since the fifth node must take a fifth of the data.

## Pitfalls

- **Sharding too early.** Scatter-gather, distributed transactions, harder migrations and a router, for data one server could hold.
- **Wrong shard key.** Hot queries don't include it, so everything is scatter-gather. Symptom: p99 latency grows with the number of shards. Resharding on a new key takes months, so choose carefully up front.
- **Partitioning without pruning.** Queries don't filter on the partition key or wrap it in a function; every partition is scanned. Check `EXPLAIN` for the partitions touched.
- **Schema migrations across shards.** Success on 30 shards and failure on 2 leaves a mixed schema. Use expand-contract migrations and track per-shard state. See [Database migrations]({{ '/backend/database-migrations/' | relative_url }}).
- **Uneven tenants.** Hash sharding by tenant balances tenant counts, not load. Track per-tenant size and traffic and pin outliers.

## Interview questions

**Q: Partitioning vs sharding?**
Partitioning splits a table within one server, for pruning, retention and maintenance. Sharding puts pieces on different servers to scale writes and storage, and brings routing, cross-node query and transaction problems.

**Q: How do you choose a shard key for a multi-tenant SaaS?**
Usually `tenant_id`: nearly every query includes it, tenant data is co-located so joins and transactions stay local, and isolation is natural. Handle skew by pinning large tenants to dedicated shards through a directory.

**Q: Range vs hash sharding?**
Range keeps neighboring keys together, so range scans are cheap and ranges can split, but monotonic keys create a hot shard. Hash spreads load evenly but turns range queries into scatter-gather and needs consistent hashing or logical shards to avoid remapping on growth.

**Q: How do you reshard without downtime?**
Use many logical shards mapped to servers. Move a shard by copying a snapshot, streaming changes, verifying, briefly pausing writes for that shard, flipping the directory, and keeping reverse replication for rollback.

**Q: How do you handle a query that needs data from all shards?**
Avoid it for hot paths with co-location, reference tables or denormalization. For the rest, scatter-gather with limits pushed down to each shard and merged at the router, or send it to an analytics store fed by CDC.

**Q: How does partition pruning work, and when does it fail?**
The planner compares `WHERE` conditions on the partition key with each partition's bounds and skips non-matching ones, at plan time for constants or execution time for parameters. It fails when the query omits the key or wraps it in a function or cast.

## Further reading

- PostgreSQL documentation, "Table Partitioning" (Data Definition chapter).
- Martin Kleppmann, *Designing Data-Intensive Applications*, chapter 6 (partitioning).
- Vitess documentation: concepts (keyspace, VSchema, vindexes) and resharding guides.
- Citus documentation; Cubukcu et al., "Citus: Distributed PostgreSQL for Data-Intensive Applications" (SIGMOD 2021).
- Corbett et al., "Spanner: Google's Globally-Distributed Database" (OSDI 2012).
