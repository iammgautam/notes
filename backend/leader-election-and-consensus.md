---
title: Leader election and consensus
section: backend
order: 37
phase: "Phase 5 · Distributed systems"
summary: Bully and ring election, lease-based election on etcd and ZooKeeper, fencing tokens, Raft terms, voting and log replication, and Paxos in outline.
---

Many backend problems become simple if exactly one node is in charge: one process runs the billing cron, one broker owns a partition, one database node accepts writes for a shard. Electing that node is easy when nothing fails. The hard part is that a "dead" leader may only be slow, and two nodes that both believe they lead will happily corrupt data. Safe leader election is a consensus problem, which is why production systems delegate it to Raft, Paxos or Zab rather than inventing a protocol.

## Leader election

An election protocol needs **safety** (at most one leader per term or epoch) and **liveness** (eventually some leader exists). Note the wording: not "at most one leader at any instant". In an asynchronous network a deposed leader cannot know it was deposed until it hears about it, so for a short window two nodes may each believe they lead. Correct systems make that harmless with terms and fencing.

### Bully algorithm

Garcia-Molina's bully algorithm makes the highest live ID the leader. A node that notices the leader is gone sends `ELECTION` to every node with a higher ID. If none answers within a timeout, it declares itself leader and broadcasts `COORDINATOR`. If a higher node answers, that node takes over the election. Worst case `O(n^2)` messages, when the lowest ID starts.

### Ring algorithm

In the Chang-Roberts ring algorithm nodes form a logical ring. A node starts an election by sending its ID clockwise; each node forwards the larger of the incoming ID and its own. When a node receives its own ID back, it has the maximum and announces itself with an `ELECTED` message around the ring. That costs `O(n log n)` messages on average and `O(n^2)` in the worst case, and the ring must be repaired when a member dies.

| | Bully | Ring | Lease on a consensus store |
|---|---|---|---|
| Winner | Highest live ID | Highest ID on the ring | Whoever creates the key first |
| Messages | `O(n^2)` worst | `O(n log n)` average | A few RPCs to the store |
| Assumes | Timeouts mean crashes | Timeouts mean crashes, intact ring | Store is a majority quorum |
| Under partition | Two leaders | Two leaders | At most one lease holder per epoch |
| Used in practice | Teaching, small closed systems | Teaching, token rings | etcd, ZooKeeper, Consul, Kubernetes |

Both classic algorithms assume failure detection is accurate. On a real network a timeout cannot tell "crashed" from "slow" or "partitioned", so both happily elect a second leader on each side of a partition. They are worth knowing for interviews and as a contrast, not for production.

### Lease-based election with etcd and ZooKeeper

The production pattern: let a consensus-backed store decide, and make leadership a lease that must be renewed.

- **etcd.** Grant a lease with a TTL, then in a transaction create the leader key bound to that lease only if it does not exist. The holder keeps the lease alive; if it dies or is partitioned, the lease expires, etcd deletes the key, and watchers campaign. etcd's election API (`etcdctl elect`, the Go `concurrency` package) wraps this, and the key's creation revision serves as a fencing token.
- **ZooKeeper.** Each candidate creates an ephemeral sequential znode under `/election`. The lowest sequence number is the leader. Every other candidate watches only its immediate predecessor, so a leader crash wakes one node instead of all of them (avoiding the herd effect). Ephemeral nodes vanish when the client's session times out.
- **Kubernetes.** Controllers use `Lease` objects in `coordination.k8s.io` through client-go's leader election helper, with defaults for kube-controller-manager of a 15 s lease, 10 s renew deadline and 2 s retry period. The API server sits on etcd, so this is lease-on-consensus too.
- **Postgres.** If the app already depends on one Postgres primary, a session advisory lock or a lease row is a perfectly good election for a singleton job. The database is the single point of truth (and of failure).

Lease timing is the main tuning knob: short leases fail over fast but cause false failovers on GC pauses and network blips; long leases mean longer outages when a leader really dies.

