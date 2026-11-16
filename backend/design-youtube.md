---
title: Design a YouTube backend
section: backend
order: 76
phase: "Phase 10 · System design case studies"
summary: "Resumable uploads, a DAG-based transcoding pipeline, HLS/DASH adaptive streaming, CDN tiers, view counting, metadata search and recommendations."
---

A video platform is two systems joined at object storage. The upload side is a compute-heavy batch pipeline: accept multi-gigabyte files over unreliable networks, then turn each into dozens of encoded renditions. The playback side is a bandwidth problem: deliver those renditions to millions of concurrent viewers, adapting to each viewer's network second by second. The interview usually goes deep on the transcoding pipeline and adaptive streaming. The structure follows [the design framework]({{ '/backend/design-framework/' | relative_url }}); [Design a Netflix backend]({{ '/backend/design-netflix/' | relative_url }}) covers the curated-catalog variant.

## Requirements

### Clarifying questions

- Video on demand only, or live? VOD; live is a follow-up.
- Maximum upload size? Assume up to 256 GB or 12 hours.
- Target devices? Phones, browsers, TVs: multiple resolutions and codecs.
- How fresh must view counts be? Minutes; accuracy beats freshness, so fraudulent views are removed.

### Functional

1. Upload videos with resumable, chunked transfers.
2. Transcode into multiple resolutions and codecs.
3. Stream with adaptive bitrate on any device.
4. Count views.
5. Search videos by metadata.
6. Recommend videos (high level).

### Non-functional

| Property | Target |
|---|---|
| Upload volume | 500 hours of video per minute (YouTube has publicly cited this order) |
| Watch time | 1B hours/day (also a publicly cited order of magnitude) |
| Views | 5B/day |
| Time to first frame | p95 < 2 s |
| Rebuffering | < 1% of watch time |
| Upload to watchable | Minutes for a typical video |
| Availability | 99.99% playback, 99.9% upload |
| Durability | Uploaded originals never lost |

### Out of scope

Live streaming, ads, monetization, comments, creator analytics, copyright policy details.

## Capacity estimates

**Uploads.** 500 hours/minute = 720,000 hours/day. At about 10 minutes per video, that's 4.3M videos/day ≈ 50 uploads/s. At an average upload bitrate of 5 Mbps (2.25 GB/hour), ingress is 720,000 × 2.25 GB ≈ 1.6 PB/day ≈ 150 Gbps.

**Storage.** Keep originals: 1.6 PB/day. A full ladder across several resolutions and two or three codecs totals roughly 3 GB per hour of video: 2.2 PB/day. Together that's about 3.8 PB/day, 1.4 EB/year before replication or erasure coding. Cold tiering of originals and rarely watched renditions is mandatory.

**Transcoding compute.** If a full ladder costs about 20 CPU-hours per hour of video (modern codecs are expensive), that's 14.4M CPU-hours/day, about 600,000 cores running continuously. This is why large platforms invest in hardware encoders; Google has publicly described custom video transcoding chips for YouTube.

**Egress.** 1B watch-hours/day ÷ 24 ≈ 42M concurrent streams on average. At 2.5 Mbps average, that's about 100 Tbps. No origin can serve that; the CDN is the product.

**Views and metadata.** 5B views/day ≈ 58,000/s, about 150,000/s at peak. Watch-page metadata reads track views. Metadata itself is small: 4.3M videos × 5 KB ≈ 22 GB/day.

## API design

Uploads use a resumable session protocol. Google's resumable upload protocol and the open tus protocol work the same way: create a session, send byte ranges, and ask the server for its offset after a failure.

```http
POST /upload/v1/sessions
Authorization: Bearer <token>
Content-Type: application/json

{"filename": "trip.mp4", "size": 734003200, "content_type": "video/mp4"}
```

```http
PUT /upload/v1/sessions/up_8f2c
Content-Length: 8388608
Content-Range: bytes 8388608-16777215/734003200

HTTP/1.1 308 Resume Incomplete
Range: bytes=0-16777215
```

