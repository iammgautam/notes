---
title: SQL vs NoSQL
section: hld
order: 7
summary: Pick a datastore from access patterns and real triggers, and know how much ground PostgreSQL already covers.
---

## What it is

- **SQL (relational):** tables, schema, joins, ACID transactions, flexible queries. PostgreSQL, MySQL.
- **NoSQL:** a family of stores that trade query flexibility or consistency for scale or a specific model.

| Type | Model | Examples | Sweet spot |
|---|---|---|---|
| Key-value | key → blob | Redis, DynamoDB, Memcached | Sessions, caches, counters, simple lookups |
| Document | key → JSON doc | MongoDB, Firestore | Self-contained aggregates, flexible schema |
| Wide-column | partition key → sorted rows | Cassandra, ScyllaDB, Bigtable | Massive write throughput, time series, messages |
| Graph | nodes + edges | Neo4j | Deep traversals (fraud rings, recommendations) |
| Search | inverted index | Elasticsearch, OpenSearch | Full-text, faceting, log search |
| Time series | time-ordered points | TimescaleDB, InfluxDB | Metrics, IoT |

## When to use

Default to **PostgreSQL**. Move off it only when a concrete trigger appears.

| Trigger | Consider |
|---|---|
| Sustained writes beyond one primary (~10K+/s) that cannot be sharded by tenant easily | Cassandra / DynamoDB |
| Data >> several TB with simple key access, linear scale needed | DynamoDB / Cassandra |
| Access is always by one key, no joins, need single-digit ms at any scale | DynamoDB / Redis |
| Multi-region active-active writes | Cassandra, DynamoDB global tables, Spanner/CockroachDB |
| Relevance-ranked full-text search with facets at large scale | Elasticsearch / OpenSearch |
| Traversals 4+ hops deep as the core query | Graph DB |
| Sub-ms ephemeral data (sessions, rate limits, leaderboards) | Redis |

## Postgres covers more than people think

| Need | Postgres feature |
|---|---|
| Flexible schema / documents | `JSONB` + GIN index |
| Full-text search | `tsvector`, `ts_rank`, GIN; `pg_trgm` for fuzzy |
| Vector search / RAG | `pgvector` (HNSW, IVFFlat) |
| Job queue | `SELECT ... FOR UPDATE SKIP LOCKED` |
| Pub/sub | `LISTEN/NOTIFY` |
| Time series | Declarative partitioning, BRIN, TimescaleDB |
| Geospatial | PostGIS |
| Key-value | Table with PK; `UNLOGGED` tables for cache-like data |
| Horizontal scale | Citus, read replicas, partitioning |

One system means one backup story, one consistency model, transactional writes across all of it.

## Trade-offs

| | SQL | NoSQL (typical) |
|---|---|---|
| Schema | Enforced, migrations | Flexible, enforced in app |
| Queries | Ad hoc, joins, aggregates | Designed per access pattern |
| Transactions | Multi-row ACID | Often single-item/partition only |
| Scale model | Vertical + replicas; sharding is work | Horizontal by design |
| Consistency | Strong by default | Often eventual or tunable |
| Data modeling | Normalize, then denormalize hot paths | Denormalize up front, duplicate data per query |

## Gotchas

- "Schemaless" means the schema lives in your code and old documents. Migrations still happen.
- DynamoDB/Cassandra: you must know queries up front. A new access pattern often means a new table or index and a backfill.
- Cassandra: reads before writes and secondary indexes are anti-patterns; deletes create tombstones.
- Polyglot persistence adds sync problems: keep Postgres the source of truth and feed search/cache via CDC or outbox.
- MongoDB has had multi-document transactions since 4.0; "NoSQL has no transactions" is outdated.

> **Interview tip:** Justify the store from access patterns: "Messages are always read by `(conversation_id, time)`, write-heavy, and append-only, so a wide-column store partitioned by conversation fits. User accounts and billing stay in Postgres."
