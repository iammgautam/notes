---
title: Design an Uber backend
section: backend
order: 78
phase: "Phase 10 · System design case studies"
summary: Driver location ingestion, geospatial indexes with a runnable geohash, dispatch, the trip state machine, ETAs, surge pricing and payment handoff.
---

Ride hailing is a real-time matching market over a map. Two things make it hard: millions of drivers move every few seconds, so the "where is everyone" index is rewritten constantly, and the system must never give one driver to two riders. Pricing, ETAs and payments hang off those two problems.

## Requirements

### Clarifying questions

- Rides only, or food delivery too? Rides only; delivery adds a three-party match.
- Single product or several (economy, XL, premium)? Several; each driver serves a subset.

### Functional

- Drivers go online and stream their location.
- Riders see nearby cars and a fare estimate with any surge applied.
- Riders request a ride; the system offers it to a suitable driver, who accepts or declines.
- Both sides follow the trip live: assigned, arrived, started, completed, canceled.
- On completion the rider is charged and the driver credited.

### Non-functional

| Property | Target |
|---|---|
| Trips | 20M/day (Uber reports on the order of 30M/day across rides and delivery as of 2025) |
| Online drivers at peak | 2M |
| Location update interval | every 4 s |
| Match latency | driver offer sent within 1–2 s of the request |
| Location freshness | dispatch sees a position no older than ~5 s |
| Consistency | a driver is on at most one trip; a trip has at most one driver |
| Availability | 99.99% for request and trip flows; degrade pricing before dispatch |

### Out of scope

Pooled rides (matching becomes routing), onboarding, ratings, fraud, map building.

## Capacity estimates

```text
location writes  = 2M drivers / 4 s              = 500k/s
ping size        = ~100 B (id, lat, lng, heading, speed, ts, status)
ingest bandwidth = 500k x 100 B                  = 50 MB/s
raw history      = 50 MB/s x 86,400 s            = ~4.3 TB/day
hot state        = 2M x 200 B                    = 400 MB (fits in RAM)

trip requests    = 20M / 86,400                  = ~230/s avg
peak             = x5 normal, x15 on New Year    = ~1.2k-3.5k/s
fare estimates   = ~5 per request                = ~6k/s at peak
active trips     = 20M x 20 min / 1,440 min      = ~280k avg, ~800k peak
rider live view  = 800k riders / 4 s             = ~200k pushes/s
trip storage     = 20M x 2 KB                    = 40 GB/day, ~15 TB/year
```

Location writes outnumber trip writes 2,000 to 1, yet the state they maintain is 400 MB. So: latest positions in memory, sharded by geography; the stream itself is a log for analytics. Persistent connections are the other cost: 2M drivers plus riders in active trips, at around 50k connections per gateway node, means a gateway fleet of 50 to 100 nodes.

## API design

Driver and rider apps hold a persistent connection (WebSocket, HTTP/2 or QUIC streams) for pushes; requests below are shown as HTTP for clarity.

```http
POST /v1/drivers/me/locations            (batched; usually over the socket)
{"points": [{"lat": 12.9716, "lng": 77.5946, "heading": 90,
             "speedMps": 8.1, "ts": 1763316000123}],
 "status": "AVAILABLE"}
204

POST /v1/fare-estimates
{"pickup": {"lat": 12.97, "lng": 77.59},
 "dropoff": {"lat": 13.03, "lng": 77.6}, "product": "economy"}
200 {"estimateId": "est_51", "surge": 1.4,
     "fare": {"min": 310, "max": 360, "currency": "INR"},
     "pickupEtaSec": 240, "expiresAt": "..."}

POST /v1/trips                           Idempotency-Key: 7b1e...
{"estimateId": "est_51", "paymentMethodId": "pm_3"}
201 {"tripId": "trp_88", "status": "REQUESTED"}

POST /v1/offers/{offerId}/accept         (driver)
200 {"tripId": "trp_88", "status": "DRIVER_ASSIGNED"}
409 {"error": "offer_expired"}

POST /v1/trips/{tripId}/events      (driver: ARRIVED, STARTED, COMPLETED)
{"type": "STARTED", "expectedVersion": 4}
200 {"status": "IN_PROGRESS", "version": 5}

POST /v1/trips/{tripId}/cancel           (either side)
```

The request references a signed `estimateId`, so the rider pays the price they saw. Trip events carry `expectedVersion`, turning retries and races into clean 409s.

## Data model

