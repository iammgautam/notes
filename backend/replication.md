---
title: Replication
section: backend
order: 14
phase: "Phase 2 · Databases"
summary: Copy data across nodes safely, covering sync vs async, leader-based and leaderless designs, lag and read-your-writes, failover, split brain and read routing.
---

A single database server is a single point of failure and a ceiling on read throughput. Replication keeps copies of the same data on several machines so you survive a dead disk or a lost availability zone, serve reads from more than one box, and put data closer to users. The hard part is not copying bytes. It is deciding when a write counts as done, what readers see while copies disagree, and who may accept writes when the network splits.

## Replication

Every scheme answers three questions: who accepts writes (one leader, several, or any replica), when the client hears "committed", and what is shipped. PostgreSQL streaming replication ships physical WAL records (exact copy, same major version). Logical replication and MySQL's row-based binlog ship row changes (cross-version, selective, also feeds CDC tools like Debezium). Amazon Aurora is publicly described as shipping redo records to a shared, quorum-replicated storage layer.

### Synchronous, asynchronous and semi-synchronous

- **Asynchronous.** The leader commits locally, acknowledges, and streams the change afterward. Writes stay fast and a slow replica cannot block them, but anything acknowledged and not yet shipped is lost if the leader dies. Your recovery point objective (RPO) equals the current lag.
- **Synchronous.** The leader waits for a replica before acknowledging. PostgreSQL's `synchronous_commit` picks how far the replica must get: `remote_write` (received), `on` (flushed to disk) or `remote_apply` (replayed and visible to queries). Every commit pays a round trip plus the replica's fsync, and if the only synchronous replica is down, commits hang.
- **Semi-synchronous.** MySQL's semi-sync waits until one replica has received the event and written it to its relay log, not applied it. If no replica answers within a timeout (10 s by default) it silently falls back to async, so durability degrades exactly when things go wrong. PostgreSQL's quorum commit, `synchronous_standby_names = 'ANY 1 (a, b)'`, gives "any one of N" without depending on a single replica.

| Mode | Client waits for | Added latency | Loss on leader crash | Replica outage |
|---|---|---|---|---|
| Async | Local flush | None | Up to the lag | No effect |
| Semi-sync | One replica received | One RTT | Near zero unless degraded | Falls back to async |
| Sync, one replica | Replica flush/apply | RTT + fsync | Zero | Writes block |
| Quorum `ANY k of n` | k replicas | RTT to k-th fastest | Zero | Tolerates n − k down |

> **Rule of thumb:** Synchronous to a same-region replica (about a millisecond away) for zero data loss; asynchronous across regions for disaster recovery. Cross-region synchronous commits add tens of milliseconds to every write.

## Single-leader replication

All writes go to one leader; it streams its log to followers, which apply it in the same order. PostgreSQL, MySQL, SQL Server, MongoDB replica sets and Redis work this way, and so does each Kafka partition. Ordering is trivial because one node orders all writes, so there are no write conflicts.

The costs: the leader caps write throughput, every replica replays every write, and the leader is a single point of failure until failover completes. When writes outgrow one node, [partition or shard]({{ '/backend/partitioning-and-sharding/' | relative_url }}) and keep each shard single-leader.

## Multi-leader replication

Several nodes accept writes and replicate to each other asynchronously. Reasons: multi-region writes at local latency that survive an inter-region outage, offline clients (each device is a leader that syncs later, as in CouchDB-style sync), and collaborative editing.

The price is **write conflicts**: two leaders accept different changes to the same row and nothing stops them.

| Strategy | How | Downside |
|---|---|---|
| Avoid conflicts | Each record has a home leader (by tenant or region) | Re-homing is a migration |
| Last write wins | Highest timestamp wins | Silently drops writes; clock skew decides |
| Merge or CRDTs | Types that merge deterministically (counters, sets, text) | Only some data fits |
| App resolution | Keep both versions, resolve on read or by the user | Complexity moves into product code |

Causality also breaks: an update can reach a third leader before the insert it depends on. In interviews the safe answer is "single leader per record, records partitioned across regions", which keeps local latency without general conflict resolution.

## Leaderless replication and quorums