### Fencing tokens

A lease does not stop an old leader from acting. Picture a leader that stops for a 30-second GC pause. Its 15-second lease expires, a new leader is elected and starts writing. The old one wakes up, still convinced it leads, and writes too. Checking "am I still leader?" before each write does not help, because the pause can happen between the check and the write.

The fix is a fencing token: every election produces a strictly increasing number (Raft term, etcd revision, ZooKeeper zxid, a database epoch column). The leader attaches it to every write, and the resource rejects any token lower than the highest it has seen. The stale leader's writes bounce. This requires the resource to check tokens; [distributed locking]({{ '/backend/distributed-locking/' | relative_url }}) has a runnable demo.

## Consensus

Consensus is getting a group of nodes to agree on a value, and in practice on a sequence of values: a replicated log. Every node applies the same log in the same order to a deterministic state machine, so all replicas end in the same state (state machine replication). This is how systems get the linearizable behavior described in [CAP, consistency and PACELC]({{ '/backend/cap-and-consistency-models/' | relative_url }}). The required properties are agreement (no two nodes decide differently), validity (the decided value was proposed by someone) and termination (non-faulty nodes eventually decide).

Key facts:

- **Majorities.** With `2f + 1` nodes you tolerate `f` crashed nodes: 3 nodes survive 1 failure, 5 survive 2. Any two majorities overlap, which is what carries decisions across leader changes.
- **FLP impossibility** (Fischer, Lynch, Paterson, 1985): in a fully asynchronous system no deterministic algorithm guarantees termination if even one node may crash. Practical algorithms are always safe and make progress when the network is timely enough, using randomized timeouts.
- **Crash faults only.** Raft, Paxos and Zab assume nodes fail by stopping, not by lying. Byzantine fault tolerance (PBFT and blockchain protocols) needs `3f + 1` nodes and is a different world.
- **Writes need a quorum round trip.** That is why consensus stores hold metadata (leaders, config, locks, schema), not your main dataset, unless the database shards data into many consensus groups (CockroachDB, TiKV, Spanner).

## Raft

Raft (Ongaro and Ousterhout, 2014) was designed to be understandable and is the most common consensus algorithm in new systems: etcd, Consul, CockroachDB, TiKV and Kafka's KRaft controller all use it.

### Terms

Time is divided into numbered terms. Each term begins with an election and has at most one leader. Terms act as a logical clock: every RPC carries the sender's term; a node that sees a higher term immediately adopts it and becomes a follower; requests with a stale term are rejected. A deposed leader learns it is deposed the first time it talks to anyone newer.

### Elections in Raft

```text
             timeout:                    majority
             term + 1, vote self         of votes
 FOLLOWER ---------------------> CANDIDATE ----------> LEADER
    ^  ^                          |    ^  |               |
    |  |  sees a leader or        |    |  | timeout:      |
    |  +----- a higher term ------+    +--+ split vote,   |
    |                                       new term      |
    +------------------- sees a higher term --------------+
```

- Followers expect heartbeats (empty `AppendEntries`) from the leader. If none arrives within a randomized election timeout (150–300 ms in the paper), the follower becomes a candidate, increments the term, votes for itself and sends `RequestVote` to everyone.
- A node grants at most one vote per term, and only if the candidate's log is at least as up to date as its own: higher last-entry term wins, and on a tie the longer log wins. This rule guarantees the new leader already has every committed entry.
- A candidate with votes from a majority becomes leader and starts sending heartbeats. If the vote splits, timeouts fire again; randomization makes repeated splits unlikely.

### Log replication