| Data | Store | Key / index | Why |
|---|---|---|---|
| Drivers, vehicles, products | PostgreSQL | driver_id | Relational, low write rate |
| Live driver state | In-memory geo index per shard + Redis | driver_id; geo cell | 500k writes/s, only latest value matters |
| Trips | Sharded relational or a document store | trip_id; (rider_id, created_at); (driver_id, created_at) | Transactions per trip, history queries |
| Trip events | Append-only table, same shard as trip | (trip_id, seq) | Audit, replay, disputes |
| Offers | Same shard as trip | offer_id; (trip_id) | Expiry and fencing |
| Fare estimates | Redis with TTL (or signed token) | estimate_id | Short-lived quote |
| Surge multipliers | Redis, per (cell, product) | TTL ~2 min | Read on every estimate |
| Location history | Kafka -> object storage / data lake | (date, city) | ETA training, map matching, disputes |

Uber has publicly described building its own sharded datastores on top of MySQL (Schemaless, later Docstore). Sharded PostgreSQL keyed by `trip_id`, with rider and driver history tables sharded by their own IDs, gives the same shape.

## High-level design

```text
 driver app                                        rider app
     | pings (4 s)                                    | estimate, request
     v                                                v
 +-----------+    +-------+    +-------------+   +-----------+
 | conn      |--->| Kafka |--->| location    |   | API gw    |
 | gateways  |    | (by   |    | service:    |   +-----------+
 | (sockets) |<-+ | city) |    | geo index   |     |     |
 +-----------+  | +-------+    | per shard   |     v     v
     ^          |   |   |      +-------------+  +-----+ +---------+
     | offers,  |   |   +-> surge (stream) ---> |price| | trip    |
     | updates  |   |       per cell, Redis     +-----+ | service |
     |          |   +-> data lake (ETA, maps)          +---------+
     |          |                                        |   |
     |          |   +-----------+  candidates  +------+  |   | outbox
     |          +---| dispatch  |<-------------| geo  |  |   v
     |              | service   |--ETA rank--->| ETA  |  | +--------+
     +--------------+-----------+              +------+  | |payments|
                         ^                               | +--------+
                         +------- trip REQUESTED --------+
```

1. **Location.** Pings land on a connection gateway, then Kafka, partitioned by city. The location service for that city updates its in-memory index and the driver's latest state in Redis.
2. **Estimate.** Pricing combines route distance and time with the pickup cell's surge and returns a signed estimate.
3. **Request.** The trip service creates the trip (`REQUESTED`) idempotently and hands it to dispatch for that city.
4. **Dispatch.** Pull nearby available drivers from the geo index, rank by road ETA, claim the best, send an offer over the driver's socket.
5. **Accept.** The driver accepts; the trip moves to `DRIVER_ASSIGNED` in one conditional update. The rider gets the driver's live location from then on.
6. **Complete.** The driver ends the trip; the trip service records `COMPLETED` and emits an event through the outbox. Payments charges the rider.

## Deep dives

### Driver location ingestion

- **Transport.** One long-lived connection per driver; the app sends a ping every 4 s and batches several when the network is poor. Each ping carries a client timestamp and a monotonic sequence number.
- **Validation at the edge.** Authenticate once per connection; drop impossible jumps (300 km in 4 s) and points older than the stored one.
- **Partitioning.** Kafka keyed by city (or a coarse geo cell for large cities) so one consumer owns one area's index.
- **State.** The live index holds only the latest position, status (`AVAILABLE`, `OFFERED`, `ON_TRIP`, `OFFLINE`) and product set. A driver with no ping for ~30 s is treated as unavailable; this is how a phone in a tunnel stops getting offers.
- **Recovery.** A replacement location node replays the last minute of its Kafka partitions to rebuild the index; no database restore.

### Geospatial indexing

The query is "available drivers of product P within r km of this point, nearest first", hundreds of times a second per city, against an index rewritten 500k times a second.

| Index | Cell shape | Strengths | Weaknesses |
|---|---|---|---|
| Geohash | Rectangles on a Z-order curve | A string prefix is a cell; works in any sorted store (Redis `GEOSEARCH` stores geohash scores in a sorted set) | Edge problem; cells stretch with latitude |
| Quadtree | Squares split where dense | Adapts to density: fine downtown, coarse in suburbs | In-memory tree; rebalancing as points move; harder to shard |
| S2 | Cube faces on a sphere, Hilbert curve | Good locality, region coverings; used widely at Google | More complex to reason about |
| H3 | Hexagons, hierarchical | All neighbors equidistant; great for aggregation and smoothing (surge) | Children do not nest exactly in parents |
| R-tree (PostGIS GiST) | Bounding boxes | Exact queries on arbitrary shapes | Write-heavy moving points in one database |

