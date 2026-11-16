---
title: Event sourcing and CQRS
section: backend
order: 44
phase: "Phase 6 · Messaging and events"
summary: Store state as an append-only event log, rebuild it with snapshots and upcasters, and serve reads from CQRS projections. Know when to skip both.
---

A normal table stores the latest state: `UPDATE accounts SET balance = 70` and the fact that it was 120 a minute ago is gone. Audit tables bolted on later drift from reality because nothing forces them to be complete. Event sourcing flips this: the append-only log of domain events *is* the source of truth, and current state is derived by replaying it. CQRS is the companion idea that the model you write through does not have to be the model you read from. They are often used together but are independent; most systems that benefit from CQRS do not need event sourcing.

## Event sourcing

### How it works

Each aggregate (an account, an order) owns a stream of immutable, past-tense events: `AccountOpened`, `MoneyDeposited`, `MoneyWithdrawn`. Handling a command is always four steps:

```text
 command            load stream             decide            append
 Withdraw(50) --> [Opened, Deposited(100)] --> balance=100 --> Withdrawn(50)
                  fold events into state      invariant ok    expected v=2
                                                                  |
                     events table (append-only) <-----------------+
                     stream acct-42: v1, v2, v3
                                |
              projectors read in global order
                                |
               +----------------+-----------------+
               v                v                 v
         balances view    statements view    search index
```

1. Load the stream and fold it into state: `state = reduce(apply, events, initial)`.
2. Validate the command against that state (no overdraft, order not already shipped).
3. Produce new events describing what happened. Events are facts, not requests: `MoneyWithdrawn`, never `WithdrawMoney`.
4. Append them with the version you loaded as the *expected version*. If someone else appended first, the append fails and you reload and retry.

An event carries the intent, the data needed to apply it, and metadata (event id, correlation id, actor, timestamp, schema version). `AccountUpdated` with a full row copy is CRUD in disguise: it loses the reason.

### Event store

An event store needs four operations: append to a stream with an expected version, read one stream from version N, read all events in a global order from position P (for projections), and subscribe to new events. A PostgreSQL table covers all of them:

```sql
CREATE TABLE events (
  position       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  stream_id      text        NOT NULL,
  version        int         NOT NULL,
  type           text        NOT NULL,
  schema_version smallint    NOT NULL DEFAULT 1,
  data           jsonb       NOT NULL,
  metadata       jsonb       NOT NULL DEFAULT '{}',
  recorded_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (stream_id, version)            -- optimistic concurrency lives here
);

-- Append: we loaded acct-42 at version 7, so the new event is version 8.
INSERT INTO events (stream_id, version, type, schema_version, data)
VALUES ('acct-42', 8, 'MoneyWithdrawn', 2,
        '{"amount_cents": 5000, "currency": "USD"}');
-- unique_violation => another writer got version 8 first: reload, re-decide
```

| Option | Strength | Watch out for |
|---|---|---|
| PostgreSQL table | One database, transactions, familiar ops | Global position gaps (below), table growth |
| EventStoreDB (renamed KurrentDB) | Purpose-built streams, subscriptions | Another system to run |
| DynamoDB | Conditional put on `(stream_id, version)` scales well | Global ordering needs streams/CDC |
| Kafka | Great for distributing events | No per-stream expected-version append, slow single-entity reads, retention deletes history unless configured |

> **Gotcha:** an identity or sequence column is not commit order. Transaction A takes position 101, transaction B takes 102 and commits first; a projector that reads "position > 100" sees 102, advances its checkpoint, and never sees 101. Fix it by serializing appends (an advisory lock), or by reading only up to a high-water mark that does not advance past a gap until the gap is older than a few seconds.

### Rebuilding state

State is a left fold over the stream, so `apply` must be pure and deterministic: no `now()`, no random ids, no HTTP calls, no emails. Anything nondeterministic is decided once in the command handler and recorded in the event. That discipline buys three things:

- **Rebuildable read models.** Fix a bug in a projection, drop it, replay, and history is corrected.
- **Temporal queries.** Replay up to a version or timestamp to answer "what was the balance on March 1" or "what did the order look like when support got the ticket".
- **Debugging.** Copy a stream into a test to reproduce the exact state.

Side effects (send a receipt, charge a card) belong in idempotent reactors that subscribe to live events with their own checkpoint, never in a replay.

### Snapshots

Replaying 20 events per command is nothing. Replaying 50,000 for a long-lived account on every write is not. A snapshot stores `(stream_id, version, state, snapshot_schema)`; loading becomes "latest snapshot plus events after its version".

- Take one every N events (100–1,000 is typical), ideally asynchronously.
- A snapshot is a cache. You can delete all of them at any time and the system is still correct, only slower.
- When the aggregate's state shape changes, bump the snapshot schema and ignore old snapshots rather than migrating them.
- Needing snapshots everywhere often means streams are too long. "Closing the books" (an accounting idea) ends a stream with a summary event, such as a period-end balance, and starts a fresh stream for the next period.