1. A client sends a command to the leader, which appends it to its log.
2. The leader sends `AppendEntries(term, prevLogIndex, prevLogTerm, entries, leaderCommit)` to each follower.
3. A follower accepts only if its log has an entry at `prevLogIndex` with `prevLogTerm`. If not, the leader backs up and retries, and the follower's conflicting suffix is overwritten. This keeps the log matching property: two logs that agree on an entry's index and term agree on everything before it.
4. Once an entry from the current term is stored on a majority, it is committed; the leader applies it and replies to the client. Followers learn the commit index from later messages. Entries from earlier terms are committed only indirectly, by committing a current-term entry after them; skipping this rule allows a committed-looking entry to be lost.

### Safety and reads

The voting rule plus the commit rule give leader completeness: every committed entry is present in the log of every future leader. A subtle consequence concerns reads. A leader that was partitioned away may still answer reads with stale data, so linearizable reads either confirm leadership with a heartbeat round to a majority first (ReadIndex) or rely on a time-based leader lease, which assumes bounded clock drift. etcd serves linearizable reads by default and offers cheaper serializable reads that may be stale. Membership changes are done one server at a time or with joint consensus, so two disjoint majorities can never exist during the change.

A runnable toy election: synchronous RPCs in 10 ms ticks, randomized timeouts, partitions, and the up-to-date log check. It checks election safety on every win:

```python
import random

FOLLOWER, CANDIDATE, LEADER = "follower", "candidate", "leader"


class Node:
    def __init__(self, nid, rng):
        self.id, self.rng = nid, rng
        self.term, self.voted_for, self.state = 0, None, FOLLOWER
        self.last_log = (0, 0)  # (term, index) of the last log entry
        self.deadline = 0

    def reset_timer(self, now):
        self.deadline = now + self.rng.randint(150, 300)  # randomized, ms


class Cluster:
    """Toy Raft leader election: synchronous RPCs, 10 ms ticks, partitions."""

    def __init__(self, n=5, seed=1):
        self.rng = random.Random(seed)
        self.nodes = [Node(i, self.rng) for i in range(n)]
        self.now, self.cut = 0, set()  # cut: node ids on the far side
        self.winners = {}              # term -> leader id, for the safety check
        for node in self.nodes:
            node.reset_timer(0)

    def reachable(self, a, b):
        return (a.id in self.cut) == (b.id in self.cut)

    @staticmethod
    def observe(node, term):
        """Any message with a higher term demotes the receiver to follower."""
        if term > node.term:
            node.term, node.voted_for, node.state = term, None, FOLLOWER

    def request_vote(self, cand, voter):
        self.observe(voter, cand.term)
        self.observe(cand, voter.term)
        ok = (cand.term == voter.term
              and voter.voted_for in (None, cand.id)   # one vote per term
              and cand.last_log >= voter.last_log)     # log at least as new
        if ok:
            voter.voted_for = cand.id
            voter.reset_timer(self.now)
        return ok

    def start_election(self, node):
        node.term += 1
        node.state, node.voted_for = CANDIDATE, node.id
        node.reset_timer(self.now)
        votes = 1 + sum(self.request_vote(node, p) for p in self.nodes
                        if p is not node and self.reachable(node, p))
        if node.state == CANDIDATE and votes > len(self.nodes) // 2:
            node.state = LEADER
            assert self.winners.setdefault(node.term, node.id) == node.id

    def heartbeat(self, leader):
        """Empty AppendEntries: asserts leadership and resets timers."""
        for p in self.nodes:
            if p is leader or not self.reachable(leader, p):
                continue
            self.observe(leader, p.term)
            if leader.state != LEADER:
                return  # it learned of a newer term and stepped down
            self.observe(p, leader.term)
            p.state = FOLLOWER
            p.reset_timer(self.now)

    def can_commit(self, leader):
        acks = sum(self.reachable(leader, p) for p in self.nodes)
        return leader.state == LEADER and acks > len(self.nodes) // 2

    def run(self, ms):
        for _ in range(ms // 10):
            self.now += 10
            for node in self.nodes:
                if node.state == LEADER:
                    if self.now % 50 == 0:
                        self.heartbeat(node)
                elif self.now >= node.deadline:
                    self.start_election(node)

    def leaders(self):
        return [n for n in self.nodes if n.state == LEADER]


if __name__ == "__main__":
    # 1. A leader emerges quickly, and never two in the same term.
    for seed in range(200):
        c = Cluster(seed=seed)
        c.run(1000)
        assert len(c.leaders()) == 1, seed

    # 2. Partition the leader with one follower: the majority side elects a
    #    new leader in a higher term; the old one cannot commit anything.
    c = Cluster(seed=7)
    c.run(1000)
    old = c.leaders()[0]
    buddy = next(n for n in c.nodes if n is not old)
    c.cut = {old.id, buddy.id}
    c.run(1000)
    new = next(n for n in c.leaders() if n is not old)
    print(f"old leader n{old.id} term {old.term}, new leader n{new.id} term {new.term}")
    assert new.term > old.term
    assert c.can_commit(new) and not c.can_commit(old)

    # 3. Heal: the stale leader sees the higher term and steps down.
    c.cut = set()
    c.run(500)
    assert c.leaders() == [new] and old.state == FOLLOWER

    # 4. A node with a stale log can campaign but never win.
    c = Cluster(seed=3)
    for n in c.nodes:
        n.last_log = (2, 10)
    c.nodes[0].last_log = (1, 12)  # longer, but from an older term
    c.nodes[0].deadline = 0        # it times out first
    c.run(2000)
    assert c.leaders() and c.leaders()[0].id != 0
    assert 0 not in c.winners.values()
    print("ok")
```