Dynamo-style stores (Cassandra, ScyllaDB, Riak, after Amazon's Dynamo paper) have no leader. A coordinator sends each write to all N replicas of a key and succeeds after W acknowledgments; a read asks R replicas and keeps the newest version. If `R + W > N`, every read set overlaps every successful write set, so reads see the latest successful write. A common setting is N = 3, W = R = 2 (`QUORUM` in Cassandra).

```text
 N = 3, W = 2, R = 2              write v2           read
                                  --------           ----
 +-----------+                    replica A  ack     (down)
 |coordinator| -- send to all --> replica B  ack  -> v2  <- newest wins
 +-----------+                    replica C  down -> v1  -> read repair
                                  W met (2)          R met (2): B, C
```

Replicas converge through **read repair** (write the newest value back to stale replicas seen on a read), **hinted handoff** (another node holds writes for a down replica; with a "sloppy quorum" those count toward W and break the overlap guarantee) and **anti-entropy** (background Merkle-tree comparison).

Quorums are not linearizability. Concurrent writes need a conflict rule (Cassandra uses per-cell last-write-wins timestamps; Riak can return siblings tracked with version vectors), and a write that misses its quorum is not rolled back where it landed. Amazon DynamoDB, despite the name, is publicly described as leader-based per partition.

## Replication lag and read-your-writes

Async replicas trail the leader by milliseconds to minutes, which shows up as three anomalies:

| Anomaly | User sees | Fix |
|---|---|---|
| Read-your-writes violation | Saves a profile, reloads, sees the old one | Read own recent writes from the leader |
| Non-monotonic reads | A comment appears, then vanishes on refresh | Pin each user to one replica (hash of user id) |
| Inconsistent prefix | An answer before its question | Keep causally related writes in one partition |

Ways to get read-your-writes:

1. **By ownership.** Reads of the current user's own data go to the leader.
2. **Time window.** After a user writes, route their reads to the leader for longer than p99 lag.
3. **Log position.** After the write, record the leader's position (`pg_current_wal_lsn()`, or a GTID set in MySQL) and only read from a replica whose `pg_last_wal_replay_lsn()` has passed it. The most precise option.
4. **`synchronous_commit = remote_apply`.** The write is visible on the synchronous replica before commit returns, at a latency cost.

MongoDB packages option 3 as causally consistent sessions. Measure lag on the leader with `pg_stat_replication` (`write_lag`, `flush_lag`, `replay_lag`). On a replica, `now() - pg_last_xact_replay_timestamp()` keeps growing on an idle database even though nothing is missing.

## Failover

Failover replaces a dead leader with a replica in three steps, each with a trap:

1. **Detect.** A missed heartbeat for some timeout. Too short and a GC pause triggers needless failovers; too long and you are down that whole time. Patroni's defaults use a leader lease on the order of 30 seconds.
2. **Elect.** Pick the most up-to-date replica, with agreement from a majority (Patroni uses etcd, Consul or ZooKeeper; MongoDB and Redis Sentinel run their own elections). See [leader election and consensus]({{ '/backend/leader-election-and-consensus/' | relative_url }}).
3. **Reconfigure.** Point writers at the new leader (DNS, virtual IP, proxy or service discovery), repoint the other replicas, and make sure the old leader never accepts writes again.

With async replication, failover loses the unshipped tail. Worse, IDs that caches, search indexes or other services already saw can be reissued by the new leader for different rows; a widely cited GitHub MySQL incident, described in *Designing Data-Intensive Applications*, followed this pattern. Managed failover (RDS Multi-AZ flips DNS to a synchronous standby, typically within a minute or two) still needs clients to re-resolve DNS and pools to drop dead connections.

> **In production:** Rehearse planned switchovers regularly. A failover path that has never run does not work.

## Split brain

Split brain is two nodes both acting as leader and accepting writes, so the data diverges and must be merged by hand. Causes: a partition that isolates the old leader from the failover manager but not from clients; an old leader that freezes (long GC, VM pause), is replaced, then wakes up; failover tooling without a quorum.

| Defense | Mechanism |
|---|---|
| Majority quorum | Leadership needs a majority of an odd-sized group (3 or 5, or 2 nodes plus a witness); the minority side cannot elect |
| Leases | The leader stops writing if it cannot renew its lease; Patroni demotes a primary that cannot update its leader key |
| Fencing tokens | Each term gets a higher epoch; storage rejects requests carrying an older one |
| STONITH | Power off or isolate the old node before promoting |

Leases depend on clocks and pause-free processes, which is why fencing tokens matter, the same idea as in [distributed locking]({{ '/backend/distributed-locking/' | relative_url }}). In PostgreSQL a promotion starts a new timeline, and the old primary must be rewound with `pg_rewind` or rebuilt before rejoining.

## Master-slave architecture

"Master-slave" is the older name for single-leader primary-replica replication: one writable primary and read-only replicas. Most projects now say primary and replica.

### Primary-replica setup

In PostgreSQL: allow replication connections and create a `REPLICATION` role on the primary; clone it with `pg_basebackup`; start the replica in standby mode (`standby.signal` plus `primary_conninfo`) and it streams WAL continuously. A **replication slot** makes the primary keep WAL until the replica consumes it, so a lagging replica never needs a re-clone, but a dead one makes the primary keep WAL forever; cap it with `max_slot_wal_keep_size`. Replicas can feed other replicas (cascading) to spare the primary.

### Read scaling

Replicas multiply read capacity for dashboards, catalogs and feeds. They do not scale writes, since every replica replays every write. Lag grows during write bursts (bulk imports, backfills). Long replica queries conflict with WAL replay: Postgres cancels them after `max_standby_streaming_delay` ("conflict with recovery"), or with `hot_standby_feedback = on` keeps them alive at the cost of primary bloat. Give analytics its own replica with relaxed settings.

### Routing reads

| Layer | How | Examples |
|---|---|---|
| Application | ORM router per query | Django `DATABASE_ROUTERS` |
| Driver | Multi-host URL with a target role | libpq `target_session_attrs=read-write` or `prefer-standby` |
| Proxy | Splits reads/writes, health-checks | ProxySQL, Pgpool-II, HAProxy |
| Managed endpoint | Writer and reader DNS names | Aurora cluster and reader endpoints |

Application routing is the most reliable because only the application knows which reads must be fresh; SQL-parsing proxies misroute functions with side effects.

### Promotion

`pg_ctl promote` or `SELECT pg_promote()` turns a replica into the primary. A planned **switchover** stops writes on the old primary, waits for the target to catch up fully, then promotes, losing nothing. An unplanned **failover** promotes whatever the best replica has. Afterward, repoint the remaining replicas and rewind or rebuild the old primary.

## In practice

### PostgreSQL settings and lag query (illustrative)

```text
# primary
wal_level = replica
max_slot_wal_keep_size = 50GB
synchronous_standby_names = 'ANY 1 (replica_a, replica_b)'
synchronous_commit = on

# replica
hot_standby_feedback = off        # on only for a reporting replica
max_standby_streaming_delay = 30s
```

```sql
SELECT application_name, sync_state, replay_lag,
       pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS replay_bytes
FROM pg_stat_replication;
```

Relax durability per transaction for low-value writes with `SET LOCAL synchronous_commit = local`.

### Django read routing with read-your-writes (illustrative)

Reads go to replicas unless the request is pinned; the middleware pins write requests and any client that wrote in the last few seconds.

```python
import random
import time
from contextvars import ContextVar

_pin_primary = ContextVar("pin_primary", default=False)


class PrimaryReplicaRouter:
    def db_for_read(self, model, **hints):
        if _pin_primary.get():
            return "default"
        return random.choice(["replica_1", "replica_2"])

    def db_for_write(self, model, **hints):
        return "default"

    def allow_relation(self, obj1, obj2, **hints):
        return True

    def allow_migrate(self, db, app_label, **hints):
        return db == "default"


class ReadYourWritesMiddleware:
    WINDOW = 5   # seconds; keep above p99 replication lag

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        writing = request.method not in ("GET", "HEAD", "OPTIONS")
        recent = time.time() - float(request.COOKIES.get("lw", 0)) < self.WINDOW
        token = _pin_primary.set(writing or recent)
        try:
            response = self.get_response(request)
        finally:
            _pin_primary.reset(token)
        if writing:
            response.set_cookie("lw", str(time.time()), max_age=self.WINDOW)
        return response
```

### Quorum reads and writes (runnable)

Quorum overlap, read repair, a stale read when `R + W <= N`, and a failed write that still lands on one replica.

```python
class Replica:
    def __init__(self, name):
        self.name, self.up, self.data = name, True, {}

    def write(self, key, version, value):
        if not self.up:
            raise ConnectionError(self.name)
        if version > self.data.get(key, (0, None))[0]:   # keep the newest
            self.data[key] = (version, value)

    def read(self, key):
        if not self.up:
            raise ConnectionError(self.name)
        return self.data.get(key, (0, None))


class QuorumClient:
    """Dynamo-style: write to all, need W acks; read R, keep newest."""

    def __init__(self, replicas, w, r):
        self.replicas, self.w, self.r = replicas, w, r
        self.version = 0   # stand-in for a timestamp or version vector

    def put(self, key, value):
        self.version += 1
        acks = 0
        for rep in self.replicas:
            try:
                rep.write(key, self.version, value)
                acks += 1
            except ConnectionError:
                pass   # real systems: hinted handoff
        if acks < self.w:
            raise RuntimeError(f"write quorum not met: {acks} of {self.w}")

    def get(self, key):
        replies = []
        for rep in self.replicas:
            if len(replies) == self.r:
                break
            try:
                replies.append((rep, rep.read(key)))
            except ConnectionError:
                pass
        if len(replies) < self.r:
            raise RuntimeError("read quorum not met")
        newest = max((reply for _, reply in replies), key=lambda v: v[0])
        for rep, reply in replies:          # read repair
            if reply[0] < newest[0]:
                rep.write(key, *newest)
        return newest[1]


if __name__ == "__main__":
    a, b, c = Replica("a"), Replica("b"), Replica("c")
    client = QuorumClient([a, b, c], w=2, r=2)      # N=3, R+W=4 > 3

    client.put("cart:42", "v1")
    c.up = False
    client.put("cart:42", "v2")                      # lands on a and b
    c.up, a.up = True, False                         # c is back but stale
    assert client.get("cart:42") == "v2"             # b overlaps the write
    assert c.read("cart:42") == (2, "v2")            # read repair fixed c

    a.up, b.up = True, False
    c.data["cart:42"] = (1, "v1")                    # make c stale again
    weak = QuorumClient([c, a, b], w=1, r=1)         # R+W=2, no overlap
    assert weak.get("cart:42") == "v1"               # stale read

    a.up = False                                     # only c is up
    try:
        client.put("cart:42", "v3")
        raise AssertionError("expected quorum failure")
    except RuntimeError as exc:
        assert "1 of 2" in str(exc)
    assert c.read("cart:42")[1] == "v3"              # partial write stays
    print("quorum demo ok")
```

## Pitfalls

- **Reading your own write from a replica.** "I saved it but it's gone" until a refresh. Use a read-your-writes strategy; an ORM router does not know you just wrote in the same request.
- **Lag spikes from bulk writes.** One 50-million-row `UPDATE` is a WAL burst replicas take minutes to replay. Batch and throttle on measured lag.
- **Abandoned replication slot.** The primary's disk fills with retained WAL. Alert on `pg_replication_slots` and set `max_slot_wal_keep_size`.
- **One synchronous replica.** When it reboots, every commit blocks. Use `ANY 1` of at least two.
- **Stale connections after failover.** Pools and DNS caches keep pointing at the old primary. Use short DNS TTLs, pool pre-ping or max connection lifetime, and `target_session_attrs`.
- **Treating replicas as backups.** `DROP TABLE` replicates in milliseconds. Keep point-in-time recovery or a delayed replica (`recovery_min_apply_delay`); see [disaster recovery and backups]({{ '/backend/disaster-recovery-and-backups/' | relative_url }}).
- **Two-node automatic failover.** Without a third voter, a partition yields either no failover or split brain.

## Interview questions

**Synchronous or asynchronous replication?**
Synchronous gives zero loss on failover but adds write latency and couples availability to the replica. Asynchronous keeps writes fast but loses the lag window. A common answer: quorum-sync to one of two same-region replicas, async to another region.

**How do you guarantee read-your-writes with replicas?**
Send a user's reads of their own recent data to the primary (by ownership or a time window), or record the primary's log position after the write and only use a replica that has replayed past it.

**What does R + W > N guarantee?**
Read and write quorums overlap, so reads see the latest successful write. It is not linearizability: concurrent writes still need conflict resolution, failed writes are not rolled back, and sloppy quorums break the overlap.

**What is split brain and how do you prevent it?**
Two leaders accepting conflicting writes. Prevent it with majority election (odd voter count or a witness), leases that make the old leader step down, fencing tokens, or STONITH.

**Replica lag jumped to two minutes. What do you check?**
Write bursts on the primary, replica I/O and CPU (replay is largely single-threaded), long replica queries delaying replay, and network throughput. `pg_stat_replication` shows whether lag is in sending, flushing or replaying.

**When would you choose multi-leader?**
Multi-region writes at local latency that must survive an inter-region outage, or offline clients. Give each record a home region so conflicts cannot occur.

**Is a read replica a backup?**
No. It replicates mistakes instantly. Backups with point-in-time recovery protect against logical errors; replicas protect against hardware loss.

## Further reading

- *Designing Data-Intensive Applications*, Martin Kleppmann, chapters 5 and 9.
- "Dynamo: Amazon's Highly Available Key-value Store", DeCandia et al., SOSP 2007.
- PostgreSQL documentation, "High Availability, Load Balancing, and Replication".
- MySQL Reference Manual, "Semisynchronous Replication".
- Patroni documentation.