### Schema evolution and upcasting

Events are written once and read forever, by code that keeps changing. Rules: never change the meaning of an existing event type, never remove a field consumers depend on, and prefer additive changes.

| Strategy | How | Use when |
|---|---|---|
| Tolerant reader | New optional fields with defaults; ignore unknown fields | Most changes |
| Upcasting | A function maps v1 payload to v2 at read time, before `apply` | Renamed, split or re-typed fields |
| New event type | `AccountOpenedWithCurrency` alongside the old type | The meaning changed |
| Copy and transform | Rewrite streams into a new store, then switch | Large cleanups; last resort |

Upcasters chain (v1 to v2 to v3), so domain code only ever sees the latest shape, and the stored bytes are never mutated. Personal data is the hard case: immutable logs conflict with erasure requests. Either keep PII out of events (store a reference to a deletable record) or use crypto-shredding: encrypt PII with a per-subject key and delete the key to make the data unreadable.

### When to use it

Event sourcing fits domains where history is a feature: ledgers, payments, wallets, inventory movements, order lifecycles, regulated workflows. Double-entry bookkeeping is the original example; a journal of entries is never edited, only corrected with new entries. Martin Fowler's public write-up of the LMAX exchange describes an in-memory business logic processor whose state is rebuilt from its input event journal.

Skip it for CRUD-shaped domains, admin back offices, and anything where nobody will ever ask "how did we get here". It adds projections, versioning and eventual consistency. Apply it to the one core subdomain that needs it, not the whole system. See [Domain-driven design]({{ '/backend/domain-driven-design/' | relative_url }}) for aggregate boundaries.

## CQRS

### Separate read and write models

Command-query separation (Bertrand Meyer) says a method either changes state or returns data, not both. CQRS, named by Greg Young, applies the same split to whole models: the write side handles commands through aggregates that enforce invariants; the read side serves queries from models shaped for each screen or API.

The levels, from cheap to expensive:

| Level | Write model | Read model | Consistency |
|---|---|---|---|
| 1. Code split | Domain services and aggregates | Raw SQL / `.values()` queries on the same tables | Immediate |
| 2. Same database, extra tables | Normalized tables | Denormalized tables or materialized views updated in the same transaction | Immediate |
| 3. Separate stores | Normalized DB or event store | Async projections into Postgres, Elasticsearch, Redis | Eventual |

Level 1 is just good layering. Level 3 is what people usually mean, and the only level that buys independent scaling and a different storage engine per query shape. CQRS is not read replicas: a replica has the same model; CQRS has different models.

### Projections

A projector consumes events in order and updates a read model. Two properties make it safe:

- **Checkpoint in the same transaction as the update.** Store the last applied position next to the read model; a crash then resumes exactly where it stopped. If the read model lives in another store (Elasticsearch), make the update idempotent instead.
- **Idempotent writes.** Guard on aggregate version so a replayed event is a no-op.

```sql
BEGIN;
UPDATE account_balances
   SET balance_cents = balance_cents - 5000, version = 8
 WHERE account_id = 'acct-42' AND version < 8;       -- replay-safe
UPDATE projection_checkpoints SET position = 19377 WHERE name = 'balances';
COMMIT;
```

Rebuilding without downtime is blue/green for tables: create `account_balances_v2`, replay from position 0 while v1 keeps serving, let v2 catch up to the head, switch the view or the query to v2, then drop v1. One event stream can feed many projections: balances, monthly statements, fraud features, a search index.

### Eventual consistency of reads

With async projections a user can submit a form and then not see the change. Options, roughly in order of preference:

- Return the new version or log position from the command; the read API waits (with a short timeout) until the projection checkpoint passes it.
- Read the user's own just-written entity from the write model, and everything else from projections.
- Update the UI optimistically from the command response.
- Make the one critical view synchronous (level 2) and leave the rest async.

Alert on projection lag (head position minus checkpoint, in events and seconds). See [CAP and consistency models]({{ '/backend/cap-and-consistency-models/' | relative_url }}) for read-your-writes.

### When not to use CQRS

Fowler's own warning is that for most systems CQRS adds risky complexity. Do not use it when reads and writes have the same shape, the domain is CRUD, the team is small, or the business cannot tolerate stale reads and you would end up making everything synchronous anyway. Reach for it when reads outnumber writes by orders of magnitude and need different shapes (feeds, dashboards, search), when the write side has real invariants worth isolating, or when several teams need their own views of the same facts.

| | Event sourcing alone | CQRS alone | Both |
|---|---|---|---|
| Source of truth | Event log | Normalized tables | Event log |
| Reads | Hard (replay per query) | Projections or views | Projections |
| History | Complete | Only what you log | Complete |
| Typical fit | Rare in practice | Common | Ledgers, order lifecycles |

