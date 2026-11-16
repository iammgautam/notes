---
title: Design a Netflix backend
section: backend
order: 77
phase: "Phase 10 · System design case studies"
summary: Catalog, playback and DRM, a pre-positioned edge CDN, adaptive bitrate, homepage personalization, viewing history and active-active regions.
---

A streaming service is two systems glued at the play button. The control plane (browse, search, recommendations, playback authorization, history) is ordinary microservices traffic at tens of thousands of requests per second. The data plane moves video at a scale where bandwidth, not compute, is the cost: tens of terabits per second at evening peak. Netflix has publicly described exactly this split: control plane on AWS, video served from its own CDN, Open Connect, with appliances placed inside ISP networks. The interview is about keeping the two apart and getting each one right.

## Requirements

### Clarifying questions

- On-demand only, or live events too? Assume on-demand; live changes the CDN and encoding story.
- Who uploads content? Assume a studio ingest pipeline, not user uploads (that is [YouTube]({{ '/backend/design-youtube/' | relative_url }})).
- Which devices? TVs, phones, browsers, consoles, with very different codecs and DRM.
- Offline downloads? Out of scope, but the license design should not block it.

### Functional

- Browse a personalized homepage of rows per profile; view title details; search.
- Play a title on any supported device, starting quickly and adapting to network conditions.
- Enforce plan rules: max resolution, concurrent streams, country licensing windows.
- Resume where you left off on any device ("continue watching"); keep viewing history.

### Non-functional

| Property | Target |
|---|---|
| Members | about 300M accounts, 500M profiles (order of magnitude as of 2025) |
| Daily active | 150M accounts, ~2 hours of viewing each |
| Peak concurrent streams | ~30M |
| Playback start | p95 under 2 s from press to first frame |
| Rebuffering | well under 1% of viewing time |
| Availability | control plane 99.99%; playback degrades but never "all down" |
| Read/write | browse is read-heavy (100:1+); heartbeats are the big write stream |

### Out of scope

Content ingest UI, billing internals, ads, live, games.

## Capacity estimates

**Streaming egress.** 150M accounts × 2 h = 300M stream-hours per day. Average concurrency is 300M / 24 = 12.5M; evening peaks across time zones put it near 30M. At an average 4 Mbps (a mix of phone and 4K):

```text
peak egress  = 30M x 4 Mbps            = 120 Tbps
per hour     = 4 Mbps x 3600 s / 8     = 1.8 GB
daily bytes  = 300M h x 1.8 GB         = 540 PB/day
```

No cloud egress price survives that, which is why the bytes come from appliances inside ISPs. At roughly 100 Gbps per appliance (order of magnitude), peak needs 1,200 fully loaded boxes; with headroom and geographic spread, several thousand.

**Control plane.** 150M × 20 API calls a day (home, rows, details, search) = 3B/day, about 35k QPS average and ~100k at peak. Playback starts: 150M × 4 = 600M/day, about 7k/s average, 20k/s peak, with a license request each.

**Heartbeats.** Each stream reports position every 60 s plus on pause, seek and stop: 30M / 60 s = 500k writes/s at peak. This dominates write load.

**Storage.** Assume 100k hours of content. Each hour is encoded into ~10 video rungs × 3 codecs (H.264, HEVC, AV1) plus dozens of audio and subtitle tracks, about 130 Mbps summed:

```text
encoded  = 130 Mbps x 3600 s / 8 = ~60 GB per content hour
library  = 100k h x 60 GB        = ~6 PB of encodes
masters  = 100k h x ~0.5 TB      = ~50 PB at the origin
history  = 500M profiles x 3,000 rows x 150 B = ~225 TB
```

**Caches.** Localized catalog metadata, 20k titles × 30 locales × 50 KB, is about 30 GB: small enough to hold in memory on every API node. Precomputed homepages for active profiles, 150M × 20 KB, are about 3 TB in a distributed cache.

## API design

