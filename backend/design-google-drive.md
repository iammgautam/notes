---
title: Design a file storage system (Google Drive)
section: backend
order: 85
phase: "Phase 10 · System design case studies"
summary: "Cloud file storage and sync: content-defined chunking, dedup, resumable uploads, metadata, sync conflicts, versions, sharing and storage tiers."
---

A Drive or Dropbox-style system looks like "upload a file, download a file". The interesting parts are underneath: splitting files into chunks so a one-byte edit does not re-upload a gigabyte, keeping many devices in sync without losing edits, making uploads survive flaky networks, and storing an exabyte cheaply without ever losing a byte. The split that drives the whole design: **metadata** (small, transactional, strongly consistent) and **blocks** (huge, immutable, content-addressed).

## Requirements

### Clarifying questions

- Consumer product with desktop sync clients, web and mobile? Yes.
- Maximum file size? 50 GB.
- Real-time collaborative editing (Google Docs)? Out of scope; that is an operational transform or CRDT problem.
- Offline edits? Yes, desktop clients sync when they reconnect.
- Version history? Yes, 30 days or 100 versions per file.

### Functional

- Upload, download, create folders, move, rename, delete (with trash).
- Sync changes across a user's devices within seconds.
- Share files and folders with users, groups or links, with roles (viewer, commenter, editor, owner).
- Version history and restore.

### Non-functional