Scenario 2 shows the window that fencing exists for: during the partition two nodes believe they are leader, in different terms. Only the one with a majority can commit.

## Paxos at a high level

Lamport's Paxos solves single-value consensus with three roles: proposers, acceptors (the voting majority) and learners.

1. **Prepare.** A proposer picks a ballot number `n` higher than any it has used and sends `Prepare(n)` to the acceptors. Each acceptor promises to ignore ballots below `n` and reports the highest-numbered proposal it has already accepted, if any.
2. **Accept.** With promises from a majority, the proposer sends `Accept(n, v)`, where `v` must be the value of the highest-numbered proposal reported (or its own value if none was reported). Acceptors accept unless they have promised a higher ballot. Once a majority accepts the same ballot, `v` is chosen.

The "adopt the reported value" rule is what makes Paxos safe: once a value may have been chosen, every later ballot carries it forward. Competing proposers can preempt each other forever, so practical systems elect a distinguished proposer. **Multi-Paxos** runs phase 1 once for a stable leader, then each log slot costs a single accept round, the same steady-state cost as Raft. Raft is best understood as a Multi-Paxos variant with stricter rules (logs flow only from leader to followers; only up-to-date nodes can lead) that make it easier to implement correctly.

| | Raft | Multi-Paxos | Zab |
|---|---|---|---|
| Leader | Required, elected by terms | Optimization over basic Paxos | Required, by epochs |
| Log holes | Not allowed | Allowed, filled later | Not allowed |
| Spec clarity | One paper, complete | Many variants, gaps left to implementers | Tied to ZooKeeper |
| Users | etcd, Consul, CockroachDB, KRaft | Chubby, Spanner | ZooKeeper |

## In practice

etcd election from the shell (illustrative):

```bash
# Blocks until this process becomes leader, then holds leadership until killed.
etcdctl elect billing-cron worker-a

# In another terminal: print the current leader and follow changes.
etcdctl elect --listen billing-cron
```

ZooKeeper election from Python with kazoo (illustrative):

```python
from kazoo.client import KazooClient

zk = KazooClient(hosts="zk-1:2181,zk-2:2181,zk-3:2181")
zk.start()
election = zk.Election("/elections/billing-cron", identifier="worker-a")
election.run(run_billing_loop)  # blocks until elected, then calls the function
```

A lease row in Postgres for a singleton Django job, with an epoch as the fencing token (illustrative). Expiry uses the database clock, so app servers with skewed clocks cannot disagree about it:

```sql
CREATE TABLE leader_lease (
    name       text PRIMARY KEY,
    holder     text NOT NULL,
    epoch      bigint NOT NULL,
    expires_at timestamptz NOT NULL
);

-- Acquire or renew. A returned row means "you lead, with this epoch".
INSERT INTO leader_lease AS l (name, holder, epoch, expires_at)
VALUES ('billing-cron', 'worker-a', 1, now() + interval '15 seconds')
ON CONFLICT (name) DO UPDATE
SET holder = EXCLUDED.holder,
    epoch = CASE WHEN l.holder = EXCLUDED.holder THEN l.epoch
                 ELSE l.epoch + 1 END,
    expires_at = EXCLUDED.expires_at
WHERE l.holder = EXCLUDED.holder OR l.expires_at < now()
RETURNING epoch;
```

Renew every 5 seconds; if a renewal returns no row, stop work immediately. Pass `epoch` into every write the job makes so a stale holder's writes are rejected.

## Pitfalls

- **Split brain from timeouts alone.** Heartbeat-based election without a quorum elects one leader per side of a partition. Use a majority-based store.
- **No fencing.** Leases plus GC pauses equal two active leaders. Carry the epoch or term into every side effect and enforce it at the resource.
- **Even-sized clusters.** Four nodes tolerate one failure, the same as three, with a bigger quorum. Use 3 or 5; 7 at most for write-heavy metadata.
- **Consensus across distant regions.** Every write waits for a majority round trip. Place a majority close together or accept the latency.
- **Election timeout too close to heartbeat interval or disk latency.** Slow fsyncs or a busy node trigger needless elections and write stalls. Watch leader-change metrics; etcd recommends fast disks for this reason.
- **Reading from a follower and assuming freshness.** Only leader reads with ReadIndex or leases are linearizable.
- **Running consensus yourself.** Use etcd, ZooKeeper or Consul; implementing Raft correctly, including snapshots and membership changes, is a multi-month project.

## Interview questions

**Why not just pick the node with the highest ID?** Because nodes cannot tell a crashed leader from a slow or partitioned one. Bully and ring algorithms elect a second leader during a partition. Majority-based consensus guarantees at most one leader per term.

**What is a fencing token and why do you need it?** A strictly increasing number issued with each leadership grant and checked by the resource. Leases expire while a paused leader still believes it leads; the token lets the resource reject the stale leader's writes.

**How does Raft prevent two leaders in the same term?** Each node votes at most once per term and a candidate needs a majority; two majorities overlap, so two candidates cannot both win the same term.

**Why does Raft check that a candidate's log is up to date?** So that only a node holding every committed entry can become leader. Committed entries are on a majority, and a candidate needs votes from a majority, so at least one voter would refuse a candidate missing them.

**When is a Raft entry committed?** When the leader has stored an entry from its current term on a majority. Earlier-term entries become committed only as a side effect of committing a current-term entry after them.

**Paxos vs Raft?** Same fault model and quorum math. Multi-Paxos with a stable leader has the same steady-state cost. Raft constrains the design (strong leader, no log holes, explicit terms) so it is easier to implement and reason about.

**How many nodes for a consensus cluster?** `2f + 1` to survive `f` failures: 3 for one, 5 for two. More nodes add fault tolerance but slow writes.

## Further reading

- *In Search of an Understandable Consensus Algorithm*, Diego Ongaro and John Ousterhout (USENIX ATC 2014).
- *Paxos Made Simple*, Leslie Lamport (2001).
- *Impossibility of Distributed Consensus with One Faulty Process*, Fischer, Lynch and Paterson (JACM, 1985).
- *ZooKeeper: Wait-free Coordination for Internet-scale Systems*, Hunt, Konar, Junqueira and Reed (USENIX ATC 2010).
- *The Chubby Lock Service for Loosely-Coupled Distributed Systems*, Mike Burrows (OSDI 2006).