```http
GET /v1/profiles/{profileId}/home?device=tv&cursor=r20
200 {"rows": [{"rowId": "continue", "title": "Continue watching",
               "items": [{"titleId": "t81", "artworkUrl": "...",
                          "progressPct": 42}]}],
     "nextCursor": "r40"}

GET /v1/titles/{titleId}?locale=en-US
200 {"titleId": "t81", "type": "series", "seasons": [...],
     "maturity": "TV-14", "availableIn": true}

POST /v1/playback/manifest
{"profileId": "p7", "titleId": "t81", "episodeId": "e3",
 "device": {"codecs": ["av1", "hevc"], "hdr": ["hdr10"],
            "drm": "widevine", "securityLevel": "L1"}
}
200 {"playbackSessionId": "ps_9f2", "startPositionSec": 1312,
     "videoStreams": [{"id": "v12", "codec": "av1",
                       "height": 1080, "kbps": 4300}],
     "audioStreams": [...], "textTracks": [...],
     "cdnUrls": ["https://oca-1.isp-a.example/...", "..."],
     "licenseUrl": "/v1/playback/license", "expiresAt": "..."}

POST /v1/playback/license
{"playbackSessionId": "ps_9f2", "challenge": "<base64 CDM bytes>"}
200 {"license": "<base64 bytes for the CDM>"}

POST /v1/playback/events
{"playbackSessionId": "ps_9f2", "type": "heartbeat",
 "positionSec": 1372, "kbps": 4300, "rebufferMs": 0, "ts": "..."}
202

GET /v1/profiles/{profileId}/history?cursor=...
GET /v1/search?q=dark&profileId=p7
```

The manifest request carries device capabilities, so the server, not the client, decides which streams the device may play. The license endpoint takes an opaque challenge produced by the device's Content Decryption Module (CDM) and returns an opaque license; the API server never sees a usable key in transit to the client.

## Data model

| Data | Store | Key / index | Why |
|---|---|---|---|
| Accounts, plans, profiles | PostgreSQL (or another relational store) | account_id | Strong consistency, low volume |
| Catalog (titles, episodes, rights) | Relational source of truth, published as in-memory snapshots | title_id | Read on every request; tiny |
| Encoded assets | Relational + object storage | (title_id, codec, height, kbps) | Joins for ladder selection |
| Content keys | Key management service / HSM | key_id per asset | Never in general-purpose stores |
| Bookmarks | Cassandra | (profile_id, title_id) | Last-write-wins, multi-region |
| Viewing history | Cassandra | partition profile_id, cluster watched_at DESC | 500k writes/s, append-heavy |
| Homepages | Distributed cache (memcached-style) | profile_id + device | Precomputed, rebuildable |
| Stream leases | Cassandra or Redis with TTL | (account_id, session_id) | Concurrent-stream limits |

Netflix has publicly described using Cassandra for viewing history and EVCache, its memcached-based cache, for this kind of data.

### Catalog and metadata

The catalog is small (gigabytes) but read on every request, with per-country rules: a title may be licensed in India until March and not at all in Germany. Treat it as a read-only dataset, not a database you query:

1. Editors and the rights system write to a relational source of truth.
2. A publisher builds a versioned snapshot every few minutes (titles, localized text, artwork references, availability windows per country, maturity ratings).
3. Every API node loads the snapshot into memory and swaps it atomically. Netflix open-sourced Hollow for exactly this pattern.

A homepage that touches 400 titles then costs 400 hash lookups, not 400 queries. The availability check (`country in window and plan allows`) happens in memory per request, so a title that expires at midnight disappears without a cache purge. Snapshots also give rollback: a bad publish is undone by pinning the previous version.

## High-level design

```text
                 CONTROL PLANE (cloud, 3 regions active-active)
 +--------+ HTTPS +---------+  +----------------------------------+
 | client |------>| gateway |->| BFF / API: home, title, search,  |
 | TV,    |       | (Zuul-  |  | playback, license, events        |
 | phone, |       |  style) |  +----------------------------------+
 | web    |       +---------+     |          |             |
 |        |                       v          v             v
 |        |               +------------+ +--------+ +------------+
 |        |               | catalog    | | KV     | | Kafka      |
 |        |               | snapshot + | | cache  | | -> history |
 |        |               | Postgres   | |        | | -> metrics |
 |        |               +------------+ +--------+ +------------+
 |        |
 |        |  DATA PLANE: byte ranges from the nearest healthy box
 |        |<---------------+-------------------------------------+
 +--------+                | edge appliances in ISPs and IXPs    |
                           | filled off-peak from origin storage |
                           +-------------------------------------+
```