## In practice

### Runnable: an event-sourced account

The demo covers an event store with optimistic concurrency, an upcaster for a legacy v1 event, snapshots, an as-of query, and a projection that can be rebuilt from scratch.

```python
import json
from dataclasses import dataclass
from decimal import Decimal

SNAPSHOT_EVERY = 5


class ConcurrencyError(Exception):
    pass


class EventStore:
    """Append-only log with per-stream versions and a global position."""

    def __init__(self):
        self.log = []        # (position, stream_id, version, json_event)
        self.versions = {}   # stream_id -> current version
        self.snapshots = {}  # stream_id -> dict(version=..., balance_cents=..., ...)

    def append(self, stream_id, expected_version, events):
        # In Postgres this check is UNIQUE (stream_id, version) on INSERT.
        if self.versions.get(stream_id, 0) != expected_version:
            raise ConcurrencyError(f"{stream_id}: expected v{expected_version}")
        for event in events:
            expected_version += 1
            self.log.append((len(self.log) + 1, stream_id, expected_version,
                             json.dumps(event)))  # stored bytes are immutable
        self.versions[stream_id] = expected_version

    def read_stream(self, stream_id, after_version=0):
        return [json.loads(e) for _, s, v, e in self.log
                if s == stream_id and v > after_version]

    def read_all(self, after_position=0):
        return [(p, s, json.loads(e)) for p, s, _, e in self.log
                if p > after_position]


def event(type_, **data):
    return {"type": type_, "schema": 2, "data": data}


def upcast(ev):
    """v1 money events stored dollars as a string; v2 uses integer cents."""
    if ev["type"] in ("MoneyDeposited", "MoneyWithdrawn") and ev["schema"] == 1:
        cents = int(Decimal(ev["data"]["amount"]) * 100)
        data = {"amount_cents": cents, "currency": "USD"}
        return dict(ev, schema=2, data=data)
    return ev


@dataclass
class Account:
    id: str
    version: int = 0
    balance_cents: int = 0
    is_open: bool = False

    def apply(self, ev):  # pure: no I/O, no clock, no randomness
        kind, data = ev["type"], ev["data"]
        if kind == "AccountOpened":
            self.is_open = True
        elif kind == "MoneyDeposited":
            self.balance_cents += data["amount_cents"]
        elif kind == "MoneyWithdrawn":
            self.balance_cents -= data["amount_cents"]
        self.version += 1


def load(store, account_id, use_snapshot=True, upto_version=None):
    snap = store.snapshots.get(account_id) if use_snapshot else None
    if snap and upto_version is not None and snap["version"] > upto_version:
        snap = None  # as-of query older than the snapshot: replay from 0
    acc = Account(account_id, **snap) if snap else Account(account_id)
    for ev in store.read_stream(account_id, after_version=acc.version):
        if upto_version is not None and acc.version >= upto_version:
            break
        acc.apply(upcast(ev))
    return acc


def execute(store, account_id, decide):
    """Load, decide new events, append with optimistic concurrency, snapshot."""
    acc = load(store, account_id)
    new_events = decide(acc)
    store.append(account_id, acc.version, new_events)
    for ev in new_events:
        acc.apply(ev)
    if acc.version // SNAPSHOT_EVERY > (acc.version - len(new_events)) // SNAPSHOT_EVERY:
        state = dict(vars(acc))
        del state["id"]
        store.snapshots[account_id] = state  # a cache: safe to delete any time


def open_account(acc):
    if acc.is_open:
        raise ValueError("already open")
    return [event("AccountOpened", currency="USD")]


def deposit(cents):
    def decide(acc):
        if not acc.is_open:
            raise ValueError("account not open")
        return [event("MoneyDeposited", amount_cents=cents, currency="USD")]
    return decide


def withdraw(cents):
    def decide(acc):
        if cents > acc.balance_cents:
            raise ValueError("insufficient funds")
        return [event("MoneyWithdrawn", amount_cents=cents, currency="USD")]
    return decide


class BalanceProjection:
    """Read model: account_id -> balance, with a checkpoint for resuming."""

    def __init__(self):
        self.balances, self.checkpoint = {}, 0

    def catch_up(self, store):
        for position, stream_id, ev in store.read_all(self.checkpoint):
            ev = upcast(ev)
            if ev["type"] == "AccountOpened":
                self.balances[stream_id] = 0
            elif ev["type"] == "MoneyDeposited":
                self.balances[stream_id] += ev["data"]["amount_cents"]
            elif ev["type"] == "MoneyWithdrawn":
                self.balances[stream_id] -= ev["data"]["amount_cents"]
            self.checkpoint = position  # same transaction as the update in SQL


if __name__ == "__main__":
    store = EventStore()
    execute(store, "acct-1", open_account)
    # An event written by last year's code, still in the log in its v1 shape.
    v1_data = {"amount": "12.50"}
    legacy = {"type": "MoneyDeposited", "schema": 1, "data": v1_data}
    store.append("acct-1", 1, [legacy])
    for cents in (1000, 2000, 3000):
        execute(store, "acct-1", deposit(cents))
    execute(store, "acct-1", withdraw(500))
    execute(store, "acct-2", open_account)

    acc = load(store, "acct-1")
    assert acc.balance_cents == 1250 + 6000 - 500 and acc.version == 6
    assert store.snapshots["acct-1"]["version"] == 5           # snapshot taken
    assert load(store, "acct-1", use_snapshot=False) == acc    # replay agrees
    assert load(store, "acct-1", upto_version=2).balance_cents == 1250  # as-of

    try:
        execute(store, "acct-1", withdraw(10**9))
    except ValueError:
        pass
    else:
        raise AssertionError("invariant not enforced")

    # Two writers load version 6; the second append must lose.
    stale = load(store, "acct-1")
    execute(store, "acct-1", deposit(100))
    try:
        store.append("acct-1", stale.version, [event("MoneyWithdrawn",
                                                     amount_cents=1, currency="USD")])
    except ConcurrencyError:
        pass
    else:
        raise AssertionError("lost update")

    proj = BalanceProjection()
    proj.catch_up(store)
    assert proj.balances == {"acct-1": 6850, "acct-2": 0}
    rebuilt = BalanceProjection()           # drop and replay from position 0
    rebuilt.catch_up(store)
    assert rebuilt.balances == proj.balances and rebuilt.checkpoint == len(store.log)
    print("ok", proj.balances)
```

