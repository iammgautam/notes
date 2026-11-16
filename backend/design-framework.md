---
title: The system design interview framework
section: backend
order: 72
phase: "Phase 10 · System design case studies"
summary: A minute-by-minute plan for 45–60 minute design rounds, estimation with a latency cheat sheet, and what SDE-2 vs SDE-3 interviewers score.
---

A system design round tests whether you can turn a vague product ask into a system that works at a stated scale, and whether you can explain why each piece is there. The interviewer has a rubric and a limited amount of time to fill it in. A repeatable framework does two things for you: it guarantees you hit every rubric line, and it frees your attention for the one or two hard problems that actually separate candidates. The case studies that follow ([URL shortener]({{ '/backend/design-url-shortener/' | relative_url }}), [WhatsApp]({{ '/backend/design-whatsapp/' | relative_url }}), [Instagram]({{ '/backend/design-instagram/' | relative_url }}), [YouTube]({{ '/backend/design-youtube/' | relative_url }})) all use this shape.

## A 45–60 minute plan

Most rounds are 45 or 60 minutes, with 5 minutes lost to introductions and 5 to your questions at the end. Budget the remaining time explicitly and glance at the clock at each transition.

| Phase | 45 min | 60 min | What you leave on the board |
|---|---|---|---|
| Requirements and scope | 5 | 6–8 | Functional list, NFRs with numbers, out of scope |
| Estimates | 3–4 | 5 | QPS avg/peak, storage, bandwidth, cache size |
| API | 3–4 | 5 | 3–6 endpoints with shapes |
| Data model | 5 | 6 | Tables or keys, indexes, store choice |
| High-level design | 10 | 12–15 | Box diagram, one request traced end to end |
| Deep dives | 12 | 15–20 | 2–3 hard parts, alternatives compared |
| Bottlenecks and wrap-up | 5 | 5 | 10× story, failure modes, summary |

```text
 0      5    9   13     18             28                40     45
 |------|----|----|------|--------------|-----------------|------|
  reqs   est  API  data   high-level     deep dives         wrap
  ^ agree on scope       ^ checkpoint:   ^ ask "which part
                           "does this      do you want to
                           cover it?"      go deeper on?"
```

The 60-minute version doesn't add phases; it adds depth. Spend the extra time on a third deep dive and on failure modes, which is where senior candidates earn their level.

> **Interview tip:** Say the plan out loud in the first minute: "I'll clarify requirements, do quick estimates, sketch the API and data model, draw the high-level design, then go deep on the hardest parts." Interviewers relax when they can see the structure.

## Requirements and non-functional requirements

### Clarifying questions

Ask questions that change the design, not questions that fill time. Good ones:

- **Who uses it and how?** Consumers on mobile, internal services, third-party developers via API.
- **Which flows matter most?** Pick 3–4 core flows. Explicitly park the rest.
- **Scale?** DAU, peak events per second, data volume, growth rate.
- **Read-heavy or write-heavy?** The ratio decides caching, replication, and fan-out strategy.
- **Consistency?** Can a like count be stale for 5 seconds? Can a payment?
- **Latency target?** p99 for the hot path, not average.
- **Geography?** Single region, multi-region reads, or multi-region writes.
- **Retention and compliance?** How long data lives, deletion requirements, encryption.

### Functional vs non-functional

Functional requirements are verbs the user performs: "post a photo", "see a feed of people I follow". Non-functional requirements (NFRs) are the properties the system must have while doing them. Put numbers on every NFR, because a number is what lets you make a decision later.

| NFR | Typical target | What it drives |
|---|---|---|
| Availability | 99.9% (8.8 h/yr down) to 99.99% (53 min/yr) | Redundancy, multi-AZ, failover |
| Latency | p99 < 200 ms for reads, < 500 ms for writes | Caching, data locality, async work |
| Durability | No acknowledged write lost | Replication, sync vs async commit |
| Consistency | Strong for money, eventual for counters | Store choice, read paths |
| Throughput | Peak QPS per flow | Sharding, horizontal scaling |
| Cost | Rarely stated; ask if storage is huge | Tiered storage, compression |