After a dropped connection, the client sends `Content-Range: bytes */734003200` with an empty body; the server replies with the committed `Range`, and the client continues from there. Google's protocol reuses status 308 for "incomplete"; tus uses `HEAD` and an `Upload-Offset` header instead.

| Endpoint | Purpose |
|---|---|
| `POST /v1/videos` | `{upload_id, title, description, tags, visibility}` → `{video_id, status: "processing"}` |
| `GET /v1/videos/{id}` | Metadata, status, manifest URLs, thumbnails |
| `GET {cdn}/v/{id}/master.m3u8`, `manifest.mpd` | HLS and DASH manifests |
| `POST /v1/videos/{id}/views` | View and watch-progress events, batched by the player |
| `GET /v1/search?q=&cursor=` | Search |
| `GET /v1/recommendations?context=&cursor=` | Home and up-next |

```json
{
  "video_id": "a1B2c3D4e5F",
  "title": "Trip to the coast",
  "status": "ready",
  "duration_s": 642,
  "manifests": {
    "hls": "https://cdn.example.com/v/a1B2c3D4e5F/master.m3u8",
    "dash": "https://cdn.example.com/v/a1B2c3D4e5F/manifest.mpd"
  },
  "view_count": 18234
}
```

## Data model

| Data | Store | Key | Notes |
|---|---|---|---|
| `videos` | Sharded MySQL/PostgreSQL | `video_id`; index `(channel_id, published_at)` | Title, status, visibility, duration |
| `renditions` | Same shard as video | `(video_id, codec, height)` | Bitrate, segment prefix, manifest path |
| `upload_sessions` | Redis + durable row | `upload_id` | Size, committed offset, multipart ID, part checksums, expiry |
| `transcode_jobs`, `tasks` | PostgreSQL or a workflow engine | `(job_id, task_id)` | State, attempts, lease expiry, output key |
| View counters | Bigtable/Cassandra-style counters, cached | `video_id` | Updated by stream aggregation |
| Raw view events | Kafka → warehouse | event ID | Source of truth for counts and recommendations |
| Search documents | Elasticsearch/OpenSearch | `video_id` | Built from metadata via change data capture |
| Media | Object storage | `raw/{id}/source`, `media/{id}/{codec}_{height}/seg_00042.m4s` | Immutable |

Metadata is relational and modest in size, so a sharded relational store fits; YouTube publicly created Vitess to scale MySQL horizontally. Bytes never touch the database. Task state needs transactional claims (`SELECT ... FOR UPDATE SKIP LOCKED` works well at this scale, see [Postgres as a queue]({{ '/postgresql/postgres-as-a-queue/' | relative_url }})), or you delegate it to a workflow engine such as Temporal.

## High-level design

```text
 UPLOAD
 creator --chunks--> Upload svc ------------> object storage (raw)
                         | upload.complete
                         v
                  +--------------+  tasks   +-------------------+
                  | Transcode    |--------->| task queue        |
                  | orchestrator |<---------| (leases, retries) |
                  | (DAG state)  |  done    +---------+---------+
                  +------+-------+                    v
                         |              +-------------------------+
                         |              | encoder workers         |
                         |              | (CPU, GPU, hardware)    |
                         |              +------------+------------+
                         | video.ready               v
                         v                 object storage (segments,
                  Metadata DB --CDC--> Kafka         manifests)
                                         +--> search indexer
                                         +--> recommendation features

 PLAYBACK
 viewer --> API: watch page (metadata, manifest URL, up-next)
   |
   +--> CDN edge --miss--> regional cache --miss--> origin storage
   |    manifests + segments; the player picks the bitrate
   |
   +--> view events --> Kafka --> stream aggregation --> view counters
```

### Upload flow