**Browse.** The client calls the BFF, which fetches the precomputed homepage for the profile from cache, filters it against the in-memory catalog for the member's country and maturity settings, merges live rows (continue watching), and hydrates artwork URLs. Images come from a regular CDN.

**Play.** The client asks for a manifest. The playback service checks the plan and stream limits, picks streams the device can decode, asks the steering service for a ranked list of edge appliances holding those files, and returns URLs plus a session. The client requests a license, then fetches segments directly from the edge. After that first second, the control plane is out of the byte path.

**Watch.** Heartbeats go to the events service, which writes the bookmark and publishes to Kafka. Consumers append to viewing history, update "continue watching", feed quality-of-experience metrics and feed the recommendation pipelines.

## Deep dives

### Playback flow and DRM license

1. **Entitlement.** Account active, plan allows the requested resolution, title available in the request's country (from the IP, not a client claim).
2. **Concurrent streams.** Each playback session takes a lease row `(account_id, session_id)` with a TTL of a few minutes, renewed by heartbeats. Start is refused when the count of live leases reaches the plan limit. Expiring leases handle crashed clients without cleanup jobs.
3. **Stream selection.** Intersect the title's ladder with device capability: codec support, HDR format, and DRM security level. A device with software-only DRM (for example Widevine L3) is capped at lower resolution; 4K requires hardware-backed decryption and an HDCP-protected output.
4. **License.** Content is encrypted once with Common Encryption (CENC); with the `cbcs` scheme one set of files serves Widevine, PlayReady and FairPlay devices (older devices that only support the `cenc` scheme need a second copy). The CDM generates a challenge; the license service validates the playback session, looks up the content keys, and returns a license that wraps the keys for that CDM along with a policy: expiry, allowed output protection, max resolution. Keys are decrypted only inside the CDM.

Separating manifest and license lets you keep the license service small, heavily audited, and backed by a key store. It also lets a license expire and renew mid-session, which is how you revoke a stream when a plan downgrades or a lease is lost.

### Open Connect-style CDN with pre-positioning

A general CDN pulls on a miss. A catalog service can do better because demand is predictable: tomorrow's popular titles in São Paulo are mostly today's, plus scheduled launches.

- **Placement.** Each night a popularity model predicts per-region demand per file. Hot files are copied to many boxes; long-tail files go to fewer boxes with large disks. Inside a cluster, files are spread by [consistent hashing]({{ '/backend/consistent-hashing/' | relative_url }}) so each box owns a predictable slice.
- **Fill.** Appliances download new and changed files during a local off-peak window, from the origin or from peer appliances, so fill traffic never competes with evening viewing.
- **Tiers.** Appliances embedded in ISPs serve most bytes; larger sites at internet exchanges catch misses; the origin is a last resort.
- **Steering.** At manifest time the steering service picks boxes that hold the files, are healthy (reporting load and health continuously), and are close in network terms (BGP routes the ISP announces to the box). It returns a ranked list; the client fails over down the list on errors without asking again.

The payoff: 90%+ of bytes can be served from inside the viewer's ISP, and a new season can launch at midnight with every hot file already in place. See [distributed caching and CDN]({{ '/backend/distributed-caching-and-cdn/' | relative_url }}) for the pull-through model this replaces.

### Adaptive bitrate streaming

Each title is encoded into a ladder of renditions cut into segments of a few seconds, described by a DASH or HLS manifest. Netflix has publicly described per-title (and later per-shot) encoding: a cartoon needs far fewer bits than a grainy war film for the same quality, so the ladder is chosen per title by measured quality, not a fixed table.

The player chooses the next segment's rung. Throughput-only rules oscillate and stall on sudden drops; buffer-based rules (Huang et al., SIGCOMM 2014, with Netflix authors) use buffer level as the main signal. A runnable sketch combining both:

```python
# Illustrative ladder in kbps; real ladders differ per title and codec.
LADDER = [235, 375, 750, 1750, 3000, 4300, 5800, 8000, 16000]


def choose_bitrate(buffer_s, est_kbps, ladder=LADDER,
                   reservoir=10.0, cushion=40.0, safety=0.8):
    """Buffer-based pick (BBA-style), capped by a throughput estimate."""
    if buffer_s <= reservoir:                      # protect against stalls
        return ladder[0]
    frac = min(1.0, (buffer_s - reservoir) / cushion)
    by_buffer = ladder[int(frac * (len(ladder) - 1))]
    affordable = [r for r in ladder if r <= safety * est_kbps] or ladder[:1]
    return min(by_buffer, affordable[-1])


def simulate(trace_kbps, chooser, seg_s=4.0, max_buffer=60.0):
    """One segment per trace step; returns (rebuffer seconds, picks).
    Waiting for the first segment is startup delay, not a rebuffer."""
    buffer, stalls, est, picks = 0.0, 0.0, trace_kbps[0], []
    for bw in trace_kbps:
        rate = chooser(buffer, est)
        download_s = rate * seg_s / bw
        if picks:
            stalls += max(0.0, download_s - buffer)
        picks.append(rate)
        buffer = min(max(0.0, buffer - download_s) + seg_s, max_buffer)
        est = bw                              # last measured throughput
    return stalls, picks


if __name__ == "__main__":
    trace = [25000] * 40 + [3000] * 30 + [25000] * 20  # fast, slow, fast
    stalls, picks = simulate(trace, choose_bitrate)
    assert stalls == 0, stalls
    assert picks[0] == LADDER[0]              # fast start on lowest rung
    assert max(picks[:40]) == LADDER[-1]      # climbs to the top rung
    assert max(picks[45:70]) <= 3000          # backs off when congested
    naive_stalls, _ = simulate(trace, lambda b, e: LADDER[-1])
    assert naive_stalls > 60                  # always-max player stalls
    print("ok", stalls, naive_stalls)
```

The backend's part: produce good ladders, keep segment boundaries aligned across rungs so switching is seamless, and collect per-session QoE (startup time, rebuffer ratio, average bitrate) to tune the algorithm by A/B test.

### Personalization and homepage assembly

Netflix has publicly described an offline / nearline / online split for recommendations, and personalizing artwork per member. A reasonable design:

- **Offline (hours).** Batch jobs train models and, for each active profile, generate candidate rows ("Because you watched X", genres, trending) and rank titles within each row.
- **Nearline (minutes).** Stream consumers react to events: finishing a series removes it from "continue watching" and refreshes "because you watched" rows. Results overwrite the cached homepage.
- **Online (milliseconds).** The BFF reads the cached page, applies country availability and maturity filters from the in-memory catalog, removes duplicates across rows, orders rows for the device (TVs show fewer rows above the fold), and picks artwork variants.

The online step has a strict budget, around 100–200 ms of the request. If the cached page is missing or the personalization call times out, return a precomputed "popular in your country" page. A slightly generic homepage costs a little engagement; an error page costs the session.

### Viewing history

Two different things live here: the **bookmark** (last position per profile and title, overwritten constantly) and the **history** (an append-only list of what was watched).

- Heartbeats go to the events service, which writes the bookmark directly with last-write-wins on the event timestamp, then publishes to Kafka keyed by profile_id so per-profile order holds within a partition.
- A consumer appends history rows: partition key `profile_id`, clustering key `watched_at DESC`. Reads of the recent page are a single-partition slice.
- Very long histories make wide partitions. Netflix has publicly described splitting live recent history from compressed older history; the same idea works anywhere: roll rows older than N days into a compressed blob per profile.
- Two devices on one profile are a real conflict. Last-write-wins by event time is acceptable: the user resumes from the most recent position they actually watched.

## Scaling and bottlenecks

| Scale | What breaks first | Fix |
|---|---|---|
| 10× streams | Edge capacity in specific ISPs at peak | More appliances there; better popularity prediction; more efficient codecs (AV1 cuts bits for the same quality) |
| 10× streams | Heartbeat ingestion | Batch on the client (send every 60 s, not every 10), coalesce bookmark writes in the events service, partition Kafka wider |
| 10× launches | Thundering herd at a premiere | Pre-position the files, pre-warm homepage caches, pre-scale playback and license services ahead of the launch time |
| 100× | Homepage compute | Recompute only for active profiles; incremental nearline updates instead of full nightly rebuilds |
| 100× | License service hot path | Cache entitlement per session; scale stateless license workers; keep the key store in-region |

## Reliability and failure modes