Availability math is worth memorizing: 99.9% allows about 43 minutes of downtime per month, 99.99% about 4.3 minutes, 99.999% about 26 seconds. Serial dependencies multiply: three services at 99.9% each give about 99.7% end to end, which is why the critical path should have as few hops as possible. See [CAP and consistency models]({{ '/backend/cap-and-consistency-models/' | relative_url }}) for the consistency vocabulary.

### Out of scope

Write the out-of-scope list on the board. It protects your time and shows judgment. "Out of scope: ads, search, admin tooling, billing. I'll mention where they plug in."

## Back-of-envelope estimation

Estimation exists to answer yes/no questions: does this fit on one database, does it fit in memory, do we need a CDN, do we need to shard. Round aggressively and keep the arithmetic visible.

### The method

1. **Average QPS** = DAU × actions per user per day ÷ 86,400. Use 10^5 seconds per day for mental math; it overstates nothing important.
2. **Peak QPS** = average × 2–3 for smooth diurnal traffic, × 5–10 for spiky products (sports, flash sales, push-triggered).
3. **Storage** = writes per day × bytes per record × retention days × replication factor.
4. **Bandwidth** = QPS × payload size. Separate ingress and egress.
5. **Cache** = size of the hot working set: about 20% of the distinct keys read in a day, times record size (the 80/20 heuristic). Count distinct keys, not read requests; counting requests inflates the answer by the repeat factor.
6. **Servers** = peak QPS ÷ sustainable QPS per instance, plus headroom for losing a zone.

Handy conversions: 1 million per day ≈ 12 per second; 1 billion per day ≈ 12,000 per second; 1 year ≈ 3.15 × 10^7 seconds; 2^10 ≈ 10^3, 2^30 ≈ 10^9, 2^40 ≈ 10^12. Sizes: int64 and timestamps 8 bytes, UUID 16 bytes binary (36 as text), a short text row with metadata 200–500 bytes, a compressed photo 200 KB–2 MB, a minute of 1080p video 50–100 MB.

### Latency cheat sheet

Orders of magnitude, not benchmarks. Real hardware varies by 2–5×.

| Operation | Latency |
|---|---|
| L1 cache reference | ~1 ns |
| Main memory reference | ~100 ns |
| Compress 1 KB (Snappy/LZ4) | ~2 µs |
| Read 1 MB sequentially from RAM | ~10–50 µs |
| Random 4 KB read, NVMe SSD | ~20–100 µs |
| Round trip within a datacenter | ~0.1–0.5 ms |
| Redis GET over the network | ~0.2–1 ms |
| PostgreSQL indexed point lookup (hot) | ~0.5–2 ms |
| Read 1 MB sequentially from SSD | ~0.3–1 ms |
| Cross-AZ round trip, same region | ~1–2 ms |
| HDD seek | ~5–10 ms |
| US East ↔ US West round trip | ~60–80 ms |
| US ↔ Europe round trip | ~80–150 ms |
| TLS handshake to a far region | 2–3 RTTs, ~200–400 ms |

Three takeaways: memory is roughly 1,000× faster than a network hop, a cross-region round trip costs as much as hundreds of local database queries, and anything on the hot path that crosses an ocean needs a very good reason.

### Throughput cheat sheet

Per instance, for planning. Treat these as defaults to state and adjust, not facts about your hardware.

| Component | Sustainable throughput |
|---|---|
| Python/Django API worker pool, DB-backed endpoint | ~200–1,000 RPS per instance |
| Go/Java API instance, simple endpoint | ~5,000–20,000 RPS |
| nginx / L7 proxy | tens of thousands RPS per instance |
| PostgreSQL primary, simple indexed reads | ~10,000–50,000 QPS |
| PostgreSQL primary, small write transactions | ~2,000–10,000 TPS |
| Redis, single instance | ~100,000 ops/s (more with pipelining) |
| Kafka broker | hundreds of MB/s; plan ~10 MB/s per partition |
| Cassandra/ScyllaDB node | ~10,000–100,000 writes/s |
| Amazon S3 per prefix (documented) | 3,500 writes/s and 5,500 reads/s |
| Long-lived WebSocket connections per box | ~100,000–1,000,000 with tuning |
| 10 Gbps NIC | ~1.25 GB/s |