1. The client creates an upload session and sends 8 MB chunks; the upload service writes each as a part of an object-storage multipart upload and records the committed offset.
2. On the final chunk, the service completes the multipart upload, verifies the checksum, and emits `upload.complete`.
3. The creator submits metadata at any point; the video row starts as `processing`.
4. The orchestrator builds the transcoding DAG and schedules tasks. Low renditions publish first, so the video becomes watchable before the full ladder finishes.

### Playback flow

1. The watch page API returns metadata, the manifest URL (pointing at a CDN host chosen for the viewer), and recommendations.
2. The player fetches the manifest, starts at a conservative bitrate, and requests segments from the CDN.
3. The player adapts quality per segment based on throughput and buffer level, and batches view and progress events to the API.

## Deep dives

### Chunked and resumable uploads

A 5 GB upload over a phone connection will be interrupted. Restarting from zero is unacceptable, so the protocol is resumable by design:

- **Fixed-size chunks.** 8 MB is a common choice; Google's protocol requires multiples of 256 KiB. Each chunk carries a checksum (CRC32C or MD5), which the server verifies before committing it.
- **Server-owned offset.** The server, not the client, says what is committed. After any failure, the client asks for the offset and resumes. Sessions expire after about a week; parts of abandoned sessions are garbage-collected by an object-storage lifecycle rule.
- **Parallel parts.** Over fast links, the client uploads several chunks in parallel as numbered parts of an S3-style multipart upload. S3 documents a minimum part size of 5 MiB (except the last) and at most 10,000 parts, which bounds the chunk size for very large files.
- **Direct to storage.** For scale, the upload service hands out presigned part URLs and bytes bypass application servers entirely; the service only tracks parts.
- **Idempotency.** Re-sending a committed chunk is harmless: same range, same bytes, same checksum.

The same pattern applies to any large-file product ([Design Google Drive]({{ '/backend/design-google-drive/' | relative_url }})).

### Transcoding pipeline as a DAG of workers

Transcoding one long video on one machine would take hours. Instead, the orchestrator expands each upload into a directed acyclic graph of small, independent tasks:

1. **Probe:** read container, codecs, duration, frame rate; reject corrupt files early.
2. **Split:** cut the source into chunks of tens of seconds at keyframes, so each chunk decodes independently.
3. **Encode:** one task per chunk per rendition, run in parallel across the worker fleet.
4. **Side tasks:** audio extraction and encoding, thumbnail generation, speech-to-text captions, content fingerprinting for copyright matching.
5. **Package:** join chunks per rendition into CMAF segments, then write HLS and DASH manifests.
6. **Publish:** flip the video to `ready` and emit `video.ready`.

Chunking gives two things: parallelism (an hour of video encodes in minutes) and retry granularity (a preempted worker loses 30 seconds of work, not an hour). The price is keeping quality consistent across chunk boundaries, which encoders handle with closed GOPs and per-title rate control.

The orchestrator stores DAG state in a database. Workers lease tasks with a heartbeat; an expired lease returns the task to the queue. Every task writes to a deterministic output key, so a retry overwrites rather than duplicates. Failures retry with backoff and land in a dead-letter queue after a few attempts ([retries and backoff]({{ '/backend/retries-and-backoff/' | relative_url }})). Priority queues let short or popular uploads jump ahead, preemptible capacity absorbs bulk work, and slow tasks get speculatively re-run on a second worker. Expensive codecs (VP9, AV1) are worth their cost only for videos with enough views to repay it in bandwidth, so they can run later, triggered by popularity.

A runnable sketch of the scheduler, using `graphlib` from the standard library:

```python
import concurrent.futures as cf
import threading
import time
from graphlib import TopologicalSorter

RENDITIONS = ["360p", "720p", "1080p", "2160p"]


def build_dag(chunks):
    """task -> set of tasks it depends on."""
    dag = {"probe": set()}
    for task in ("split", "audio", "thumbnails", "fingerprint"):
        dag[task] = {"probe"}
    for r in RENDITIONS:
        encodes = {f"encode:{r}:{c}" for c in range(chunks)}
        for task in encodes:
            dag[task] = {"split"}
        dag[f"package:{r}"] = encodes | {"audio"}
    dag["manifest"] = {f"package:{r}" for r in RENDITIONS}
    dag["publish"] = {"manifest", "thumbnails", "fingerprint"}
    return dag


def run_dag(dag, work, workers=8, max_attempts=3):
    ts = TopologicalSorter(dag)
    ts.prepare()                         # raises CycleError on bad DAGs
    attempts, finished, running = {}, [], {}
    with cf.ThreadPoolExecutor(workers) as pool:
        while ts.is_active():
            for task in ts.get_ready():  # all dependencies are done
                attempts[task] = 1
                running[pool.submit(work, task)] = task
            done, _ = cf.wait(running, return_when=cf.FIRST_COMPLETED)
            for fut in done:
                task = running.pop(fut)
                if fut.exception() is None:
                    ts.done(task)
                    finished.append(task)
                elif attempts[task] < max_attempts:
                    attempts[task] += 1   # outputs are idempotent: rerun
                    running[pool.submit(work, task)] = task
                else:
                    raise RuntimeError(f"{task} failed {max_attempts} times")
    return finished, attempts


def make_worker(fail_counts):
    calls, lock = {}, threading.Lock()

    def work(task):
        with lock:
            calls[task] = calls.get(task, 0) + 1
            n = calls[task]
        if n <= fail_counts.get(task, 0):
            raise OSError(f"{task}: worker preempted")
        time.sleep(0.001)                # pretend to encode
        return task

    return work


if __name__ == "__main__":
    dag = build_dag(chunks=6)
    flaky = {"encode:1080p:3": 1, "audio": 2}
    order, attempts = run_dag(dag, make_worker(flaky))
    pos = {task: i for i, task in enumerate(order)}
    assert len(order) == len(dag) == 5 + 4 * 6 + 4 + 2
    assert all(pos[dep] < pos[t] for t, deps in dag.items() for dep in deps)
    assert order[0] == "probe" and order[-1] == "publish"
    assert attempts["encode:1080p:3"] == 2 and attempts["audio"] == 3
    try:
        run_dag(dag, make_worker({"split": 5}))
        raise AssertionError("expected failure")
    except RuntimeError as err:
        assert "split" in str(err)
    print(len(order), "tasks; retried:",
          {t: n for t, n in attempts.items() if n > 1})
```

### Adaptive bitrate streaming: HLS and DASH

The video is encoded into a **bitrate ladder** (illustrative H.264 values: 360p near 0.7 Mbps, 720p near 2.5 Mbps, 1080p near 5 Mbps, 4K near 15–20 Mbps; VP9 and AV1 need noticeably less for the same quality). Every rendition is cut into segments of 2–6 seconds with aligned keyframes, so the player can switch rendition at any segment boundary.

- **HLS** (Apple): a master playlist (`.m3u8`) lists variants with bandwidth and resolution; each variant has a media playlist of segment URLs. Required for Apple devices.
- **DASH** (MPEG standard): an XML manifest (`.mpd`) describing representations; codec-agnostic and common on Android, smart TVs and browsers via Media Source Extensions.
- **CMAF**: a common fragmented-MP4 segment format both can reference, so one set of segments serves both protocols. That halves storage and CDN cache footprint compared with keeping separate TS and MP4 copies.

```text
#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
360p/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2800000,RESOLUTION=1280x720
720p/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
1080p/index.m3u8
```

Real playlists also declare `CODECS`. The adaptation logic lives in the player, not the server. Throughput-based players estimate bandwidth from recent segment downloads; buffer-based players choose quality from how many seconds are buffered; production players blend both (dash.js ships BOLA, a buffer-based algorithm). The priorities are: start fast at a low rung, never stall, then climb, and avoid oscillating. Segment length is a trade-off: short segments adapt faster and enable lower live latency, but mean more requests and slightly worse compression.

