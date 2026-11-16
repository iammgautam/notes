---
title: SQL vs NoSQL
section: backend
order: 10
phase: "Phase 2 · Databases"
summary: The main database families, what each data model fits, how they differ on consistency, scaling and query flexibility, and how to choose.
---

"SQL or NoSQL?" is the wrong question. The useful questions are: what shape is the data, how will it be read and written, what must never be wrong, and how big will it get. The boundary has also blurred: PostgreSQL stores documents in JSONB, MongoDB runs multi-document transactions, and distributed SQL systems shard automatically. Picking a store means matching a data model and its guarantees to your access patterns, within the number of systems your team can operate.

## SQL vs NoSQL: what the split means

| Dimension | Relational (SQL) | Typical NoSQL |
|---|---|---|
| Data model | Tables, rows, foreign keys | Key-value, document, wide-column, graph |
| Schema | Enforced on write | Flexible, enforced (if at all) on read |
| Query | Declarative SQL, ad hoc joins | API or query language shaped by the model |
| Transactions | Multi-row, multi-table ACID | Often single-item or single-partition |
| Scaling | Scale up, read replicas, sharding is extra work | Built-in partitioning across nodes |
| Design method | Model the data, then write queries | Model the queries, then lay out the data |

The last row matters most. In a relational database you normalize and the planner answers new questions. In DynamoDB or Cassandra you list every access pattern first and lay out data so each is a single-partition read; a new access pattern later means a new table, index or backfill.

"NewSQL" or distributed SQL (Google Spanner, CockroachDB, YugabyteDB, TiDB) sits in between: SQL and serializable or snapshot transactions, with automatic range sharding and consensus-based replication. You pay for it in write latency (every commit needs a quorum, sometimes across regions) and operational or licensing cost.

## Relational databases

PostgreSQL, MySQL, SQL Server, Oracle. Typed tables, foreign keys, declarative queries and a cost-based planner. Underneath: B-tree indexes, a write-ahead log for durability, and MVCC so readers don't block writers.

- **Use for:** the system of record for business data: users, tenants, orders, payments, permissions. Anything with invariants (unique emails, balances that must not go negative, foreign keys) and anything you will query in ways you can't predict yet.
- **Avoid for:** extremely high write rates of simple items across many nodes, or data whose natural shape is a deep graph traversed many hops deep.
- **Scale ceiling:** higher than most assume; one well-tuned PostgreSQL primary with replicas handles terabytes and tens of thousands of transactions per second. See [Partitioning and sharding]({{ '/backend/partitioning-and-sharding/' | relative_url }}) for what to do past that.

## Key-value stores

Redis, Memcached, Amazon DynamoDB, etcd. The model is a map: `get(key)`, `put(key, value)`, sometimes with range reads over a sort key.

- **Redis** keeps data in memory and offers data structures (hashes, sorted sets, streams). Use it for caches, sessions, rate-limit counters, leaderboards and locks. See [Redis and Memcached]({{ '/backend/redis-and-memcached/' | relative_url }}).
- **DynamoDB** is a managed, partitioned key-value and document store. Every item has a partition key and an optional sort key; queries hit one partition and read a sorted range. Global secondary indexes are maintained asynchronously, so they're eventually consistent. Items are capped at 400 KB.
- **etcd** is a small, strongly consistent (Raft) store for configuration, service discovery and leader election, not for application data.

Key-value stores scale almost linearly because every operation touches one partition. The cost: no "all orders over $100 last week" unless you designed a key or index for it.

## Document databases

MongoDB, Couchbase, Firestore. A record is a JSON-like document with nested objects and arrays; documents in a collection don't need identical fields. Secondary indexes work on nested paths, and MongoDB's aggregation pipeline covers filtering, grouping and limited joins (`$lookup`).