### A runnable estimator

Doing estimates in code once makes the mental version faster. This script reproduces the steps above and prints human-readable units.

```python
DAY = 86_400


def human(n, unit=""):
    for prefix in ["", "K", "M", "G", "T", "P"]:
        if abs(n) < 1000:
            return f"{n:.1f} {prefix}{unit}".strip()
        n /= 1000
    return f"{n:.1f} E{unit}"


def estimate(dau, writes_per_user, reads_per_write, record_bytes,
             retention_years=5, peak_factor=3, replication=3,
             hot_fraction=0.2, working_set_days=1, rps_per_server=1000):
    writes_per_day = dau * writes_per_user
    reads_per_day = writes_per_day * reads_per_write
    write_qps = writes_per_day / DAY
    read_qps = reads_per_day / DAY
    peak_read = read_qps * peak_factor
    day_bytes = writes_per_day * record_bytes
    return {
        "write_qps": write_qps,
        "read_qps": read_qps,
        "peak_write_qps": write_qps * peak_factor,
        "peak_read_qps": peak_read,
        "storage_per_day_B": day_bytes,
        "storage_total_B": day_bytes * 365 * retention_years * replication,
        "ingress_bps": write_qps * record_bytes * 8,
        "egress_bps": read_qps * record_bytes * 8,
        # Distinct keys read per day ~ records written in the working set.
        "cache_B": writes_per_day * working_set_days * hot_fraction
                   * record_bytes,
        "app_servers": int(-(-peak_read // rps_per_server)),  # ceil
    }


def report(name, est):
    print(f"--- {name}")
    for key, value in est.items():
        unit = "B" if key.endswith("_B") else "bps" if key.endswith("bps") else ""
        print(f"{key:>18}: {human(value, unit)}")


if __name__ == "__main__":
    # 100M DAU, 1 write each, 100 reads per write, 1 KB records.
    est = estimate(100_000_000, 1, 100, 1_000)
    report("feed-like service", est)
    assert round(est["write_qps"]) == 1157
    assert round(est["read_qps"]) == 115_741
    assert est["storage_per_day_B"] == 100e9          # 100 GB/day
    assert round(est["storage_total_B"] / 1e12, 1) == 547.5  # TB
    assert est["cache_B"] == 20e9                      # 20 GB
    assert est["app_servers"] == 348
    assert human(1_500_000, "B") == "1.5 MB"
    print("ok")
```

## API, data, high-level design, deep dives, bottlenecks

These five steps are the body of the interview. Each one should take the previous step's output as input.

### API

List 3–6 endpoints for the core flows, with request and response shapes. Mention the cross-cutting concerns once: authentication via bearer token, cursor-based [pagination]({{ '/backend/pagination/' | relative_url }}), [idempotency]({{ '/backend/idempotency/' | relative_url }}) keys on non-idempotent writes, [rate limits]({{ '/backend/rate-limiting/' | relative_url }}). Choose the protocol deliberately: REST for public and CRUD-shaped APIs, gRPC between services, WebSockets or SSE for server push ([real-time communication]({{ '/backend/real-time-communication/' | relative_url }})).

### Data model

Start from access patterns, not entities. Write down the top queries ("get the 20 newest posts by authors I follow", "resolve code to URL") and then design tables and keys that serve them with one index lookup each. Then pick the store and say why in one sentence tied to a requirement:

- **PostgreSQL/MySQL:** relations, transactions, secondary indexes, data up to a few TB per primary.
- **Wide-column (Cassandra, ScyllaDB, DynamoDB):** huge write volume, known partition key, time-ordered rows, no joins.
- **Redis:** hot sets, counters, rate limits, sessions, leaderboards.
- **Object storage:** blobs; the database stores only the key.
- **Search engine:** full-text and faceted queries, fed by change data capture.