### CDN

Popularity is a steep head with a very long tail. Segments are immutable, so they cache with long TTLs under a key per segment, and caching works in tiers ([distributed caching and CDN]({{ '/backend/distributed-caching-and-cdn/' | relative_url }})):

- **Edge caches** close to viewers hold the head. Google publicly documents Google Global Cache, servers placed inside ISP networks; Netflix's Open Connect is the equivalent public example.
- **Regional caches and an origin shield** catch edge misses and collapse concurrent requests for the same segment into one origin fetch.
- **Origin** object storage serves the long tail.

The control plane steers each player to a good cache: the watch API returns per-viewer CDN hostnames based on location, load and health, and the player fails over to an alternate host on segment errors. Segment URLs carry signed, expiring tokens to stop hotlinking. Popular new uploads can be pre-positioned to edges off-peak. Egress is the largest cost in the system, which is why better codecs for popular videos pay for themselves: encode cost is paid once, bandwidth savings accrue on every view.

### View counting

A view counts only after a minimum watch and after passing fraud filters; YouTube doesn't publish its exact rules. The pipeline:

1. The player batches events (start, progress heartbeats, end) with a session ID and posts them to the API, which writes to Kafka.
2. A stream processor validates and dedupes by `(session_id, video_id)`, applies rate and bot heuristics, and aggregates per video per 10-second window.
3. One counter increment per video per window updates the counter store; the watch page reads the cached value.
4. A daily batch job recomputes counts from raw events with stricter fraud models and corrects the counters. Counts can therefore go down, which platforms do publicly when they remove spam views.

Pre-aggregation solves the hot-key problem: a viral video receiving 100,000 views/s produces one write per window, not 100,000. Partition Kafka by session, not video, so the viral video doesn't overload a single partition, and aggregate in two stages. Exactly-once counting comes from idempotent window updates or a stream processor with transactional sinks ([delivery semantics]({{ '/backend/delivery-semantics/' | relative_url }})).

### Metadata and search

Metadata (title, description, tags, channel, status, visibility) lives in the sharded relational store and is heavily cached: watch-page reads are 10^5/s, writes are rare. Public metadata can even be cached at the CDN for short TTLs.

Search is a separate read model. Change data capture streams metadata changes into Kafka; an indexer builds documents (title, description, tags, channel name, and auto-generated captions, which make spoken content searchable) and writes them to an inverted-index cluster sharded by document and replicated for QPS. Queries scatter to shards and gather results. Ranking combines text relevance (BM25-style) with quality and engagement signals (watch time, freshness, channel authority) and light personalization. Private and unlisted videos are filtered at query time, and visibility changes propagate in seconds. Autocomplete is served from precomputed top-k completions per prefix built from query logs. The general mechanics are in [Design a search engine]({{ '/backend/design-search-engine/' | relative_url }}).

### Recommendations

Most watch time on large video platforms comes from recommendations, so this is a core system, but interviews only expect the shape. YouTube's publicly described architecture ("Deep Neural Networks for YouTube Recommendations", RecSys 2016) has two stages:

1. **Candidate generation** narrows millions of videos to hundreds, using the user's watch and search history embedded into a vector and matched to video embeddings (approximate nearest neighbor), plus sources like subscriptions, co-watch, and trending.
2. **Ranking** scores those hundreds with a richer model and many features (video age, past interactions with the channel, predicted watch time), then applies filters (already watched, policy) and diversity rules.

Training runs offline on view logs from the same Kafka streams that feed counters. Serving combines precomputed candidates with real-time features from a feature store, within a budget of a few hundred milliseconds. Changes ship behind online A/B tests measured on long-term satisfaction metrics, not only clicks, because optimizing clicks alone rewards clickbait.

## Scaling and bottlenecks

**At 10× uploads.** Transcoding compute breaks first. Autoscale workers on queue depth, push more work onto preemptible capacity and hardware encoders, and get stricter about which videos earn expensive codecs. Storage growth forces aggressive tiering: originals and cold renditions move to archival storage, and the rarest renditions can be re-generated on demand.