Uber has publicly described using Google's S2 in dispatch and later open-sourced H3. For a live index any cell scheme works, as long as you query the home cell plus enough neighbors. A runnable geohash encoder and index, including the case where a driver 20 cm away sits in a cell with a different prefix:

```python
import math

BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"   # no a, i, l, o


def encode(lat, lng, precision=6):
    """Interleave longitude/latitude bisection bits, 5 bits per char."""
    lat_rng, lng_rng = [-90.0, 90.0], [-180.0, 180.0]
    out, bits, nbits, even = [], 0, 0, True    # even bit = longitude
    while len(out) < precision:
        rng, val = (lng_rng, lng) if even else (lat_rng, lat)
        mid = (rng[0] + rng[1]) / 2
        if val >= mid:
            bits, rng[0] = bits * 2 + 1, mid
        else:
            bits, rng[1] = bits * 2, mid
        even, nbits = not even, nbits + 1
        if nbits == 5:
            out.append(BASE32[bits])
            bits, nbits = 0, 0
    return "".join(out)


def decode_box(gh):
    """Return ((lat_min, lat_max), (lng_min, lng_max)) of the cell."""
    lat_rng, lng_rng, even = [-90.0, 90.0], [-180.0, 180.0], True
    for ch in gh:
        idx = BASE32.index(ch)
        for shift in range(4, -1, -1):
            rng = lng_rng if even else lat_rng
            mid = (rng[0] + rng[1]) / 2
            if (idx >> shift) & 1:
                rng[0] = mid
            else:
                rng[1] = mid
            even = not even
    return tuple(lat_rng), tuple(lng_rng)


def cells_within(lat, lng, radius_km, precision=6):
    """Cells covering a radius: the home cell plus enough rings around it.
    Fixes the edge problem: a nearby point can sit in a cell with a
    completely different prefix. Ignores pole wrap (fine for cities)."""
    (la0, la1), (lo0, lo1) = decode_box(encode(lat, lng, precision))
    dlat, dlng = la1 - la0, lo1 - lo0
    km_lat = dlat * 111.32
    km_lng = dlng * 111.32 * math.cos(math.radians(min(abs(lat) + 1, 89)))
    ki, kj = math.ceil(radius_km / km_lat), math.ceil(radius_km / km_lng)
    clat, clng = (la0 + la1) / 2, (lo0 + lo1) / 2
    return {
        encode(max(-90.0, min(90.0, clat + i * dlat)),
               (clng + j * dlng + 180) % 360 - 180, precision)
        for i in range(-ki, ki + 1) for j in range(-kj, kj + 1)
    }


def haversine_km(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(a))


class DriverIndex:
    """cell -> driver ids, plus each driver's latest position."""

    def __init__(self, precision=6):
        self.p, self.cells, self.pos = precision, {}, {}

    def update(self, driver, lat, lng):
        cell = encode(lat, lng, self.p)
        old = self.pos.get(driver)
        if old and old[0] != cell:
            self.cells[old[0]].discard(driver)
        self.cells.setdefault(cell, set()).add(driver)
        self.pos[driver] = (cell, lat, lng)

    def nearby(self, lat, lng, radius_km, k=5):
        found = []
        for cell in cells_within(lat, lng, radius_km, self.p):
            for d in self.cells.get(cell, ()):
                _, dlat, dlng = self.pos[d]
                dist = haversine_km(lat, lng, dlat, dlng)
                if dist <= radius_km:
                    found.append((dist, d))
        return [d for _, d in sorted(found)[:k]]


if __name__ == "__main__":
    assert encode(57.64911, 10.40744, 11) == "u4pruydqqvj"  # known vector
    assert encode(42.6, -5.6, 5) == "ezs42"
    (la0, la1), (lo0, lo1) = decode_box("ezs42")
    assert la0 <= 42.6 <= la1 and lo0 <= -5.6 <= lo1

    idx = DriverIndex()
    rider = (12.9716, 77.5946)                     # Bengaluru
    idx.update("d_north", 12.9743, 77.5946)        # ~300 m
    idx.update("d_east", 12.9716, 77.6029)         # ~900 m
    idx.update("d_far", 13.0200, 77.5946)          # ~5.4 km
    assert idx.nearby(*rider, radius_km=1.0) == ["d_north", "d_east"]
    idx.update("d_north", 13.0300, 77.5946)        # driver moved away
    assert idx.nearby(*rider, radius_km=1.0) == ["d_east"]

    # Edge problem: two points 0.2 m apart in cells with different prefixes.
    (la0, la1), _ = decode_box(encode(*rider))
    a, b = (la1 - 1e-6, rider[1]), (la1 + 1e-6, rider[1])
    assert encode(*a) != encode(*b)
    idx.update("d_edge", *b)
    assert idx.nearby(*a, radius_km=0.1) == ["d_edge"]
    print("ok", encode(*rider), len(cells_within(*rider, 1.0)))
```