Name the partition key whenever data won't fit one machine, and say what happens to queries that don't include it ([partitioning and sharding]({{ '/backend/partitioning-and-sharding/' | relative_url }}), [SQL vs NoSQL]({{ '/backend/sql-vs-nosql/' | relative_url }})).

### High-level design

Draw clients, load balancer, stateless services, stores, caches, queues, and CDN. Then trace one write and one read end to end, naming every hop. The trace is what proves the diagram works; a diagram without a trace is just boxes. Keep services coarse: three to six boxes is right for the first pass.

### Deep dives

Pick the two or three hardest problems, the ones where a naive design fails at the stated scale: feed fan-out, ID generation, hot keys, ordering, exactly-once payments, the transcoding pipeline. For each, present at least two options, compare them on the NFRs you wrote down, choose one, and state its failure mode. Offer the interviewer a choice: "The interesting parts are fan-out and the counter hot spot. Which would you like first?"

### Bottlenecks and wrap-up

Close by walking the design forward in scale: what breaks first at 10× traffic (usually the database primary, a hot partition, or the cache), and what breaks at 100× (usually a single-region assumption or a fan-out that becomes quadratic). Then list failure modes: what happens when the cache, a queue, or a region disappears. End with a 30-second summary that maps decisions back to requirements.

## How to drive the conversation

- **Narrate decisions, not drawings.** "I'm putting a queue here because the analytics write shouldn't add latency to the redirect" beats silently drawing a box labeled Kafka.
- **Checkpoint every phase.** "Does this cover what you had in mind before I go deeper?" Interviewers often have a specific deep dive planned; checkpoints let them steer you there early.
- **State assumptions and move on.** If a number is unknown, pick a reasonable one, say it, and continue. Asking the interviewer for every number wastes time.
- **Treat hints as gifts.** "What happens if this user has 50 million followers?" means your fan-out has a problem. Acknowledge it, fix it, don't defend the original.
- **Disagree with data.** If you think a suggestion is worse, say why in terms of a requirement, then defer if they insist: it is their scenario.
- **Keep the board clean.** Requirements in one corner, numbers in another, diagram in the middle. You will point back at them.
- **Manage depth.** If you notice you are 10 minutes into one component, say so and offer to move on.

## What SDE-2 vs SDE-3 interviewers score

Rubrics vary by company, but the axes are consistent: problem exploration, design quality, technical depth, trade-offs, operational awareness, and communication. What changes with level is how much of it you do unprompted.

| Axis | SDE-2 / mid-senior bar | SDE-3 / senior-staff bar |
|---|---|---|
| Requirements | Gathers the main functional and NFRs | Finds the requirement that dominates the design and says so |
| Estimation | Correct QPS and storage | Uses numbers to make decisions (shard or not, cache or not) |
| High-level design | Complete, working, standard components | Simple; every box justified; avoids unnecessary components |
| Depth | Solid on one hard part with some guidance | Identifies the hard parts unprompted, goes deep on two or three |
| Trade-offs | Names alternatives | Compares them against NFRs, picks, states failure modes |
| Operations | Mentions monitoring and replication | Failure modes, degradation, rollout, migrations, cost |
| Evolution | Handles the stated scale | Explains what breaks at 10× and 100× and the path there |
| Communication | Clear, responsive to hints | Drives the session; interviewer mostly listens |

> **Interview tip:** A senior signal that costs nothing: say what you would build first for launch and what you would defer until a metric demands it. Over-building is a negative signal at every level.

## Common mistakes