The design question is **embed or reference**. Embed child data that is read with the parent, bounded and owned by it (an order's line items). Reference data that is shared, unbounded or updated independently (a product used by many orders). A document maps well to a DDD aggregate.

- **Use for:** content and catalog data with variable attributes, per-entity aggregates read as a whole, rapid schema evolution.
- **Avoid for:** heavily relational data with many-to-many links and cross-entity invariants.
- **Consistency:** single-document writes are atomic. MongoDB added multi-document transactions on replica sets in 4.0 and sharded clusters in 4.2; they work, but cost more than in a relational database and are not the default modeling tool.

> **Gotcha:** "Schemaless" means the schema lives in your application code and in every old document version. Use JSON Schema validation or a strict model layer.

## Wide-column stores

Apache Cassandra, ScyllaDB, HBase, Google Bigtable. Don't confuse these with columnar analytics databases (ClickHouse, BigQuery, Redshift), which store each column separately for fast scans.

A wide-column table has a **partition key** that decides which nodes own the data, and **clustering columns** that sort rows inside the partition. A query reads one partition and a contiguous slice of it. Storage is a log-structured merge tree: writes go to a commit log and memtable, flush to immutable SSTables, and background compaction merges them. That makes writes cheap and sequential; reads may consult several SSTables (bloom filters help).

Cassandra uses leaderless replication with tunable consistency per query: with replication factor `N`, choosing read and write quorums so that `R + W > N` gives read-your-writes for a single key. Lightweight transactions (`IF NOT EXISTS`) use Paxos and are much slower than plain writes.

- **Use for:** very high write throughput of time-ordered, per-entity data: messages per channel, sensor readings per device, activity per user. Discord has publicly described storing messages in Cassandra, later ScyllaDB.
- **Avoid for:** ad hoc queries, joins, aggregates across partitions, or data that needs multi-row transactions. You design one table per query and duplicate data across them.

## Graph databases

Neo4j (Cypher), Amazon Neptune, JanusGraph. Data is nodes and edges with properties. Native graph stores keep adjacency next to each node, so traversal cost depends on how much of the graph you touch, not its total size.

- **Use for:** multi-hop, variable-depth questions: fraud rings (accounts sharing devices and cards), "friends of friends who bought X", permission inheritance through nested groups, knowledge graphs.
- **Avoid for:** bulk aggregates over all nodes, simple CRUD, or high-volume writes of independent records.
- **Before adopting one:** PostgreSQL recursive CTEs handle trees and shallow graphs (org charts, folders, a few hops of permissions). Reach for a graph database when deep traversals are central to the product.

## Time-series databases

InfluxDB, TimescaleDB, Prometheus, VictoriaMetrics, QuestDB. Time-series data is append-mostly, keyed by time plus tags (host, region, tenant), queried by time range and aggregated. Engines exploit that: they partition by time, compress heavily (delta-of-delta timestamps, XOR-encoded floats as described in Facebook's Gorilla paper), downsample old data, and drop expired chunks cheaply.

- **TimescaleDB** is a PostgreSQL extension: hypertables auto-partition by time, with compression and continuous aggregates, while you keep SQL and joins to regular tables.
- **Prometheus** is a pull-based metrics system with its own TSDB, built for monitoring rather than business data.

> **Gotcha:** Cardinality kills metrics stores. Every unique combination of label values is a separate series. Putting `user_id` or `request_id` in a Prometheus label creates millions of series and exhausts memory. Keep high-cardinality values in logs or traces.

## Search engines

Elasticsearch, OpenSearch, Apache Solr, all built on Lucene. The core structure is an inverted index: for each term, the list of documents containing it. Text passes through analyzers (tokenize, lowercase, stem, synonyms) at index and query time, and results are ranked, by default with BM25. They also do faceting and aggregations well.

- **Use for:** full-text search with relevance ranking, typo tolerance, faceted navigation, log search.
- **Not a system of record:** indexing is near real time (documents become searchable after a refresh, by default every second in Elasticsearch), there are no multi-document transactions, and mapping changes often require a full reindex. Keep the truth in your primary database and feed the index from it.
- **Before adopting one:** PostgreSQL full-text search (`tsvector` with a GIN index) and `pg_trgm` cover a lot of in-app search. See [Full-text search]({{ '/postgresql/full-text-search/' | relative_url }}).

## Data model fit

Start from the shape of the data and the dominant access pattern:

| Shape / access pattern | Natural fit |
|---|---|
| Entities with relationships and invariants | Relational |
| Lookup by a single key, very hot | Key-value |
| Self-contained aggregates read whole | Document (or JSONB column) |
| Huge per-entity ordered streams, write-heavy | Wide-column |
| Deep, variable-length relationship traversal | Graph |
| Metrics and events by time range | Time-series |
| Free text, relevance, facets | Search engine |

## Consistency

Relational databases give multi-statement ACID transactions with selectable isolation levels; see [ACID and transactions]({{ '/backend/acid-and-transactions/' | relative_url }}). Many NoSQL systems scope atomicity to one item or one partition and offer eventual consistency on replicas and secondary indexes by default, with stronger options at a latency cost (DynamoDB strongly consistent reads, Cassandra `QUORUM`). The question to ask: which invariants span more than one record? Money moving between accounts, inventory reservations and uniqueness across records want a store that can enforce them in one transaction. See [CAP and consistency models]({{ '/backend/cap-and-consistency-models/' | relative_url }}).

## Scaling

| Family | How it scales writes | Typical limit you hit first |
|---|---|---|
| Relational | Bigger primary, then partition, then shard | Single-primary write throughput, vacuum, connection count |
| Key-value / wide-column | Add nodes; data spreads by partition key | Hot partitions from a skewed key |
| Document | Sharding by a shard key | Jumbo chunks, scatter-gather queries |
| Search | More shards and replicas | Shard count fixed at index creation, reindex cost |
| Distributed SQL | Automatic range splits | Cross-region commit latency, contention |

Built-in horizontal scale is the main reason to pick a NoSQL store, and it only helps if your key spreads load: one hot partition caps you at one node's throughput.

## Query flexibility

SQL answers questions you didn't plan for: a new report, a support query, a one-off data fix. Access-pattern-first stores trade that for predictable latency at scale. For a young product whose access patterns change monthly, that trade is usually wrong; for one or two known queries at very high volume, usually right.

## Decision table

| Requirement | Default choice | Why | Watch out for |
|---|---|---|---|
| Core business data, multi-tenant SaaS | PostgreSQL | Transactions, constraints, ad hoc SQL | Plan for partitioning of big tables |
| Session store, cache, rate limits | Redis | Sub-millisecond, TTLs, atomic ops | Memory cost, persistence settings |
| Predictable single-key access at any scale, serverless | DynamoDB | Managed, scales per partition | Access patterns fixed up front |
| Chat messages, IoT readings at very high write rate | Cassandra / ScyllaDB | Write-optimized, linear scale | No joins, one table per query |
| Catalog with variable attributes | PostgreSQL JSONB or MongoDB | Flexible fields | Don't hide relational data in JSON |
| Fraud rings, deep relationship queries | Neo4j / Neptune | Fast multi-hop traversal | Another system to sync |
| Metrics, monitoring | Prometheus / TimescaleDB | Compression, downsampling, retention | Label cardinality |
| Product search with ranking | OpenSearch / Elasticsearch | Inverted index, BM25, facets | Not a source of truth |
| Global writes with SQL and strong consistency | Spanner / CockroachDB | Distributed transactions | Latency and cost |

> **Rule of thumb:** Start with PostgreSQL for the system of record. Add a specialized store only when you can name the access pattern Postgres can't serve within your latency or cost budget, and you have measured it.

## Polyglot persistence

Polyglot persistence (a term popularized by Martin Fowler and Pramod Sadalage) means using several data stores in one system, each for the job it does best. A typical SaaS backend ends up with:

```text
                 +------------------+
  writes ------> |    PostgreSQL    |  system of record
                 |  (+ outbox table)|
                 +--------+---------+
                          | WAL / logical decoding (CDC)
                          v
                 +------------------+
                 |  Kafka / queue   |
                 +---+---------+----+
                     |         |            +-------------+
                     v         v            |    Redis    | cache,
              +-----------+ +-----------+   | (read-thru) | sessions
              | OpenSearch| | Warehouse |   +-------------+
              |  (search) | | (BI, OLAP)|
              +-----------+ +-----------+
```

Each query hits a store built for it. The costs are easy to underestimate:

- **Synchronization.** Writing to Postgres and then to Elasticsearch from the request handler (dual writes) loses updates whenever the second write fails or the two race. Use the transactional outbox or change data capture (Debezium reading the Postgres WAL) so derived stores follow the source of truth. See [Event-driven architecture]({{ '/backend/event-driven-architecture/' | relative_url }}).
- **Operations.** Every store needs backups, upgrades, monitoring and someone on call who understands it.
- **Consistency across stores.** Derived stores lag. The UI must tolerate a search result that's a second stale, and you need a way to rebuild a derived store from scratch.

PostgreSQL with extensions (JSONB, full-text search, `pg_trgm`, pgvector, TimescaleDB, PostGIS) often covers three or four of these roles well enough to defer adding a system for a long time.

## In practice

Document-style data inside PostgreSQL, with an index on the field you filter by. Illustrative:

```sql
CREATE TABLE products (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id   bigint NOT NULL REFERENCES tenants (id),
  sku         text   NOT NULL,
  name        text   NOT NULL,
  attributes  jsonb  NOT NULL DEFAULT '{}',
  UNIQUE (tenant_id, sku)
);

-- containment queries: attributes @> '{"color": "red"}'
CREATE INDEX products_attrs_gin ON products USING gin (attributes jsonb_path_ops);

-- a hot scalar filter gets a plain B-tree on the expression
CREATE INDEX products_brand_idx ON products (tenant_id, (attributes ->> 'brand'));
```

See [JSONB in practice]({{ '/postgresql/jsonb/' | relative_url }}) for when a JSON key should become a real column.

The same "messages per channel" requirement modeled query-first in Cassandra (CQL, illustrative). The partition key includes a time bucket so one busy channel doesn't grow an unbounded partition:

```sql
CREATE TABLE messages_by_channel (
  channel_id  bigint,
  bucket      int,          -- e.g. days since epoch
  message_id  timeuuid,
  author_id   bigint,
  body        text,
  PRIMARY KEY ((channel_id, bucket), message_id)
) WITH CLUSTERING ORDER BY (message_id DESC);

-- the only efficient query shape: one partition, newest first
SELECT message_id, author_id, body
FROM messages_by_channel
WHERE channel_id = ? AND bucket = ?
LIMIT 50;
```

Keeping a search index in sync with a transactional outbox in Django (illustrative). The event commits atomically with the data; a relay process publishes it later:

```python
from django.db import transaction

def rename_product(product_id: int, name: str) -> None:
    with transaction.atomic():
        Product.objects.filter(id=product_id).update(name=name)
        OutboxEvent.objects.create(
            topic="product.updated",
            key=str(product_id),
            payload={"id": product_id, "name": name},
        )
    # A relay reads unpublished OutboxEvent rows, publishes to Kafka,
    # and an indexer consumer upserts the document into OpenSearch.
```

## Pitfalls

- **Choosing NoSQL "for scale" at small scale.** 50 GB fits on one Postgres server; you give up joins, constraints and transactions for a problem you don't have.
- **Modeling a key-value store relationally.** Normalized tables in DynamoDB lead to many round trips per page or full table scans. Symptom: `Scan` operations in production code, rising read costs. Fix: design from access patterns, use composite sort keys and GSIs.
- **Unbounded partitions.** A Cassandra partition per user that grows forever, or a DynamoDB partition key with one hot value. Symptom: timeouts on one key, throttling while the cluster is mostly idle. Fix: add a time bucket or a suffix to the partition key.
- **Dual writes.** Writing to two stores in the request path without an outbox. Symptom: search results that never update for some records. Fix: outbox or CDC, plus a periodic reconciliation job.

## Interview questions

**Q: When would you pick Cassandra over PostgreSQL?**
When the workload is very high write volume of per-entity, time-ordered data, the read patterns are few and known (read a partition's latest N rows), you need multi-node or multi-region writes with no single primary, and you can live without joins and multi-row transactions.

**Q: Is MongoDB ACID?**
Single-document operations are atomic. Multi-document transactions exist since 4.0 (replica sets) and 4.2 (sharded clusters), with snapshot isolation. They cost more than in a relational database, so data is still modeled so that most writes touch one document.

**Q: Why not use Elasticsearch as the primary database?**
No multi-document transactions, near-real-time visibility, limited constraints, and mapping changes need reindexing from a source. It's a derived read model, fed from the system of record.

**Q: What is polyglot persistence and its main risk?**
Using multiple specialized stores in one system. The main risk is keeping them consistent: dual writes diverge, so you use an outbox or CDC, make consumers idempotent, and keep a rebuild path. The second risk is operational load.

**Q: What does distributed SQL give you, and at what cost?**
SQL, secondary indexes and serializable transactions with automatic sharding and replication. The cost is higher commit latency (consensus round trips, more so across regions), contention on hot ranges, and more complex operations than a single Postgres.

**Q: You have 1 billion rows. SQL or NoSQL?**
Row count alone doesn't decide it. Ask about access patterns, write rate, row size and invariants. A billion narrow rows with tenant-scoped queries fits in partitioned PostgreSQL. A billion writes per day of append-only events read by key and time may fit Cassandra or a time-series store better.

## Further reading

- Martin Kleppmann, *Designing Data-Intensive Applications*, chapters 2 and 3 (data models and storage engines).
- Pramod Sadalage and Martin Fowler, *NoSQL Distilled*.
- DeCandia et al., "Dynamo: Amazon's Highly Available Key-value Store" (SOSP 2007).
- Chang et al., "Bigtable: A Distributed Storage System for Structured Data" (OSDI 2006).
- Alex DeBrie, *The DynamoDB Book*.