Precision 6 cells are about 1.2 km × 0.6 km at the equator, so a 1 km search touches around 15 cells. In production the index is sharded by city, cells also carry status and product, and the radius widens in steps when nothing is found.

### Matching and dispatch

Nearest by straight line is wrong: a driver 300 m away on the other side of a river or a one-way highway can be 10 minutes out.

1. Pull 20–50 candidates from the geo index (available, right product, fresh ping).
2. Rank by road ETA to the pickup from the ETA service, adjusted by signals like acceptance rate and heading.
3. Claim the top driver and send an offer with a 10–15 s timeout. On decline or timeout, release the claim and move to the next candidate.

Greedy per-request matching is locally optimal and globally poor: two riders a block apart can each grab the driver the other needed. Batching requests for a second or two and solving the assignment jointly (minimizing total pickup ETA) does better; Uber has publicly described batched matching. The window trades match quality against latency.

### Consistency of driver assignment

Two invariants: a driver is never on two trips, and a trip never gets two drivers. They break when two dispatcher instances pick the same driver, or when an accept races a rider cancel.

- **Single writer per area.** Route all dispatch for a city shard to one owner (consistent hashing over the dispatch fleet; Uber has publicly described Ringpop for this). Most races disappear because one process decides.
- **Atomic claim on the driver** for the leftovers (failover, shard boundaries). The claim holds the offer ID as a fencing token and expires with the offer:

```text
SET driver:d42:claim offer_9001 NX PX 15000     (illustrative Redis)
```

- **Conditional transition on the trip** as the source of truth. Accept succeeds only if both rows are still in the expected state:

```sql
-- Illustrative: both must update exactly one row, or roll back
BEGIN;
UPDATE driver_state SET status = 'ON_TRIP', trip_id = 'trp_88'
 WHERE driver_id = 'd42' AND status = 'OFFERED' AND offer_id = 'offer_9001';
UPDATE trips SET status = 'DRIVER_ASSIGNED', driver_id = 'd42',
                 version = version + 1
 WHERE trip_id = 'trp_88' AND status = 'REQUESTED';
COMMIT;
```

If the rider canceled a millisecond earlier, the trip update matches zero rows, the transaction rolls back, the driver gets "offer no longer available", and the claim is released. When driver state and trips live in different stores, do the same as a short saga: claim the driver, CAS the trip, release the driver if the CAS fails. See [distributed locking]({{ '/backend/distributed-locking/' | relative_url }}) for why the fencing token matters.

### Trip state machine

| From | Event | To |
|---|---|---|
| REQUESTED | driver accepts offer | DRIVER_ASSIGNED |
| REQUESTED | no driver within N minutes | NO_DRIVER_FOUND |
| REQUESTED | rider cancels | CANCELED |
| DRIVER_ASSIGNED | driver arrives (tap or geofence) | DRIVER_ARRIVED |
| DRIVER_ASSIGNED | driver cancels | REQUESTED (re-dispatch) |
| DRIVER_ASSIGNED, DRIVER_ARRIVED | rider cancels | CANCELED (fee rules apply) |
| DRIVER_ARRIVED | driver starts trip | IN_PROGRESS |
| IN_PROGRESS | driver ends trip | COMPLETED |

Every transition is a single `UPDATE ... WHERE trip_id = ? AND status = ? AND version = ?` plus an insert into `trip_events` and an outbox row, in one transaction. The state machine lives in one place (the trip service), clients only send intents, and a retried "start trip" is a no-op because the version moved.

### ETAs

ETAs feed the estimate, dispatch ranking (dozens per request) and the live trip view.

- A routing engine computes shortest time over the road graph with techniques like contraction hierarchies, so a city-scale query takes milliseconds.
- Edge speeds come from live traffic, derived from map-matched driver pings, blended with historical speeds for that hour and weekday.
- A learned model corrects the routing estimate for what the graph misses (airport pickups, time of day); Uber has publicly described such a model on top of its routing engine.
- For ranking, a precomputed cell-to-cell travel-time matrix filters candidates; exact routing runs for the top few.

### Surge pricing

When demand outruns supply in an area, a higher price trims demand and pulls drivers in.