| Mistake | Why it hurts | Fix |
|---|---|---|
| Drawing boxes in minute one | Design unanchored to requirements | Spend the first 5 minutes on scope |
| No numbers | Can't justify sharding, caching, CDN | Do the 3-minute estimate every time |
| Inconsistent numbers | 10k QPS in estimates, one DB box later | Carry the numbers onto the diagram |
| Buzzword architecture | Kafka, Kubernetes, microservices with no reason | Each component cites a requirement |
| Skipping the data model | Hard parts usually live in keys and indexes | Write tables with partition keys |
| Cache without invalidation | Stale reads, stampedes | State TTL, invalidation, and [stampede]({{ '/backend/cache-stampede/' | relative_url }}) handling |
| Ignoring failure | Looks like a happy-path design | One sentence per component: "if this dies..." |
| Going silent | Interviewer can't score thinking | Think aloud, even when unsure |
| Defending a flawed choice | Signals inflexibility | Acknowledge, fix, move on |
| Ten-minute requirements | No time for depth | Timebox, assume, proceed |

## One-page template

Copy this structure to the whiteboard or doc at the start of every round.

```text
1. REQUIREMENTS
   Functional:   [3-4 core flows]
   Non-functional: DAU __  peak QPS __  read:write __
                   p99 __ ms  availability __  consistency __
   Out of scope: [...]

2. ESTIMATES
   write QPS = DAU x writes / 1e5 = __   (peak x3 = __)
   read QPS  = write QPS x ratio  = __   (peak x3 = __)
   storage   = writes/day x size x 365 x years x 3 = __
   bandwidth = QPS x payload = __ in / __ out
   cache     = 20% of distinct daily read keys x size = __

3. API
   POST /...   GET /...   (auth, pagination, idempotency)

4. DATA MODEL
   table/key | partition key | sort key | indexes | store + why

5. HIGH-LEVEL DESIGN
   client -> CDN -> LB -> services -> cache -> DB / queue
   trace: one write, one read

6. DEEP DIVES (pick 2-3)
   option A vs B vs C -> chosen, why, failure mode

7. BOTTLENECKS AND FAILURE
   10x: __ breaks -> fix     100x: __ breaks -> fix
   cache down / queue down / region down -> behavior

8. SUMMARY: decisions mapped to requirements
```

## Interview questions

**How do you estimate peak QPS when the interviewer gives only DAU?**
Assume actions per user per day, divide by about 10^5 seconds for the average, then multiply by 2–3 for diurnal peaks or more for event-driven spikes. State each assumption so the interviewer can adjust it.

**When does a design need sharding?**
When a single primary can't hold the data (beyond a few TB, or when the working set no longer fits in memory) or can't absorb the write rate (beyond a few thousand to tens of thousands of transactions per second). Read load alone is usually solved with replicas and caching first.

**How do you size a cache?**
Estimate the hot working set: distinct keys read in a window times object size, often approximated as 20% of the distinct keys read per day. Then check it fits in a few dozen GB per node and decide on TTL and eviction. If the hit rate target is above 99%, size for the long tail too.

**How do you decide which component to deep-dive?**
Pick the one where the naive approach fails the NFRs at the stated scale, or the one the interviewer hinted at. Offer the choice explicitly. Don't deep-dive something generic like the load balancer unless asked.

**What does 99.99% availability mean in practice?**
About 4.3 minutes of downtime per month. It rules out single-instance components on the critical path, manual failover, and deploys that cause downtime, and it implies multi-AZ redundancy with automatic failover.

**The interviewer says your design won't scale. What do you do?**
Ask which dimension they're worried about, find the specific bottleneck with numbers, and fix that component. Don't redesign everything; show that you can localize the problem.

**How do you handle a requirement you've never built?**
Reason from first principles: identify the access patterns, the consistency needs, and the scale, then map them to building blocks you know. Interviewers score the reasoning, not prior exposure.

## Further reading

- *Designing Data-Intensive Applications*, Martin Kleppmann
- *System Design Interview: An Insider's Guide*, volumes 1 and 2, Alex Xu
- *Site Reliability Engineering* (Google), chapters on service level objectives and handling overload
- "The Tail at Scale", Jeffrey Dean and Luiz André Barroso, Communications of the ACM, 2013
- Jeff Dean, "Designs, Lessons and Advice from Building Large Distributed Systems" (LADIS 2009 keynote), the origin of the latency numbers list
