---
title: Distributed locking
section: backend
order: 38
phase: "Phase 5 · Distributed systems"
summary: Redis SET NX PX, Redlock and its critique, ZooKeeper and etcd locks, Postgres advisory locks, and why lease expiry plus GC pauses demand fencing tokens.
---

Twenty Celery workers, and only one of them may rebuild tenant 42's search index at a time. Two API pods receive the same webhook, and only one may charge the invoice. A `threading.Lock` works inside one process; across processes and machines you need a lock that lives somewhere shared and survives the holder crashing. That turns out to be a lease with a timeout, and timeouts are where the bugs live.

## Distributed locking

A distributed lock grants one client at a time the right to act on a named resource. Because a holder can crash without releasing, every practical lock is a **lease**: it expires after a TTL unless renewed. That gives liveness (a dead holder cannot block everyone forever) at the cost of safety (a live but paused holder can lose the lock without knowing).

Martin Kleppmann's distinction is the most useful way to choose:

| | Efficiency lock | Correctness lock |
|---|---|---|
| Purpose | Avoid duplicate work | Prevent corruption or double effects |
| If two clients hold it | Some wasted compute, a duplicate email | Lost updates, double charges, broken invariants |
| What is enough | Single Redis `SET NX PX` | Consensus-backed lock **plus** fencing tokens, or no lock at all |

> **Rule of thumb:** for correctness, first try not to need a lock. A unique constraint, `SELECT ... FOR UPDATE`, a conditional `UPDATE ... WHERE version = n`, or an [idempotency]({{ '/backend/idempotency/' | relative_url }}) key in the same database as the data is simpler and safer than any lock service.

## Redis SET NX PX

The standard single-instance Redis lock is one atomic command:

```text
SET lock:invoice:42 3f9c1e7a... NX PX 30000
```

`NX` sets the key only if it does not exist; `PX 30000` expires it after 30 seconds; the value is a random token unique to this holder. Releasing must check the token, because by the time you release, your lease may have expired and someone else may hold the lock. A plain `DEL` would delete their lock. The check and delete must be atomic, so it is a Lua script; extending the lease (`PEXPIRE`) uses the same compare-first pattern.

What can still go wrong:

- **Failover loses locks.** Redis replication is asynchronous. If the primary grants a lock and crashes before the key reaches the replica, the promoted replica has no lock, and a second client acquires it. `WAIT` narrows the window but does not close it.
- **TTL is a guess.** Too short and the lease expires while the holder is still working; too long and a crashed holder blocks others for the full TTL. Renewing in a background thread (a "watchdog") helps until that thread is paused too.
- **No fencing token.** The random value identifies the holder but is not ordered, so the protected resource cannot tell an old holder from a new one.

For efficiency locks, a single Redis with these caveats is fine and cheap.

## Redlock and its critique

Redlock is the Redis author's algorithm for a lock that survives a node failure without replication. With `N` independent Redis primaries (typically five):

1. Record the start time, then try `SET key token NX PX ttl` on every instance with a short per-instance timeout.
2. The lock is acquired if a majority (3 of 5) succeeded and the elapsed time is less than the TTL. The remaining validity is `ttl - elapsed - clock_drift_allowance`.
3. Otherwise, release on all instances and retry after a random delay.

Kleppmann's 2016 critique, in short:

- **No fencing.** Like single-instance Redis, Redlock issues no monotonic token, so a client paused past its validity can still write after another client acquires the lock.
- **Timing assumptions.** Safety depends on bounded network delay, bounded process pauses and bounded clock drift. A Redis node whose clock jumps forward (an NTP step, an operator fixing the time) expires keys early, and two clients can each assemble a majority.
- **Wrong tool either way.** For efficiency, one Redis is enough and Redlock adds five nodes of cost. For correctness, Redlock is not enough, because it is not a consensus protocol and gives no fencing.

Salvatore Sanfilippo's reply ("Is Redlock safe?") argued that clock jumps can be prevented operationally and that the random token can serve for check-and-set on resources that support it. The practical takeaway most teams draw: use single-node Redis for efficiency locks, and a consensus system with fencing (or the database itself) for correctness.

## ZooKeeper and etcd locks

Both are consensus-replicated, so a lock grant survives node failures and cannot be split by a failover.