- **Measure.** A streaming job (Kafka into Flink or similar) counts demand (requests and estimate views) and supply (available drivers) per H3 cell (resolution 7 is around 5 km²) and product, over a sliding window of 1–2 minutes.
- **Compute.** Multiplier = a capped, monotonic function of the demand/supply ratio. Smooth in time (EWMA, so it does not flicker) and in space (blend with neighboring cells, so crossing a street does not halve the price).
- **Serve.** Write multipliers to Redis with a short TTL. If the pipeline stalls, entries expire and pricing falls back to 1.0 rather than serving a stale spike.
- **Lock.** The estimate embeds the multiplier and an expiry and is signed. The trip uses the estimate, so the rider pays what they accepted. Many markets now show an upfront total rather than a multiplier; the mechanics underneath are the same.

### Payments handoff

The trip service does not talk to card networks. At request time payments places an authorization hold for the estimated fare. On `COMPLETED`, the trip service writes a `TripCompleted` event through the transactional outbox; the payment service consumes it and captures the final amount with idempotency key `trip_id`, so a redelivered event cannot double-charge. A payment failure never reopens the trip: the rider's account goes into arrears and is retried or collected later, while the driver's earnings are credited from the platform's ledger regardless. See [design a payment system]({{ '/backend/design-payment-system/' | relative_url }}) and [idempotency]({{ '/backend/idempotency/' | relative_url }}).

## Scaling and bottlenecks

| Scale | What breaks first | Fix |
|---|---|---|
| 10× drivers | Location shard for a mega city | Split the city into geo sub-shards; query neighbors across shard edges |
| 10× drivers | Connection gateways | More nodes, consistent-hash drivers to gateways, keep gateways stateless beyond the socket |
| 10× requests | ETA calls for ranking | Cell matrix prefilter; cache recent ETAs per (cell, cell, 5-min bucket) |
| 100× | Hot event (stadium exit) | Queue riders with honest wait times; surge; batch matching over a larger window |
| 100× | Trip DB | Shard by trip_id; rider and driver history tables sharded by their own IDs |

## Reliability and failure modes

| Failure | Effect | Mitigation |
|---|---|---|
| Location node dies | Its area has no index | Replay Kafka to rebuild in seconds; standby replica consuming the same partition |
| Dispatch owner dies mid-offer | Driver claimed, trip in limbo | Claim TTL expires; trip watchdog re-dispatches REQUESTED trips older than N seconds |
| Driver phone loses network | Stale position | Ping TTL removes driver from available pool |
| Surge pipeline lag | Stale prices | Multiplier TTL falls back to 1.0 |
| Region outage | Cities served there go dark | Cities are pinned to a primary region with a warm standby; trip state replicated; apps reconnect to the standby |

Degrade in order of value: if ETAs are slow, rank by distance; if surge is down, price at 1.0; if the live map is down, still dispatch. Never degrade the assignment invariants.

## Trade-offs

| Decision | Option | Chosen? | Why |
|---|---|---|---|
| Live location store | Database with geo index | No | 500k writes/s of data that is stale in 5 s |
| Live location store | In-memory index per geo shard, Kafka as log | Yes | Fast, rebuildable from the log |
| Cell scheme | Geohash / S2 for index, H3 for surge | Yes | Simple lookups; uniform neighbors for aggregation |
| Matching | Greedy nearest by distance | No | Ignores roads; locally optimal |
| Matching | ETA-ranked, short batched windows | Yes | Better global pickup times |
| Assignment safety | Distributed lock only | No | Locks expire; need a durable truth |
| Assignment safety | Single writer + claim + conditional update | Yes | Invariant enforced by the database |
| Pricing quote | Recompute at request | No | Rider pays a price they never saw |
| Pricing quote | Signed estimate with expiry | Yes | Price honesty, cheap to verify |

## Interview follow-ups

**Why not store every location ping in PostgreSQL with PostGIS?** You would write 500k rows/s to answer questions about the last 5 seconds. Keep the latest position in memory, the stream in Kafka, and history in cheap storage.

**How do you find drivers near a shard boundary?** The query covers neighbor cells; if any belong to another shard, scatter to that shard too and merge. Shard boundaries are cell boundaries, so the lookup is deterministic.

**A driver accepts at the same instant the rider cancels. What happens?** Both are conditional updates on the trip row; whichever commits first wins. If cancel wins, the accept matches zero rows and the driver sees "offer no longer available".

**How do you stop surge from flickering?** Window over 1–2 minutes, EWMA over windows, hysteresis on the thresholds, and spatial smoothing across neighbor cells.