**At 10× views.** Egress and edge capacity break first. Add ISP-embedded caches and edge capacity, tune shield tiers to protect the origin, and lean on better codecs. View counting scales easily because of pre-aggregation.

**At 100×.** Ingest moves to multiple regions near creators, origin storage replicates across regions, metadata becomes globally replicated with regional read replicas, and the recommendation and search stacks run per region ([scaling and load balancing]({{ '/backend/scaling-and-load-balancing/' | relative_url }})).

## Reliability and failure modes

| Failure | Behavior |
|---|---|
| Upload interrupted | Client queries offset and resumes; session lives about a week |
| Worker preempted | Lease expires, task retried; deterministic output keys make it idempotent |
| Corrupt or unsupported source | Probe fails fast; video marked `failed`; creator notified |
| Bad encoder release | Canary on a fraction of jobs, compare objective quality scores (such as VMAF) before full rollout; keep ability to re-transcode |
| Orchestrator crash | DAG state is in the database; a new instance resumes from it |
| CDN edge failure | Player fails over to an alternate host; steering removes the edge |
| Origin region loss | Replicated object storage; edges keep serving the cached head |
| View pipeline lag | Counts are stale for a while; playback unaffected |

Watch quality-of-experience metrics from players (startup time, rebuffer ratio, average bitrate, playback failures) alongside backend metrics such as queue depth and encode latency ([performance monitoring]({{ '/backend/performance-monitoring/' | relative_url }})).

## Trade-offs

| Decision | Option | Chosen? | Why |
|---|---|---|---|
| Upload | Resumable chunked, direct to storage | Yes | Survives network loss; app servers don't proxy bytes |
| Transcoding | Chunked DAG across a fleet | Yes | Parallelism, fine-grained retries |
| | One job per video | No | Hours per video; a failure restarts everything |
| Publishing | Progressive, low renditions first | Yes | Watchable within minutes |
| Packaging | CMAF segments for both HLS and DASH | Yes | One set of bytes, half the cache footprint |
| Codecs | H.264 for all, VP9/AV1 for popular | Yes | Encode cost justified by egress saved |
| ABR | Client-side adaptation | Yes | The player sees its network directly |
| View counts | Stream pre-aggregation + batch correction | Yes | No hot rows; fraud corrected later |
| Search | Separate index via CDC | Yes | Keeps the metadata DB small and transactional |

## Interview follow-ups

**How quickly can a new upload be watched?**
Within minutes for typical videos: chunks encode in parallel and low renditions publish first. Higher resolutions and expensive codecs arrive later.

**Why split the video before encoding?**
Parallelism and retry granularity. The cost is ensuring chunks start on keyframes and quality stays consistent across boundaries.

**HLS or DASH?**
Both, from the same CMAF segments. HLS is required for Apple devices; DASH is codec-agnostic and widely used elsewhere. Only the manifests differ.

**How do you stop view-count fraud?**
Count only after a minimum watch, dedupe per session, filter by rate and behavior in the stream, and re-verify in a daily batch job that can lower counts.

**How would you add live streaming?**
Ingest over RTMP or SRT to regional ingest servers, transcode in real time per stream with no chunked DAG, publish short segments (low-latency HLS uses partial segments), and update manifests continuously at short CDN TTLs.

**How do you reduce CDN cost?**
Better codecs for videos that are watched most, per-title ladders that skip unnecessary rungs, ISP-embedded caches to cut transit, and higher edge hit ratios through tiered caching.

**What if the phone switches networks mid-upload?**
The upload session is independent of the connection. The client reconnects, asks for the committed offset, and continues.

**How do you detect copyrighted content?**
A fingerprinting task in the DAG compares audio and video fingerprints against a reference database before publishing, and the publish step applies the rights holder's policy (block, track, or monetize).