- **ZooKeeper.** Each client creates an ephemeral sequential znode under `/locks/invoice-42`. The client with the lowest sequence number holds the lock. Every other client watches only its immediate predecessor, so a release wakes exactly one waiter (no herd effect). Ephemeral nodes disappear when the client's session expires, which is the lease. The creation zxid of the lock node is monotonic and works as a fencing token; Chubby, Google's lock service, published the same idea as "sequencers". Libraries: Apache Curator's `InterProcessMutex`, kazoo's `Lock` in Python.
- **etcd.** The client creates a key under a lock prefix attached to a lease and waits until its key has the lowest creation revision, watching the key just before it. `etcdctl lock` and the Go `concurrency.Mutex` implement this. The key's revision is the fencing token.

The cost is running a three- or five-node cluster and a quorum round trip per acquire, which is why these locks are for coordination (leaders, schema migrations, singleton jobs), not per-request work. Electing a long-lived owner is the same mechanism; see [leader election and consensus]({{ '/backend/leader-election-and-consensus/' | relative_url }}).

## Postgres advisory locks

If your app already runs on Postgres, advisory locks give you named locks with no new infrastructure.

| Function | Scope | Blocks? |
|---|---|---|
| `pg_advisory_lock(key)` | Session, until unlock or disconnect | Yes |
| `pg_try_advisory_lock(key)` | Session | No, returns `true`/`false` |
| `pg_advisory_xact_lock(key)` | Transaction, released at commit/rollback | Yes |
| `pg_try_advisory_xact_lock(key)` | Transaction | No |
| `pg_advisory_unlock(key)` | Releases one session-level hold | n/a |

Keys are one `bigint` or two `int4` values; hash string names to a 64-bit integer in your app. Properties and traps:

- **Liveness comes free.** If the holder's connection dies, Postgres releases its locks. A stuck-but-connected client keeps them, though, and detecting a dead peer depends on TCP keepalives.
- **Session locks and PgBouncer don't mix.** In transaction pooling mode, consecutive statements may run on different server connections, so a session lock can be taken on one connection and the unlock sent to another. Use the `xact` variants inside a transaction, or a dedicated session-pooled connection.
- **Session locks are reentrant.** Locking the same key twice requires unlocking twice.
- **They are not replicated.** Locks live in the primary's memory. After a failover, every advisory lock is gone while old holders may still be running.
- **Long transactions hurt.** Holding an `xact` lock for a 20-minute job keeps a transaction open, which holds back VACUUM. Use a session lock on a dedicated connection for long work.

When the protected data is in the same database, prefer row locks: `SELECT ... FOR UPDATE` puts the lock and the write in one transaction, so no fencing is needed. See [Postgres locking]({{ '/postgresql/locking/' | relative_url }}).

## Fencing tokens

A fencing token is a number that increases with every lock grant. The holder sends it with every write, and the resource remembers the highest token it has accepted and rejects anything lower. A paused ex-holder's late writes now fail loudly instead of silently corrupting data.

Sources of tokens: etcd revisions, ZooKeeper zxids, a Raft term, or a counter column incremented in the same transaction that grants a lease row. Enforcement must happen at the resource: a `WHERE fence <= :token` condition on an `UPDATE`, a conditional write on an object store, a storage service that checks a header. If the resource cannot check tokens, no lock gives you correctness, and you should redesign around idempotency or single-writer partitioning.

## Lock expiry and GC pauses

The canonical failure, which every lease-based lock is exposed to:

```text
 time ---------------------------------------------------------------->
 worker-1  acquire(tok 1)  read 100  [###### GC pause 15 s ######]  write
 lease     |<------ 10 s ------>| expires
 worker-2                          acquire(tok 2)  read, write 150
 storage   100                                     150          70 (lost)
 fenced    100                                     150   rejects tok 1
```

Worker-1 reads a balance, then stops: a stop-the-world GC pause, a VM live migration, a laptop lid in a dev environment, a page fault under memory pressure, or a long network delay on the write. Its 10-second lease expires; worker-2 acquires the lock and writes. Worker-1 resumes, still believing it holds the lock, and writes a value computed from stale data. Checking `holder == me` just before the write does not help: the pause can land between the check and the write. Clock jumps cause the same outcome without any pause.

A runnable demo. Without fencing the deposit is silently lost; with fencing the stale write is rejected and worker-1 retries correctly. It also shows why release must compare the owner:

```python
class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, seconds):
        self.t += seconds  # a GC pause is just time the process does not see


class LockService:
    """Lease lock with the shape of Redis SET key owner NX PX ttl, plus a
    strictly increasing token per grant (as etcd revisions or ZooKeeper
    zxids provide; plain Redis SET NX gives you no such number)."""

    def __init__(self, clock):
        self.clock, self.locks, self.counter = clock, {}, 0

    def acquire(self, name, owner, ttl):
        held = self.locks.get(name)
        if held and held["expires"] > self.clock.t:
            return None
        self.counter += 1
        self.locks[name] = {"owner": owner, "token": self.counter,
                            "expires": self.clock.t + ttl}
        return self.counter

    def holder(self, name):
        held = self.locks.get(name)
        return held["owner"] if held and held["expires"] > self.clock.t else None

    def release_naive(self, name):
        self.locks.pop(name, None)  # plain DEL: may delete someone else's lock

    def release(self, name, owner):
        if self.holder(name) == owner:  # compare-and-delete (a Lua script)
            del self.locks[name]


class Storage:
    def __init__(self, fenced):
        self.fenced, self.value, self.max_token = fenced, 100, 0

    def write(self, value, token):
        if self.fenced:
            if token < self.max_token:
                raise PermissionError(f"stale token {token} < {self.max_token}")
            self.max_token = token
        self.value = value


def scenario(fenced):
    """worker-1 withdraws 30, worker-2 deposits 50. Correct result: 120."""
    clock = Clock()
    locks, store = LockService(clock), Storage(fenced)

    t1 = locks.acquire("acct:42", "worker-1", ttl=10)
    balance = store.value                          # reads 100
    assert locks.holder("acct:42") == "worker-1"   # "still mine?" says yes
    clock.sleep(15)                                # GC pause longer than TTL

    t2 = locks.acquire("acct:42", "worker-2", ttl=10)
    assert t2 is not None                          # lease expired: granted
    store.write(store.value + 50, t2)              # 150
    locks.release("acct:42", "worker-2")

    try:
        store.write(balance - 30, t1)              # stale read: writes 70
    except PermissionError:
        t3 = locks.acquire("acct:42", "worker-1", ttl=10)  # retry properly
        store.write(store.value - 30, t3)
    return store.value


if __name__ == "__main__":
    print("without fencing:", scenario(fenced=False))
    print("with fencing:   ", scenario(fenced=True))
    assert scenario(fenced=False) == 70    # the deposit was silently lost
    assert scenario(fenced=True) == 120

    # Naive release after a pause deletes the new holder's lock.
    clock = Clock()
    locks = LockService(clock)
    locks.acquire("job", "worker-1", ttl=10)
    clock.sleep(15)
    locks.acquire("job", "worker-2", ttl=10)
    locks.release("job", "worker-1")       # compare-and-delete: no effect
    assert locks.holder("job") == "worker-2"
    locks.release_naive("job")             # DEL: worker-2 loses its lock
    assert locks.acquire("job", "worker-3", ttl=10) is not None
    print("ok")
```

Mitigations, in order of strength: fencing tokens enforced by the resource; doing the protected write inside the lock's own system (a database transaction); TTLs far longer than any plausible pause, plus renewal; and treating a failed renewal as "stop immediately and abandon in-flight work".

## Choosing a lock

| Option | Survives node failure | Fencing token | Released on crash by | Use for |
|---|---|---|---|---|
| Row lock / unique constraint | With the database | Not needed | Transaction end | Data in the same DB (first choice) |
| Postgres advisory lock | Lost on failover | No (add an epoch column) | Disconnect | Singleton jobs in a Postgres app |
| Redis `SET NX PX` | Lost on failover | No | TTL | Efficiency: dedupe work, throttling |
| Redlock | Under timing assumptions | No | TTL | Rarely worth it |
| etcd / ZooKeeper | Yes (consensus) | Yes (revision, zxid) | Lease or session expiry | Correctness-critical coordination |

## In practice

Redis lock with a token-checked release (illustrative, redis-py):

