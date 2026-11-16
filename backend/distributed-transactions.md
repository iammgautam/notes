---
title: Distributed transactions, 2PC and sagas
section: backend
order: 36
phase: "Phase 5 · Distributed systems"
summary: Why atomicity across services is hard, two-phase commit and its blocking problem, sagas with compensations, the outbox pattern and TCC.
---

Checkout touches three services: inventory reserves stock, payments charges the card, shipping books a courier. Each owns its own database. If the charge succeeds and shipping fails, you need the stock back and the money refunded, and you need that to happen even if a process crashes halfway. Inside one Postgres database this is `BEGIN ... COMMIT`. Across services it is one of the hardest problems in backend design, and the usual answer is to avoid needing it.

## Distributed transactions

A distributed transaction updates data owned by more than one node or service with all-or-nothing semantics. It is hard because there is no shared log: each participant can crash, time out or vote differently, and a message saying "commit" can be lost after some participants have applied it.

| Approach | Atomicity | Isolation | Failure behavior | Use when |
|---|---|---|---|---|
| One local transaction | Full | Full | The database's own | The data can live together (try this first) |
| 2PC / XA | Atomic | Locks held until decided | Blocks if the coordinator dies | Few participants, one trust domain, short transactions |
| Saga | Eventual, via compensations | None by default | Stays available; undo is semantic | Long-running, cross-service business flows |
| TCC | Eventual, via reservations | Reservation-level | Stays available | Resources that support holds |
| Outbox + idempotent consumers | Local commit + guaranteed publish | None | Stays available | Propagating a state change reliably |

> **Rule of thumb:** before reaching for any of these, check whether the boundary is wrong. If two services must always change together, they may belong in one service, or one database with a [modular monolith]({{ '/backend/monolith-and-modular-monolith/' | relative_url }}) on top.

## Two-phase commit

### The protocol

A coordinator (the transaction manager) drives participants (resource managers, usually databases) through two phases.

```text
 coordinator               participant A            participant B
     |--- PREPARE ------------->|                         |
     |--- PREPARE ---------------------------------------->|
     |                   log PREPARED,             log PREPARED,
     |                   keep locks                keep locks
     |<-- YES ------------------|                         |
     |<-- YES ---------------------------------------------|
 log COMMIT  <- the commit point
     |--- COMMIT -------------->|                         |
     |--- COMMIT ----------------------------------------->|
     |                   commit, unlock            commit, unlock
     |<-- ACK ------------------|<-- ACK ------------------|
 log END, forget the transaction
```

1. **Prepare.** Each participant does all the work, writes it durably with a `PREPARED` record, keeps its locks, and votes YES. A YES vote is a promise: "I can commit no matter what, even after a crash, and I will not decide on my own."
2. **Decide.** If every vote is YES the coordinator durably logs COMMIT; otherwise ABORT. Writing that record is the commit point.
3. **Complete.** The coordinator sends the decision and retries until every participant acknowledges.

The cost: at least two network round trips and several fsyncs per transaction, with locks held across all of it.

### The blocking problem

A participant that voted YES has given up the right to decide. If the coordinator goes silent after collecting votes, the participant cannot commit (maybe someone voted NO) and cannot abort (maybe the coordinator logged COMMIT and others already committed). It must wait, holding row locks, until the coordinator comes back. Other transactions touching those rows queue behind it.

Asking the other participants only helps if one of them already knows the outcome. If all of them are prepared, all of them are in doubt. This is why 2PC is called a blocking protocol: one machine's failure can stall everyone. Three-phase commit adds a pre-commit round to avoid blocking, but it assumes bounded network delays and is unsafe under real partitions, so it is rarely used.

### Coordinator failure and recovery

- **Durable decision log.** The coordinator writes its decision before phase 2. On restart it reads the log and re-sends any decision it finds.
- **Presumed abort.** If the log has no decision for a transaction, it never reached the commit point, so the answer is ABORT. This lets the coordinator skip logging aborts.
- **Idempotent phase 2.** Participants must accept a duplicate COMMIT or ABORT, since the coordinator retries.
- **Highly available coordinator.** The real fix for blocking is to make the coordinator not a single machine: replicate its log with consensus. Spanner publicly describes running 2PC across shards where each participant and the coordinator is a Paxos-replicated group, so a crashed machine does not block anything. CockroachDB uses a similar idea over Raft.