| Failure | Effect | Mitigation |
|---|---|---|
| Edge appliance dies mid-stream | Segment fetch fails | Client moves to the next URL in its ranked list; buffer hides the switch |
| Personalization down | No custom homepage | Fallback to cached or popular page |
| License service slow | Playback cannot start | Separate capacity pool; renewals continue with grace periods for active sessions |
| History store slow | Heartbeats back up | Kafka absorbs; bookmark writes are best effort; playback never waits on them |
| Region outage | One third of control plane | Evacuate traffic to the other regions |

### Microservices resilience

A homepage call fans out to dozens of services; at that fan-out, something is always slow. Netflix has publicly described the tools it built: Hystrix (circuit breakers, bulkheads, fallbacks, now in maintenance mode, its ideas living on in libraries like resilience4j), Chaos Monkey for killing instances in production, and prioritized load shedding at the gateway. The rules that matter:

- Every call has a timeout shorter than the caller's budget, and a fallback: stale cache, a default row, or omission.
- Bulkhead thread or connection pools per dependency, so one slow service cannot exhaust the caller.
- Critical-path tiers. Playback start outranks browse, which outranks analytics. Under overload, the gateway sheds the low tiers first.
- Retries only with budgets and jitter, or they amplify the outage. See [fault tolerance patterns]({{ '/backend/fault-tolerance-patterns/' | relative_url }}).

### Multi-region active-active

Netflix has publicly described running active-active in three AWS regions and practicing region evacuation. The design:

- DNS / global load balancing sends each member to a nearby region; any region can serve any member.
- Cassandra replicates across regions; writes use `LOCAL_QUORUM` so latency stays in-region and other regions catch up asynchronously. Data with last-write-wins semantics (bookmarks, history) tolerates this.
- Caches are filled locally, with invalidations or writes replicated across regions.
- Account and billing changes, which do need a single answer, go through a store with a clear primary per record, or tolerate a short delay after a write (sign-up then immediately play is the hard case; route that member's session to the region that took the write).
- Each region runs with enough headroom (or autoscales fast enough) to absorb a failed region's share. Evacuation is drilled regularly, because a failover path you never run does not work.

Video bytes are unaffected by a region loss: the edge appliances keep serving, and only new playback starts depend on the control plane.

## Trade-offs

| Decision | Option | Chosen? | Why |
|---|---|---|---|
| Video delivery | Commercial pull CDN | No | Egress cost at 100+ Tbps; misses on launch day |
| Video delivery | Own appliances in ISPs, pre-positioned | Yes | Predictable demand, cheapest bytes, ISP-local |
| Catalog reads | Query DB per request | No | Hundreds of lookups per homepage |
| Catalog reads | In-memory snapshot on every node | Yes | Microsecond reads, atomic swap, easy rollback |
| Homepage | Fully online ranking | No | Too slow and fragile on the request path |
| Homepage | Precompute + nearline + light online step | Yes | Fast, with a safe fallback |
| History store | Relational | No | 500k writes/s, multi-region writes |
| History store | Cassandra, LWW | Yes | Write scale, local quorums |
| Regions | Active-passive | No | Idle capacity, untested failover |
| Regions | Active-active with evacuation | Yes | Capacity used daily; failover exercised |

## Interview follow-ups

**How do you enforce "2 screens at a time"?** A lease per playback session with a TTL renewed by heartbeats; refuse a new manifest or license when live leases reach the limit. Expiry handles crashed devices. Strict global accuracy is not worth cross-region locks; a few seconds of overrun is fine.

**What happens when a new season launches at midnight?** Files are pre-positioned days ahead, homepage caches are warmed, and services pre-scale for the known start time. The launch is a capacity event you scheduled, not a surprise.

**Why encrypt once with CENC instead of per DRM system?** With the `cbcs` scheme, one set of encrypted files on every edge box serves Widevine, PlayReady and FairPlay devices; only the license differs. That halves or thirds storage and cache footprint.

**Client says its bandwidth is 50 Mbps; do you trust it?** The client chooses bitrate from what it measures, but the server caps the allowed streams by plan, device security level and country. Trust the client for performance, never for entitlement.

**How do you avoid stale "continue watching" after a binge on another device?** Bookmarks are written synchronously on heartbeat and the row is assembled online from the bookmark store, not from the nightly precompute.

**An edge box serves corrupted segments. How do you find out?** Clients report segment errors and checksum failures with the box ID in QoE events; the steering service drains boxes whose error rate spikes, and fill verifies checksums on write.