```python
import uuid

import redis

r = redis.Redis(host="redis.internal")

RELEASE = r.register_script("""
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
""")


def acquire(name, ttl_ms=30_000):
    token = uuid.uuid4().hex
    return token if r.set(f"lock:{name}", token, nx=True, px=ttl_ms) else None


def release(name, token):
    return RELEASE(keys=[f"lock:{name}"], args=[token]) == 1


# redis-py also ships the same pattern as a context manager:
with r.lock("lock:invoice:42", timeout=30, blocking_timeout=5):
    send_invoice_email(42)  # efficiency lock: a duplicate email is tolerable
```

A transaction-scoped Postgres advisory lock in Django (illustrative). Safe behind PgBouncer in transaction mode:

```python
import hashlib
from contextlib import contextmanager

from django.db import connection, transaction


def lock_key(name: str) -> int:
    digest = hashlib.sha256(name.encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


@contextmanager
def try_advisory_xact_lock(name):
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(%s)", [lock_key(name)])
            yield cur.fetchone()[0]


with try_advisory_xact_lock(f"reindex:tenant:{tenant_id}") as acquired:
    if acquired:
        rebuild_search_index(tenant_id)
```

A fenced write: the resource enforces the token, so a stale holder's update matches zero rows (illustrative):

```sql
UPDATE tenant_reports
SET body = %(body)s, fence = %(token)s, updated_at = now()
WHERE tenant_id = %(tenant_id)s
  AND fence <= %(token)s;
-- rowcount 0: a newer lock holder already wrote; abandon this run
```

## Pitfalls

- **Releasing with `DEL`.** After an expiry, you delete the next holder's lock and a third client gets in. Always compare-and-delete.
- **TTL shorter than the work.** Symptom: rare duplicate processing that never reproduces locally. Measure p99.9 job duration, set the TTL well above it, and renew.
- **Assuming the lock makes writes safe.** Without fencing at the resource, a paused holder can still write. Enforce tokens or use database transactions.
- **Redis failover.** Locks vanish when a replica is promoted. Fine for efficiency locks; disqualifying for correctness locks.
- **Advisory locks through PgBouncer in transaction mode.** Session locks leak or release on the wrong connection. Use `xact` variants or session pooling.
- **Lock as a queue.** Hundreds of workers spinning on `try_lock` for the same key burn CPU and Redis ops. Use a real queue, or partition work so each key has one natural owner.
- **Forgetting failover for advisory locks.** A Postgres failover silently drops every advisory lock while old holders continue.

## Interview questions

**How do you build a distributed lock with Redis?** `SET key random_token NX PX ttl` to acquire; release with a Lua script that deletes only if the value still equals your token; renew with a similar compare-then-`PEXPIRE` script. It is fine for efficiency, but async replication and the lack of fencing make it unsafe for correctness.

**What is the critique of Redlock?** It relies on bounded pauses, network delays and clock drift for safety, and it issues no fencing token. A paused client or a clock jump lets two clients act at once. It is more machinery than efficiency locks need and still not enough for correctness.

**What is a fencing token and who enforces it?** A monotonically increasing number issued with each lock grant. The protected resource enforces it by rejecting writes carrying a token lower than the highest it has seen. Without resource-side enforcement, the token does nothing.

**Why can't a client just check that it still holds the lock before writing?** The check and the write are separate steps. A GC pause, page fault or network delay can happen between them, by which time the lease has expired and another client holds the lock.

**How does a ZooKeeper lock avoid the herd effect?** Each waiter creates an ephemeral sequential node and watches only the node just before it, so a release notifies one client instead of all of them.

**Session-level or transaction-level advisory lock?** Transaction-level when the work fits in a transaction and especially behind PgBouncer transaction pooling; session-level on a dedicated connection for long jobs, remembering to unlock and that locks are lost on failover.

**When should you not use a distributed lock?** When the database can enforce the rule (unique constraints, row locks, conditional updates), when idempotency keys make duplicates harmless, or when partitioning gives each key a single owner, such as a Kafka partition consumed by one worker.

## Further reading

- *How to do distributed locking*, Martin Kleppmann (2016).
- *Is Redlock safe?*, Salvatore Sanfilippo (2016).
- Redis documentation: *Distributed Locks with Redis*.
- *The Chubby Lock Service for Loosely-Coupled Distributed Systems*, Mike Burrows (OSDI 2006).
- PostgreSQL documentation: *Explicit Locking*, section on advisory locks.