### Where 2PC is used

XA transactions between databases and message brokers (common in Java EE stacks), Postgres `PREPARE TRANSACTION`, internal cross-shard commits in distributed SQL databases, and Kafka transactions, whose transaction coordinator uses a two-phase protocol to atomically commit writes across partitions together with consumer offsets. Between microservices owned by different teams, 2PC is rarely a good idea: it couples availability, holds locks across network calls, and most HTTP APIs, SaaS providers and queues cannot take part.

A runnable simulation, including both coordinator crash points:

```python
class Crash(Exception):
    """Simulated coordinator crash."""


class Participant:
    def __init__(self, name, vote_yes=True):
        self.name, self.vote_yes = name, vote_yes
        self.state = "INIT"  # INIT -> PREPARED -> COMMITTED | ABORTED
        self.locks_held = False

    def prepare(self):
        if not self.vote_yes:
            self.state = "ABORTED"  # a NO voter may abort on its own
            return False
        # Real systems fsync redo/undo and a PREPARED record here,
        # and keep row locks until the decision arrives.
        self.state, self.locks_held = "PREPARED", True
        return True

    def finish(self, decision):
        assert self.state in ("PREPARED", decision)  # idempotent re-delivery
        self.state, self.locks_held = decision, False

    def on_timeout(self):
        if self.state == "INIT":
            self.state = "ABORTED"  # never voted yes: safe to abort
        # PREPARED: it promised to obey the coordinator. It can neither
        # commit nor abort alone, so it waits with its locks held.


class Coordinator:
    def __init__(self, participants):
        self.participants = participants
        self.log = {}  # durable decision log: txid -> decision

    def run(self, txid, crash=None):
        votes = [p.prepare() for p in self.participants]  # phase 1
        if crash == "before_decision":
            raise Crash(txid)
        decision = "COMMITTED" if all(votes) else "ABORTED"
        self.log[txid] = decision  # commit point: fsync before phase 2
        if crash == "after_decision":
            raise Crash(txid)
        for p in self.participants:  # phase 2
            p.finish(decision)
        return decision

    def recover(self, txid):
        """After restart: re-send a logged decision, else presume abort."""
        decision = self.log.setdefault(txid, "ABORTED")
        for p in self.participants:
            p.finish(decision)
        return decision


def ask_peers(participants):
    """Cooperative termination: learn the outcome from a peer that knows."""
    known = {p.state for p in participants} & {"COMMITTED", "ABORTED"}
    return known.pop() if known else None  # None: everyone is in doubt


if __name__ == "__main__":
    def cluster(*votes):
        ps = [Participant(f"db{i}", v) for i, v in enumerate(votes)]
        return ps, Coordinator(ps)

    # Happy path and a single NO vote.
    ps, c = cluster(True, True, True)
    assert c.run("t1") == "COMMITTED"
    assert all(p.state == "COMMITTED" and not p.locks_held for p in ps)
    ps, c = cluster(True, False, True)
    assert c.run("t2") == "ABORTED"
    assert all(p.state == "ABORTED" and not p.locks_held for p in ps)

    # Coordinator dies after collecting YES votes: everyone is blocked.
    for crash, expected in [("before_decision", "ABORTED"),
                            ("after_decision", "COMMITTED")]:
        ps, c = cluster(True, True, True)
        try:
            c.run("t3", crash=crash)
        except Crash:
            pass
        for p in ps:
            p.on_timeout()
        assert all(p.state == "PREPARED" and p.locks_held for p in ps)
        assert ask_peers(ps) is None  # nobody can decide: the blocking problem
        assert c.recover("t3") == expected  # only the coordinator's log knows
        assert all(p.state == expected and not p.locks_held for p in ps)
        c.recover("t3")  # re-delivery is harmless
    print("ok")
```

