---
title: Back-of-the-envelope estimation
section: hld
order: 2
summary: Latency numbers, powers of two and a worked QPS, storage and bandwidth example you can redo in two minutes.
---

Estimates exist to pick an architecture, not to be precise. Round to one significant figure and keep moving.

## When to use

- After requirements, before drawing boxes.
- Whenever a choice depends on size: "fits in memory?", "one DB or sharded?", "needs a CDN?".

## Latency numbers (orders of magnitude)

| Operation | Time |
|---|---|
| L1 cache reference | ~1 ns |
| Main memory reference | ~100 ns |
| Compress 1 KB (fast codec) | ~2 µs |
| Read 1 MB sequentially from memory | ~10 µs |
| Round trip within same datacenter | ~0.5 ms |
| Random read from SSD | ~0.1 ms |
| Read 1 MB sequentially from SSD | ~1 ms |
| Redis GET over network (same AZ) | ~0.5–1 ms |
| Simple indexed Postgres query | ~1–5 ms |
| Disk seek (HDD) | ~10 ms |
| Read 1 MB sequentially from HDD | ~20 ms |
| Round trip US east ↔ west | ~70 ms |
| Round trip US ↔ Europe | ~100–150 ms |

Takeaways: memory ≫ SSD ≫ network across regions. Avoid cross-region calls on the hot path; batch small network calls.

## Powers of two and handy constants

| Power | Exact | Approx | Name |
|---|---|---|---|
| 2^10 | 1,024 | 10^3 | KB |
| 2^20 | 1,048,576 | 10^6 | MB |
| 2^30 | | 10^9 | GB |
| 2^40 | | 10^12 | TB |
| 2^50 | | 10^15 | PB |

- 1 day ≈ 86,400 s ≈ **10^5 s** (round up for easy math).
- 1 year ≈ 3.15 × 10^7 s; 1 M req/day ≈ **12 QPS**; 1 B req/day ≈ **12K QPS**.
- 2^32 ≈ 4.3 B (int32 overflow); 62^7 ≈ 3.5 T (base62 IDs).
- Availability: 99.9% ≈ 8.8 h/yr down; 99.99% ≈ 53 min/yr.

## Rough capacity of one box

| Component | Ballpark |
|---|---|
| Stateless app server (Python) | 100s–low 1000s RPS per box |
| Postgres primary (simple OLTP) | ~5–20K QPS reads, ~1–5K writes/s |
| Redis single node | ~100K ops/s |
| Kafka partition | ~10 MB/s write |
| 1 Gbps NIC | ~125 MB/s |

These vary by hardware and query shape. Say "ballpark" and show the math.

## Worked example: photo sharing app

Assumptions: 10 M DAU, each views 50 photos/day and uploads 0.2 photos/day, average photo 500 KB (plus thumbnails), metadata 1 KB.

**QPS**

- Reads: 10 M × 50 = 5 × 10^8/day ÷ 10^5 ≈ **5K QPS avg**, ~15K peak (3×).
- Writes: 10 M × 0.2 = 2 × 10^6/day ÷ 10^5 ≈ **20 QPS avg**, ~60 peak.
- Read:write ≈ 250:1 → cache and CDN heavy.

**Storage**

- Photos: 2 × 10^6/day × 500 KB = 10^12 B = **1 TB/day** → ~365 TB/yr. Object storage, not a DB.
- Metadata: 2 × 10^6 × 1 KB = 2 GB/day → ~730 GB/yr. Fits one Postgres for years.

**Bandwidth**

- Egress: 5K QPS × 500 KB = 2.5 GB/s avg (~20 Gbps). Needs a CDN.
- Ingress: 20 QPS × 500 KB = 10 MB/s. Trivial.

**Cache**

- 80/20 rule: cache ~20% of daily read metadata: 0.2 × 5 × 10^8 × 1 KB ≈ 100 GB. A small Redis cluster.

## Template

```text
QPS      = DAU * actions_per_user / 1e5        (peak = 2-5x)
storage  = writes_per_day * size * retention_days * replication(3x)
egress   = read_QPS * response_size
cache    = 0.2 * daily_reads * object_size     (hot set)
servers  = peak_QPS / per_server_QPS           (+ headroom)
```

## Gotchas

- Forgetting replication factor (×3) and indexes (~+30–100%) on storage.
- Mixing bits and bytes on bandwidth (Gbps vs GB/s, ×8).
- Using averages when the system must survive peak.
- Over-precision. "~5K QPS" is the right answer; "5,787 QPS" wastes time.

> **Interview tip:** State each assumption out loud, write the result next to the requirement it affects, and say the conclusion: "metadata fits one Postgres; media needs object storage plus CDN."