In Django, the write path is one `transaction.atomic()` block that loads, decides and inserts events (plus an outbox row if other services consume them, see [Delivery semantics]({{ '/backend/delivery-semantics/' | relative_url }})). Projectors run as a management command that pulls events after the checkpoint in batches and updates the checkpoint in the same transaction.

## Pitfalls

- **Side effects in `apply`.** Replaying a stream re-sends emails or re-charges cards. Keep `apply` pure; side effects go in idempotent reactors on live events only.
- **CRUD events.** `OrderUpdated` with a full row tells you nothing. Name the business fact.
- **Skipped events in projections** from sequence gaps under concurrent appends. Symptom: a read model permanently missing an event the store has. Fix with serialized appends or a gap-aware high-water mark.
- **Non-idempotent projectors.** A crash between update and checkpoint double-applies a deposit. Same transaction, or version guards.
- **Changing event meaning in place.** Old events replay into wrong state. Add a type or an upcaster.
- **Kafka as the system of record with default retention.** `retention.ms` defaults to seven days; history silently disappears. Use the log as transport, keep the store elsewhere, or set retention deliberately.
- **Event sourcing the whole system.** The cost is per aggregate; apply it where history matters.

## Interview questions

**How do you handle two concurrent writes to the same aggregate?** Each writer loads the stream at version N and appends at N+1. A unique constraint on `(stream_id, version)`, or the store's expected-version check, rejects the second append. The loser reloads, re-validates its command against the new state and retries, or returns a conflict.

**How do you change an event's schema?** Additive changes with defaults first. For renames or type changes, write an upcaster that converts old payloads at read time so domain code sees one shape. If the meaning changes, add a new event type. Never edit stored events in place except in a planned copy-and-transform migration.

**Are snapshots required?** No. They are a performance cache for long streams, safe to delete and rebuild. If you need them everywhere, look at shortening streams ("closing the books").

**How does a user see their own write when projections are async?** Return the new version from the command and have the read side wait until the projection has caught up to it, or read that entity from the write model, or update the UI optimistically.

**How do you rebuild a projection without downtime?** Build a new table from position 0 alongside the old one, let it catch up to the head, switch reads over, drop the old table.

**Event sourcing versus an audit log versus CDC?** An audit log is derived and can drift or be incomplete. CDC captures row-level changes, which is technical history without business intent. Event sourcing records intent and is the source of truth, so it cannot drift.

**How do you delete a user's data under GDPR?** Keep PII out of events and reference a deletable store, or encrypt PII per subject and delete the key (crypto-shredding).

## Further reading

- Martin Fowler, "Event Sourcing" (2005) and "CQRS" (2011), martinfowler.com
- Greg Young, "CQRS Documents" (2010) and *Versioning in an Event Sourced System* (Leanpub)
- Microsoft Azure Architecture Center, "Event Sourcing pattern" and "CQRS pattern"
- Martin Kleppmann, *Designing Data-Intensive Applications*, chapter 11 (Stream processing)
- Vaughn Vernon, *Implementing Domain-Driven Design* (Addison-Wesley, 2013)