## Saga pattern

A saga (Garcia-Molina and Salem, 1987) splits one long transaction into a sequence of local transactions, each committed immediately in its own service. If step `k` fails, the saga runs compensating transactions for steps `k-1` down to 1. There is no global lock and no blocking; the price is that intermediate states are visible and undo is semantic, not physical.

### Compensations

A compensation is a new business action that semantically reverses an earlier one: refund a charge, release a reservation, send a cancellation email. It is not a rollback. The charge happened and stays in the ledger; the refund is a second entry.

Rules that make sagas work:

- **Order steps by reversibility.** Compensable steps first, then the pivot (the go/no-go step, after which the saga must finish), then retriable steps that are guaranteed to succeed eventually. Put the step most likely to fail early and the hardest to undo (sending an email, calling a partner) late.
- **Compensations must eventually succeed.** Retry them with backoff; if they keep failing, park the saga for an operator. A dropped compensation is silent data corruption.
- **Everything is idempotent.** The orchestrator may retry any step or compensation after a timeout, so each one is keyed by the saga ID (see [idempotency]({{ '/backend/idempotency/' | relative_url }})).
- **Separate transient from permanent failures.** Retry timeouts; compensate on business failures ("card declined", "address not serviceable").

### Choreography vs orchestration

| | Choreography | Orchestration |
|---|---|---|
| Control | Each service reacts to an event and emits the next | A central orchestrator sends commands and records progress |
| Coupling | Services share event contracts, not each other | Orchestrator knows every participant |
| Visibility | Flow is implicit, spread across services | Flow and current state live in one place |
| Failure handling | Every service listens for failure events | Orchestrator drives compensation in reverse |
| Fits | 2–4 steps, few branches | Many steps, timeouts, branching, manual review |
| Risk | Cyclic event chains; "where is order 42?" is hard to answer | Orchestrator grows into a god service and must be highly available |

Orchestrators are often a workflow engine (Temporal, AWS Step Functions, Camunda) or a `saga` table plus workers. Choreography is natural on top of [event-driven architecture]({{ '/backend/event-driven-architecture/' | relative_url }}).

### Isolation: sagas are ACD, not ACID

Other requests see a saga's intermediate states: stock reserved for an order that will be cancelled, a charge about to be refunded. Countermeasures: semantic locks (a `PENDING` status other flows respect), commutative updates (increments instead of overwrites), re-reading a value before acting on it, and ordering steps so that the visible intermediate states are harmless.

A runnable orchestrator. Shipping fails permanently, the refund times out once and is retried, and every action is idempotent by saga ID:

```python
from functools import partial


class BusinessError(Exception):
    """Permanent failure: retrying will not help, so compensate."""


def retry(fn, attempts=5):
    """Retry transient errors. Backoff and jitter omitted for the demo."""
    for i in range(attempts):
        try:
            return fn()
        except ConnectionError:
            if i == attempts - 1:
                raise  # park for an operator; never drop a compensation


class Shop:
    """Three services' state. Every action and compensation is idempotent,
    keyed by saga id, because the orchestrator may call it more than once."""

    def __init__(self, stock=5, ship_fails=False, refund_timeouts=0):
        self.stock, self.reserved, self.charges, self.shipments = stock, {}, {}, {}
        self.ship_fails, self.refund_timeouts = ship_fails, refund_timeouts

    def reserve(self, sid):
        if sid in self.reserved:
            return
        if self.stock < 1:
            raise BusinessError("out of stock")
        self.stock -= 1
        self.reserved[sid] = 1

    def release(self, sid):
        if self.reserved.pop(sid, None):
            self.stock += 1

    def charge(self, sid):
        self.charges.setdefault(sid, "charged")

    def refund(self, sid):
        if self.refund_timeouts:
            self.refund_timeouts -= 1
            raise ConnectionError("payment provider timeout")
        if self.charges.get(sid) == "charged":
            self.charges[sid] = "refunded"

    def ship(self, sid):
        if self.ship_fails:
            raise BusinessError("address not serviceable")
        self.shipments[sid] = "scheduled"


class SagaOrchestrator:
    def __init__(self, steps):
        self.steps = steps  # [(name, action, compensation)]
        self.log = []       # stands in for a durable saga_log table

    def run(self, sid):
        done = []
        for name, action, compensate in self.steps:
            try:
                retry(partial(action, sid))
            except BusinessError:
                self.log.append((sid, name, "failed"))
                for cname, comp in reversed(done):
                    retry(partial(comp, sid))
                    self.log.append((sid, cname, "compensated"))
                return "COMPENSATED"
            done.append((name, compensate))
            self.log.append((sid, name, "done"))
        return "COMPLETED"


def checkout(shop):
    return SagaOrchestrator([
        ("reserve", shop.reserve, shop.release),
        ("charge", shop.charge, shop.refund),
        ("ship", shop.ship, None),  # last step: nothing after it to undo
    ])


if __name__ == "__main__":
    shop = Shop()
    assert checkout(shop).run("o1") == "COMPLETED"
    assert (shop.stock, shop.charges["o1"], shop.shipments["o1"]) == (
        4, "charged", "scheduled")

    # Shipping fails permanently; the refund times out once and is retried.
    shop = Shop(ship_fails=True, refund_timeouts=1)
    saga = checkout(shop)
    assert saga.run("o2") == "COMPENSATED"
    assert shop.stock == 5 and shop.charges["o2"] == "refunded"
    assert [s for _, s, _ in saga.log] == [
        "reserve", "charge", "ship", "charge", "reserve"]

    # First step fails: nothing to compensate, card never charged.
    shop = Shop(stock=0)
    assert checkout(shop).run("o3") == "COMPENSATED" and not shop.charges

    # Compensations are idempotent: a duplicate release changes nothing.
    shop = Shop()
    shop.reserve("o4")
    shop.release("o4")
    shop.release("o4")
    assert shop.stock == 5
    print("ok")
```

In production the log is a table written in the same transaction as the orchestrator's state change, so a restarted orchestrator resumes from the last recorded step.

## Transactional outbox pattern

Every saga step and every event-driven service hits the dual-write problem: commit to the database and publish to Kafka. Commit first and crash, and the event is lost. Publish first and the transaction rolls back, and you announced something that never happened.

The outbox fixes it by making the event part of the local transaction. Insert the event into an `outbox` table in the same transaction as the business change. A separate relay reads unpublished rows, publishes them, and marks them sent, either by polling (`SELECT ... FOR UPDATE SKIP LOCKED`) or with change data capture (Debezium reading the Postgres WAL). Delivery is at-least-once, so consumers deduplicate by event ID, often with an inbox table. Use the aggregate ID as the partition key to keep per-entity order. See [delivery semantics]({{ '/backend/delivery-semantics/' | relative_url }}) and [Postgres as a queue]({{ '/postgresql/postgres-as-a-queue/' | relative_url }}).

## TCC (Try-Confirm-Cancel)

TCC is a reservation-based variant. **Try** checks and reserves resources without making the business effect visible (freeze 100 from the balance, hold a seat with an expiry). **Confirm** makes it final using only what was reserved, and must not fail for business reasons. **Cancel** releases the reservation. Card payments work this way: authorize, then capture or void.

Compared with a saga, TCC gives better isolation (nobody sees a half-done order as real) at the cost of every participant implementing a three-call API. Two TCC-specific bugs to guard against: a Cancel that arrives before its Try (record it, and make the late Try a no-op) and reservations that never get confirmed (expire them).

## In practice

Postgres prepared transactions, the participant side of 2PC (illustrative). They require `max_prepared_transactions > 0`, which defaults to 0:

```sql
BEGIN;
UPDATE accounts SET balance = balance - 100 WHERE id = 1;
PREPARE TRANSACTION 'transfer-8f2c-shard1';   -- survives a crash, keeps locks

-- later, from any session, once the coordinator has decided:
COMMIT PREPARED 'transfer-8f2c-shard1';
-- or: ROLLBACK PREPARED 'transfer-8f2c-shard1';

-- orphans hold locks and block VACUUM; alert on old ones
SELECT gid, prepared, owner FROM pg_prepared_xacts
WHERE prepared < now() - interval '5 minutes';
```