- 200M registered users, 50M DAU; average 5 GB stored per user.
- Durability: never lose a committed file (eleven nines from the object store).
- Metadata availability 99.95%; strong consistency per namespace (a user's tree must not fork).
- Change propagation to other online devices: p99 under 10 s.
- Uploads resume after network failure without restarting.

### Out of scope

Real-time co-editing, full-text search inside documents (a separate indexing pipeline), previews and thumbnails beyond a mention, abuse scanning.

## Capacity estimates

```text
Logical data   200M users x 5 GB             = 1 EB
Physical       erasure coding ~1.5x, minus dedup/compression savings
               → on the order of 1.2-1.5 EB raw disk
File writes    50M DAU x 4 changes/day        = 200M/day ≈ 2,300/s avg
               peak 3x                         ≈ 7,000/s
Ingress        200M x ~2 MB of new chunks      = 400 TB/day ≈ 4.6 GB/s
               ≈ 37 Gbps avg, ~110 Gbps peak
Egress         ~3x ingress (other devices, shares) ≈ 14 GB/s avg
Metadata       200M users x 2,000 files x ~500 B ≈ 200 TB + versions
Chunk index    1 EB / ~4 MB avg chunk ≈ 250B chunks x ~100 B ≈ 25 TB
Connections    50M DAU, ~40% online at peak with ~1.5 devices
               ≈ 30M notification connections
               at ~100k per gateway node ≈ 300 nodes (+ headroom)
```

Metadata at hundreds of terabytes means a sharded database from day one; blocks at exabyte scale mean object storage with tiering; and the notification fleet is a distributed system of its own.

## API design

```http
POST /v1/uploads
Content-Type: application/json

{"parent_id": "fld_91", "name": "q3-plan.pptx", "size": 48211337,
 "base_version": 7, "chunks": ["sha256:9f2c...", "sha256:01ab...", "sha256:77e0..."]}
```

```json
{"upload_id": "up_5c1", "missing": [1, 2],
 "urls": ["https://blocks.example.com/up_5c1/1?sig=...",
          "https://blocks.example.com/up_5c1/2?sig=..."],
 "expires_at": "2026-11-23T10:00:00Z"}
```

| Endpoint | Purpose |
|---|---|
| `PUT <signed url>` | Upload one chunk directly to the block service; idempotent by hash |
| `GET /v1/uploads/{id}` | Resume: which chunks have arrived |
| `POST /v1/uploads/{id}/commit` | Atomically create the new version; `409` if `base_version` is stale |
| `GET /v1/files/{id}/manifest?version=` | Chunk list plus short-lived signed download URLs |
| `GET /v1/changes?cursor=` | Delta sync: entries since cursor, new cursor, `has_more` |
| `GET /v1/changes/longpoll?cursor=` | Blocks until something changes (or use the WebSocket) |
| `POST /v1/files/{id}/permissions` | `{principal, role}` or create a link |
| `POST /v1/files/{id}/versions/{v}:restore` | Restore as a new version |

## Data model

| Data | Store | Why |
|---|---|---|
| File tree, versions, journal, ACLs | Sharded relational DB (MySQL/Postgres via Vitess or Citus, or Spanner-class) sharded by namespace | Moves and renames need transactions; uniqueness per folder; strong consistency |
| Chunk index | Key-value store sharded by hash | Point lookups by hash; huge cardinality |
| Chunk bytes | Object storage (in-house or S3/GCS-class), packed into large extents | Cheap, durable, tierable |
| Notification routing | In-memory on gateways + pub/sub | Ephemeral |

A **namespace** is a unit of consistency: a user's root, or a shared folder or shared drive. All of a namespace's metadata lives on one shard so operations inside it are single-shard transactions.

```sql
-- Illustrative metadata schema, sharded by ns_id
CREATE TABLE files (
  ns_id      bigint NOT NULL,
  file_id    bigint NOT NULL,
  parent_id  bigint,
  name       text   NOT NULL,
  is_folder  bool   NOT NULL,
  head_version int,
  size       bigint,
  trashed_at timestamptz,
  PRIMARY KEY (ns_id, file_id),
  UNIQUE (ns_id, parent_id, name)
);

CREATE TABLE file_versions (
  ns_id      bigint NOT NULL,
  file_id    bigint NOT NULL,
  version    int    NOT NULL,
  chunks     text[] NOT NULL,          -- ordered chunk hashes (the manifest)
  size       bigint NOT NULL,
  device_id  text,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (ns_id, file_id, version)
);

CREATE TABLE journal (                 -- per-namespace change log
  ns_id    bigint NOT NULL,
  seq      bigint NOT NULL,            -- gap-free within the namespace
  file_id  bigint NOT NULL,
  op       text   NOT NULL,            -- create | edit | move | delete
  version  int,
  PRIMARY KEY (ns_id, seq)
);

CREATE TABLE acl (
  resource_id bigint NOT NULL,         -- file or folder
  principal   text   NOT NULL,         -- user:42 | group:eng | link:<token>
  role        text   NOT NULL,
  PRIMARY KEY (resource_id, principal)
);
-- "shared with me" needs the reverse index: principal → resources
```

Chunk index rows: `hash → (extent_id, offset, length, refcount, tier)`. Sync cursors are `(ns_id, seq)` pairs, one per namespace the device can see.

## High-level design

```text
 desktop / mobile / web clients (watcher, chunker, local state DB)
     │  metadata RPCs          │ chunk bytes          ▲ "ns 12 at seq 9041"
     ▼                         ▼                      │
 API gateway             block service ──►      notification gateways
     │                   (verify hash,           (WebSocket / long poll)
     ▼                    dedup check)                ▲
 metadata service ───────────┐   │                    │
  - tree, versions, ACLs     │   ▼                    │
  - commit + journal append  │  object storage   pub/sub (journal
     │                       │  hot → warm → cold  events by ns_id)
     ▼                       │                        ▲
 sharded metadata DB ────────┴── journal events ──────┘
     │
     └──► async: search indexer, thumbnails, quota, audit
```

**Upload:** the client chunks the file, hashes each chunk, and calls `POST /uploads` with the hash list and the version it edited. The service returns only the chunks the server does not have. The client uploads those (in parallel) to the block service, which verifies each hash. `commit` checks `base_version`, writes the version row, moves `head_version`, appends to the namespace journal and updates quota in one transaction. The journal event goes to pub/sub.

**Sync down:** other devices hold a notification connection. They receive "namespace 12 changed at seq 9041", call `GET /changes` with their cursor, get the new manifests, download only the chunks they lack, assemble into a temp file and rename it into place.

## Deep dives

### Chunking and content-defined chunking

Files are split into chunks addressed by their SHA-256. Fixed-size chunks (Dropbox has publicly described 4 MB blocks) are simple and work for files edited in place. But an insertion near the start shifts every later boundary, so every chunk hash changes and the whole file re-uploads.

Content-defined chunking (CDC) cuts where a rolling hash over the last few dozen bytes matches a pattern. Boundaries move with the content, so an insertion changes only the chunk or two around it. Min and max sizes bound the spread; the mask width sets the average. Rabin fingerprints were the classic choice; Gear-hash variants such as FastCDC are much faster.

```python
import hashlib
import random

_rng = random.Random(42)
GEAR = [_rng.getrandbits(64) for _ in range(256)]
M64 = (1 << 64) - 1


def cdc_chunks(data, min_size=2048, avg_bits=12, max_size=32768):
    """Gear rolling hash: cut where the top avg_bits bits of the hash are 0."""
    mask = ((1 << avg_bits) - 1) << (64 - avg_bits)
    chunks, start, h = [], 0, 0
    for i, byte in enumerate(data):
        h = ((h << 1) + GEAR[byte]) & M64
        size = i + 1 - start
        if (size >= min_size and not h & mask) or size >= max_size:
            chunks.append(data[start:i + 1])
            start, h = i + 1, 0
    if start < len(data):
        chunks.append(data[start:])
    return chunks


def fixed_chunks(data, size=4096):
    return [data[i:i + size] for i in range(0, len(data), size)]


class BlockStore:
    """Content-addressed: chunk id = SHA-256 of its bytes."""

    def __init__(self):
        self.blocks, self.bytes_written = {}, 0

    def put(self, chunk):
        cid = hashlib.sha256(chunk).hexdigest()
        if cid not in self.blocks:           # dedup: skip known content
            self.blocks[cid] = chunk
            self.bytes_written += len(chunk)
        return cid

    def read(self, manifest):
        return b"".join(self.blocks[c] for c in manifest)


def reuse(old, new):
    ids = {hashlib.sha256(c).digest() for c in old}
    return sum(hashlib.sha256(c).digest() in ids for c in new) / len(new)


if __name__ == "__main__":
    v1 = random.Random(7).randbytes(1 << 20)                 # 1 MiB file
    v2 = b"header!" + v1[:600_000] + b"edit" + v1[600_004:]  # insert + patch

    cdc1, cdc2 = cdc_chunks(v1), cdc_chunks(v2)
    assert b"".join(cdc2) == v2
    assert all(2048 <= len(c) <= 32768 for c in cdc1[:-1])
    assert reuse(cdc1, cdc2) > 0.95                          # few chunks change
    assert reuse(fixed_chunks(v1), fixed_chunks(v2)) < 0.05  # every boundary shifted

    store = BlockStore()
    m1 = [store.put(c) for c in cdc1]
    after_v1 = store.bytes_written
    m2 = [store.put(c) for c in cdc2]
    assert store.read(m1) == v1 and store.read(m2) == v2
    assert store.bytes_written - after_v1 < 0.05 * len(v2)   # upload only new chunks
    print("cdc ok")
```

Production systems use megabyte-scale averages (smaller chunks dedup better but multiply index entries and per-request overhead) and often keep fixed-size chunking for formats that are already compressed and rewritten wholesale on save, where CDC buys little.

### Deduplication

Content addressing gives dedup for free: if the hash exists, skip the upload. The question is scope.

| Scope | Savings | Risk |
|---|---|---|
| Per file (across versions) | Versions cost only changed chunks | None |
| Per namespace or user | Copies and re-uploads | Low |
| Global, client-side | Highest (popular files stored once) | Leaks existence: "your upload was instant" confirms someone has that file |
| Global, server-side only | High | Client still uploads bytes; no leak |

Common practice is client-side dedup within a user or namespace plus server-side dedup globally. Never trust a client-supplied hash: the block service recomputes it on receipt, or a malicious client could register garbage under a popular hash and poison everyone who dedups against it. Deletion needs reference counts per chunk, but counts drift after crashes, so run a periodic mark-and-sweep that recomputes live chunks from all manifests and only deletes after a grace period.

### Resumable uploads

- An upload session (`upload_id`) records the expected chunk list, the base version and an expiry (for example 7 days).
- Chunks are independent and idempotent by hash: retrying one is always safe, and parallel uploads are natural.
- Resume is `GET /uploads/{id}`, which returns what has arrived; the client sends the rest.
- Large chunks can use the object store's multipart upload (S3 parts are at least 5 MiB except the last, up to 10,000 parts). Google's Drive API and the open tus protocol expose similar resumable sessions with byte offsets.
- **Commit is the only step that makes data visible.** Abandoned sessions leave orphan chunks that GC reclaims after expiry.
- Signed URLs let clients send bytes straight to the block tier, keeping the metadata service off the data path.

### Metadata service

The metadata service owns the tree and is the only writer of versions and journals. Inside a namespace, a move is one transaction: update `parent_id`, check the unique `(parent_id, name)`, and reject cycles by walking the destination's ancestors. Moves across namespaces (into a shared folder) are cross-shard: implement them as copy-manifests-then-delete with an idempotent operation id, since manifests reference immutable chunks and no bytes move.

Every commit appends to the namespace journal with a gap-free `seq`. The journal is the sync contract: a device's cursor per namespace says exactly what it has seen. Dropbox has publicly described the same namespace-plus-journal structure. Hot folders and listings are cached, with the journal seq used as a cache version. See [Partitioning and sharding]({{ '/backend/partitioning-and-sharding/' | relative_url }}).

### Sync protocol and conflict resolution

The desktop client keeps a local database with three views of each file: what is on local disk, what the server says, and the last state both agreed on (the synced base). Dropbox's rewritten sync engine was publicly described with the same three-tree model. Comparing local and remote against the base tells the client who changed what:

| Local vs base | Remote vs base | Action |
|---|---|---|
| Changed | Same | Upload, commit with `base_version` |
| Same | Changed | Download and apply |
| Changed | Changed | Conflict |
| Deleted | Changed | Remote edit wins; restore the file |
| Changed | Deleted | Re-create from local copy |

Conflicts are detected by the server's compare-and-set: `commit` succeeds only if `head_version == base_version`; otherwise it returns 409 with the current version. For binary files the safe resolution is to keep both: the loser is saved as "q3-plan (conflicted copy from laptop, 2026-11-16).pptx". Never auto-merge bytes you cannot interpret.

Other details that matter: suppress echoes (ignore journal entries for your own device's commits), apply downloads atomically (write to a temp file, fsync, rename), handle case-insensitive file systems (two server names that differ only in case), and debounce file-watcher events so a save that writes three times uploads once.

### Versioning

Every commit creates an immutable version whose manifest is an ordered list of chunk hashes. `head_version` points at the current one. Restore creates a new version whose manifest equals the old one: no bytes are copied. Because versions share chunks, history costs only the changed chunks. Retention (30 days or 100 versions) runs as a background job that deletes version rows and decrements chunk references. Deletes go to trash first (30 days), which is the same mechanism with a flag.

### Sharing and permissions

- ACL entries attach to files or folders and inherit downward. A check walks the item's ancestors (cached per request), merging grants; the strongest role wins.
- Shared folders become their own namespace, so all members sync the same journal and the folder's metadata stays on one shard.
- Link sharing stores a random token as a principal (`link:<token>`), optionally restricted to a domain or requiring sign-in.
- "Shared with me" needs a reverse index keyed by principal, maintained asynchronously from ACL changes.
- Groups are expanded at check time with caching, since group membership changes independently.
- Download URLs are signed and short-lived, minted only after an ACL check, so revocation takes effect within minutes.

Google has publicly described Zanzibar, a global authorization system storing relation tuples (`doc:readme#viewer@user:42`) used across its products. It is the reference design when ACL checks must be consistent and fast at planet scale. See [RBAC and ABAC]({{ '/backend/rbac-and-abac/' | relative_url }}).

### Notifications to clients

Clients hold a long-lived connection (WebSocket or long poll) to a notification gateway and subscribe to the namespaces they can see. When a commit appends to a journal, an event `(ns_id, seq)` goes to pub/sub; gateways with subscribers for that namespace forward it.

The notification carries no file data, only "namespace 12 is at seq 9041". The client pulls `/changes`. This makes notifications safe to lose, duplicate or coalesce: a client that missed ten pings still catches up from its cursor on the next one, and a client that reconnects simply pulls. Mobile apps use platform push (APNs, FCM) or sync on open instead of holding sockets. A shared folder with 10,000 members costs one event per gateway that has subscribers, not 10,000 messages from the metadata service. Dropbox's public API exposes this pattern as a long-poll endpoint that returns only "changes available". See [Real-time communication]({{ '/backend/real-time-communication/' | relative_url }}).

### Object storage and cold tiers

- Small chunks are packed into large immutable extents (around 1 GB) with an index of `(extent, offset, length)`, because billions of tiny objects are expensive in per-object overhead and metadata. Dropbox's in-house Magic Pocket block store is a publicly described example at exabyte scale.
- Durability comes from erasure coding across racks and zones (roughly 1.4–1.5× overhead) instead of 3× replication.
- Tiering by access: recent and frequently read data on standard storage; data untouched for months on infrequent-access tiers; old versions and trash on archive tiers. Archive tiers with minutes-to-hours retrieval (S3 Glacier Flexible Retrieval or Deep Archive) suit only data users rarely ask for; anything a user might open needs millisecond access (standard, infrequent access, or Glacier Instant Retrieval-class).
- Watch the economics: archive tiers charge retrieval fees and minimum storage durations, so moving data that is read back soon costs more than leaving it.
- Encrypt chunks at rest with envelope encryption (per-extent data keys wrapped by a KMS key). Global dedup across users rules out per-user keys for shared chunks; that trade-off is explicit. See [Encryption]({{ '/backend/encryption/' | relative_url }}).
- Popular public links are served through a CDN.

## Scaling and bottlenecks

| Load | What breaks first | Fix |
|---|---|---|
| 10× | Metadata shards with huge namespaces (a company-wide shared drive); notification fan-out | Split hot namespaces into sub-namespaces; dedicated shards for giant tenants; gateway-level subscriptions |
| 10× | Chunk index lookups on upload | Bloom filter in front of the index; batch lookups per upload |
| 100× | Ingress bandwidth and per-region capacity | Regional block ingest with async cross-region replication; metadata home region per namespace |
| 100× | GC and refcount drift | Incremental mark-and-sweep partitioned by hash prefix |

## Reliability and failure modes

| Failure | Effect | Mitigation |
|---|---|---|
| Upload interrupted | Partial chunks | Resumable session; commit only when complete |
| Commit races with another device | Lost update | `base_version` compare-and-set; conflicted copy |
| Notification lost | Device stale | Pull by cursor on reconnect and periodically |
| Chunk corrupted on disk | Bad reads | Verify hash on read; repair from erasure-coded peers; background scrubbing |
| Bug deletes manifests | Data loss risk | Soft delete, GC grace period, metadata backups and point-in-time recovery |
| Client bug mass-deletes files | Deletions sync everywhere | Trash, version history, server-side anomaly detection on delete bursts |
| Metadata shard failover | Brief write unavailability for its namespaces | Synchronous replicas, automatic failover |

## Trade-offs

| Decision | Chosen? | Why |
|---|---|---|
| Separate metadata and block planes | Yes | Different consistency, size and scaling needs |
| Content-defined chunking | Yes for sync clients | Edits upload only nearby chunks |
| Global client-side dedup | No | Existence leak; server-side global dedup instead |
| Shard metadata by namespace | Yes | Tree operations stay single-shard |
| Push file data in notifications | No | Ping then pull is idempotent and survives loss |
| Auto-merge conflicting edits | No | Keep both copies for opaque binary files |
| Erasure coding over replication | Yes | Same durability at about half the raw storage |

## Interview follow-ups

**Why not store files whole in object storage?** Every edit would re-upload and re-store the whole file, versions would cost full copies, and resumable upload and dedup would be harder. Chunks give delta sync, dedup and cheap versions.

**How does a client know what changed?** It keeps a cursor per namespace and calls `/changes?cursor=`; the journal returns entries since that seq. Notifications only tell it when to ask.

**Two devices edit the same file offline. What happens?** The first commit wins. The second gets 409 on `base_version` and saves a conflicted copy so neither edit is lost.

**How do you delete a chunk safely when it is deduplicated?** Reference counts for the fast path, a periodic mark-and-sweep from all live manifests as the source of truth, and a grace period before physical deletion.

**How do you make a 50 GB upload reliable on a bad connection?** A resumable session, independent hash-addressed chunks uploaded in parallel and retried individually, and a final commit that makes the file visible atomically.

**How do permissions propagate to a folder with a million files?** They are not copied: ACLs live on the folder and are inherited at check time, so a grant or revocation is one row.

**How do you keep hot files fast and old data cheap?** Track access per extent, move cold extents to cheaper tiers, keep current versions on millisecond-access tiers, and push old versions and trash to archive.