A Django outbox with a polling relay (illustrative):

```python
from django.db import models, transaction
from django.utils import timezone


class OutboxEvent(models.Model):
    topic = models.CharField(max_length=128)
    key = models.CharField(max_length=64)  # aggregate id = partition key
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True)


def place_order(user, cart):
    with transaction.atomic():
        order = Order.objects.create(user=user, total=cart.total, status="PENDING")
        OutboxEvent.objects.create(
            topic="orders.placed",
            key=str(order.id),
            payload={"order_id": order.id, "total": str(cart.total)},
        )
    return order


def relay_once(producer, batch=100):
    with transaction.atomic():
        events = list(
            OutboxEvent.objects.select_for_update(skip_locked=True)
            .filter(published_at__isnull=True)
            .order_by("id")[:batch]
        )
        for e in events:
            producer.send(e.topic, key=e.key.encode(), value=e.payload)
        producer.flush()  # crash before the update below means re-publish
        OutboxEvent.objects.filter(id__in=[e.id for e in events]).update(
            published_at=timezone.now()
        )
```

## Pitfalls

- **Orphaned prepared transactions.** A coordinator that never returns leaves locks held and blocks VACUUM. Monitor `pg_prepared_xacts` and resolve them from the coordinator's log.
- **Compensations that can fail permanently.** A refund rejected because the card was closed must still end somewhere: a manual-review queue with an alert, never a log line.
- **Non-idempotent steps.** A timeout followed by a retry charges twice. Pass the saga ID as an idempotency key to every downstream call.
- **Choreography spaghetti.** Ten services reacting to each other's events with no owner of the overall flow. Move to orchestration once nobody can explain the full path.
- **Publishing inside the transaction.** `producer.send()` before `COMMIT` publishes events for rolled-back data. Use the outbox.
- **Assuming the outbox gives exactly-once.** It gives at-least-once. Consumers must deduplicate.
- **Unbounded outbox growth.** Delete or partition published rows, and alert on the age of the oldest unpublished row.

## Interview questions

**Why not use 2PC between microservices?** It couples availability (every participant and the coordinator must be up), holds locks across network round trips, blocks when the coordinator fails after prepare, and most HTTP APIs and brokers cannot participate. Sagas plus an outbox trade isolation for availability and loose coupling.

**What exactly is the blocking problem?** A participant that voted YES must neither commit nor abort on its own. If the coordinator fails before telling it the outcome and no peer knows, it waits with locks held until the coordinator recovers.

**How does a coordinator recover?** It logs the decision durably before phase 2. On restart it re-sends logged decisions and presumes abort for transactions with no decision. Participants accept duplicate decisions.

**Choreography or orchestration?** Choreography for short flows with few steps and clear event ownership. Orchestration when the flow has many steps, timeouts or branches, or when you need to answer "what state is this order in?" from one place.

**What makes a good compensation?** It is a semantic undo (refund, release, cancel) that is idempotent, retried until it succeeds, and safe to run even if the forward step only partly happened.

**How do you publish an event reliably when a row changes?** Transactional outbox: write the event row in the same local transaction, relay it with polling or CDC, and make consumers idempotent because delivery is at-least-once.

**Saga vs TCC?** A saga commits each step for real and undoes with compensations, so intermediate states are visible. TCC reserves first and confirms or cancels later, giving better isolation but requiring a reservation API in every participant.

## Further reading

- *Sagas*, Hector Garcia-Molina and Kenneth Salem (SIGMOD 1987).
- *Consensus on Transaction Commit*, Jim Gray and Leslie Lamport (ACM TODS, 2006).
- *Life beyond Distributed Transactions: an Apostate's Opinion*, Pat Helland (CIDR 2007).
- *Microservices Patterns*, Chris Richardson (Manning, 2018), chapters on sagas and transactional messaging.
- PostgreSQL documentation: `PREPARE TRANSACTION` and `max_prepared_transactions`.
