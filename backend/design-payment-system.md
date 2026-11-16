---
title: Design a payment system
section: backend
order: 82
phase: "Phase 10 · System design case studies"
summary: "Payment intents, end-to-end idempotency, PSP integration, a double-entry ledger, reconciliation, webhooks and refunds without double charging."
---

A payment system is the one design where "mostly correct" is a bug. Throughput is modest; the hard part is that any network call can fail after the money has moved, and you must still end with exactly one charge, a ledger that balances, and a trail an auditor can follow. The interview signal is naming the unknown-outcome problem and closing it with idempotency, a state machine, a ledger and reconciliation.

## Requirements

### Clarifying questions

- Merchant platform on top of payment service providers (PSPs) like Stripe or Adyen, or a PSP talking to card networks? Assume the former: a marketplace's internal payments platform.
- Methods? Cards and wallets through PSPs. Authorize then capture at shipment, or immediate capture? Both.
- Seller payouts? Yes, daily from ledger balances. Multi-currency? Yes, charge in the buyer's currency.

### Functional

- Create and confirm a payment for an order; support 3-D Secure challenges.
- Capture (full or partial), cancel an authorization, refund (full or partial).
- Record every money movement in a double-entry ledger; derive seller balances; run payouts.
- Emit payment events to other services; receive PSP webhooks.
- Reconcile daily against PSP and bank reports.

### Non-functional

- 10M payments/day, peak 10× average during sales events.
- Exactly-once effect: no double charge, no lost payment, across client retries, crashes and PSP timeouts.
- Authorization p99 under 2 s (dominated by the PSP and 3-D Secure); our own overhead under 100 ms.
- 99.99% availability for accepting payments. Ledger strongly consistent. RPO 0 for committed payments.
- Status reads about 5× writes. Immutable audit history, retained for years (commonly 7+).

### Out of scope

Fraud model internals, card network processing, tax calculation, FX rate sourcing (we consume a rate feed), crypto.

## Capacity estimates

```text
Payments    10M/day / 86,400 s         ≈ 116/s avg, ~1,200/s peak
PSP calls   ~2 per payment (auth, capture) ≈ 230/s avg, ~2,400/s peak
Webhooks    ~3 per payment             ≈ 350/s avg, ~3,500/s peak
Ledger      ~5 lines per payment = 50M lines/day
                                       ≈ 580/s avg, ~5,800/s peak
Per payment intent 1 KB + attempts 1 KB + ledger 1 KB
            + events and webhooks 2 KB ≈ 5 KB
Storage     10M x 5 KB = 50 GB/day     ≈ 18 TB/year
            hot 13 months ≈ 20 TB; 7-year archive ≈ 130 TB
Idem keys   10M/day x 1 KB x 7 days    ≈ 70 GB
Bandwidth   1,200/s x 5 KB             ≈ 6 MB/s
```

This fits one well-provisioned PostgreSQL primary with a synchronous standby. Shard later for headroom and blast radius; the effort goes into correctness.

## API design

Amounts are integers in the currency's minor unit, always paired with an ISO 4217 code.

```http
POST /v1/payment_intents
Idempotency-Key: 5d1f6c0e-2b7a-4e0f-9a51-ord88412
Content-Type: application/json

{"order_id": "ord_88412", "amount_minor": 4999, "currency": "USD",
 "customer_id": "cus_19", "payment_method": "pm_tok_abc",
 "seller_id": "sel_7", "capture_method": "manual"}
```

```json
{"id": "pi_3x9", "status": "requires_capture", "amount_minor": 4999,
 "currency": "USD", "next_action": null, "created_at": "2026-11-16T10:00:00Z"}
```

| Endpoint | Purpose |
|---|---|
| `POST /v1/payment_intents/{id}/confirm` | Authorize; may return `requires_action` with a 3-D Secure URL |
| `POST /v1/payment_intents/{id}/capture` | Capture up to the authorized amount |
| `POST /v1/payment_intents/{id}/cancel` | Void an uncaptured authorization |
| `POST /v1/refunds` | `{payment_intent_id, amount_minor, reason}`, with Idempotency-Key |
| `GET /v1/payment_intents/{id}` | Status, attempts, refunds |
| `POST /webhooks/psp/{provider}` | Inbound PSP events, signature verified |

Errors: `402` declined (with a decline code), `409` same key still in flight, `422` key reused with a different body, `202` outcome still processing.

## Data model

PostgreSQL. A payment step writes the intent, the attempt, ledger lines and an outbox row in one commit; unique and check constraints are the last line of defense against application bugs; and the volume does not justify giving up multi-row atomicity. See [ACID and transactions]({{ '/backend/acid-and-transactions/' | relative_url }}).

```sql
-- Illustrative schema (abridged)
CREATE TABLE payment_intents (
  id             bigint PRIMARY KEY,
  merchant_id    bigint  NOT NULL,
  order_id       text    NOT NULL,
  amount_minor   bigint  NOT NULL CHECK (amount_minor > 0),
  currency       char(3) NOT NULL,
  status         text    NOT NULL,
  captured_minor bigint  NOT NULL DEFAULT 0,
  refunded_minor bigint  NOT NULL DEFAULT 0,
  version        int     NOT NULL DEFAULT 0,
  CHECK (refunded_minor <= captured_minor),
  UNIQUE (merchant_id, order_id)
);

CREATE TABLE payment_attempts (
  id            bigint PRIMARY KEY,       -- doubles as the PSP idempotency key
  intent_id     bigint NOT NULL REFERENCES payment_intents,
  operation     text   NOT NULL,          -- authorize | capture | refund
  psp           text   NOT NULL,
  psp_reference text,
  status        text   NOT NULL,          -- pending | succeeded | failed | unknown
  UNIQUE (psp, psp_reference)
);

CREATE TABLE ledger_transactions (
  id         bigint PRIMARY KEY,
  kind       text NOT NULL,               -- capture | settlement | refund | payout
  source_ref text NOT NULL,               -- attempt id, settlement batch id, ...
  posted_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (kind, source_ref)               -- posting the same event twice is a no-op
);

CREATE TABLE ledger_entries (
  txn_id       bigint  NOT NULL REFERENCES ledger_transactions,
  account_id   bigint  NOT NULL,
  direction    char(1) NOT NULL CHECK (direction IN ('D', 'C')),
  amount_minor bigint  NOT NULL CHECK (amount_minor > 0),
  currency     char(3) NOT NULL
);
CREATE INDEX ON ledger_entries (account_id, txn_id);
```

Also: `idempotency_keys` (primary key `(merchant_id, key)`, request hash, state, stored response), `webhook_events (provider, event_id)` as primary key, an `outbox` table, `refunds`, and reconciliation tables. Ledger tables are append-only: the application role gets `INSERT` and `SELECT` but not `UPDATE` or `DELETE`, and a deferred constraint trigger checks at commit that each transaction's debits equal its credits per currency.

## High-level design

```text
 Browser / app ──card data──► PSP hosted fields ──► token pm_...
      │ order + token
      ▼
 Order service ──► Payment API ──► Payment service ──► PSP adapter ──► PSP
                   (idempotency)      │      ▲                     │
                                      │      └── webhooks ◄────────┤
                                      ▼                            │
                     ┌──────────────────────────────┐              │
                     │ PostgreSQL: keys, intents,   │              │
                     │ attempts, ledger, outbox     │              │
                     └──────────────────────────────┘              │
                        │                     ▲                    │
                  outbox relay           recon job ◄── settlement ─┘
                        │                     │        reports
                        ▼                     ▼
                Kafka payment.*        exceptions queue ─► ops UI
                        │
          orders, notifications, payouts, analytics
```

Main flows:

1. **Tokenize.** The client collects card details in the PSP's hosted fields or SDK and receives a token. Card numbers never touch our servers.
2. **Create.** The order service calls `POST /payment_intents` with an idempotency key derived from the order. The payment service records the key, the intent and a `pending` attempt in one transaction.
3. **Authorize.** Outside any database transaction, the adapter calls the PSP with the attempt id as the PSP's idempotency key. A second transaction records the result, moves the intent, writes an outbox event and stores the response on the idempotency key.
4. **Async results.** 3-D Secure, delayed captures and disputes arrive as webhooks. They are verified, deduplicated, and applied through the same state machine.
5. **Capture and ledger.** Capture posts ledger lines: money due from the PSP, the seller's payable, our commission, the PSP fee.
6. **Settle and pay out.** The PSP deposits net funds a few days later; reconciliation matches them; a payout job pays seller balances.

## Deep dives

### Payment intent state machine

Every intent and attempt has an explicit status and a table of allowed transitions. Status changes are compare-and-set updates, so a duplicate webhook or a racing retry cannot move the state backward.

```text
requires_confirmation ─► processing ─┬─► requires_action (3-D Secure)
                                      │        └─► processing
                                      ├─► requires_capture ─┬─► succeeded
                                      │                     └─► canceled
                                      ├─► succeeded (auto capture)
                                      └─► failed
```

```sql
UPDATE payment_intents
SET status = 'succeeded', captured_minor = $2, version = version + 1
WHERE id = $1 AND status IN ('processing', 'requires_capture')
RETURNING id;
-- zero rows: someone else already moved it; re-read and decide
```

Rules: transitions only move forward; `succeeded`, `failed` and `canceled` are terminal for the intent (refunds are separate resources); and a timeout is never `failed`. It is `unknown` until something proves otherwise.

### Idempotency keys end to end

One key at the API edge is not enough. Every hop where a retry can happen gets its own deduplication point:

| Hop | Key | Dedupe store | On duplicate |
|---|---|---|---|
| Client to payment API | `Idempotency-Key` header, plus unique `(merchant_id, order_id)` | `idempotency_keys` | Replay stored response |
| Payment service to PSP | Attempt id as PSP idempotency key | PSP's key store | PSP returns the original result |
| PSP to us | Webhook event id | `webhook_events` primary key | 200, skip |
| Us to consumers | Outbox event id | Consumer's processed-events table | Skip |
| Ledger posting | `(kind, source_ref)` | `ledger_transactions` unique | No-op |

The API edge claims the key with a single insert:

```sql
INSERT INTO idempotency_keys (merchant_id, key, request_hash, state)
VALUES ($1, $2, $3, 'in_progress')
ON CONFLICT (merchant_id, key) DO NOTHING
RETURNING state;
```

If no row comes back the key exists: a different `request_hash` returns 422, `done` replays the stored response, and `in_progress` returns 409 with `Retry-After`. A key stuck in `in_progress` past a timeout is resumed from the last recorded step, which is the recovery-point approach Brandur Leach described for Stripe-style idempotency on Postgres. The request runs as three phases: local transaction (key, intent, pending attempt), the PSP call with no transaction open, local transaction (result, ledger, outbox, key marked `done` with the response). Never hold a database transaction or row lock across the PSP call. See [Idempotency]({{ '/backend/idempotency/' | relative_url }}).

### PSP integration and retries without double charging

The dangerous case is the timeout after the request left your process: the card may have been charged. Classify every outcome:

| PSP outcome | Meaning | Action |
|---|---|---|
| Decline (card, fraud, funds) | Definitive no | Mark failed, no retry |
| 400-class validation error | Our bug | Mark failed, alert |
| 429, 5xx, connection refused | Probably not processed | Retry same attempt id with backoff |
| Timeout or reset mid-request | Unknown | Mark `unknown`, retry same key or query by our reference |

Rules that prevent double charges:

- Write the attempt row before calling the PSP. It is the durable anchor for every later recovery.
- Retries reuse the attempt's idempotency key. A new attempt with a new key is created only after the previous one is definitively failed.
- A resolver job sweeps attempts in `pending` or `unknown` older than about a minute, re-sends with the same key or fetches status by our reference, and applies the result through the state machine. PSPs keep idempotency keys for a limited window (Stripe documents on the order of 24 hours), so late recovery must query by reference, never blind-retry.
- Fail over to a second PSP only for attempts that provably never reached the first one. Routing a timed-out attempt to another PSP is how customers get charged twice.

Retry timing follows [Retries and backoff]({{ '/backend/retries-and-backoff/' | relative_url }}); the PSP adapter sits behind a circuit breaker from [Fault tolerance patterns]({{ '/backend/fault-tolerance-patterns/' | relative_url }}).

### Double-entry ledger

A balance column answers "how much" but not "why". A double-entry ledger records every movement as a transaction of two or more entries whose debits equal credits, per currency. Balances are derived; history is immutable; mistakes are fixed with reversing transactions. Asset and expense accounts grow with debits; liability and revenue accounts grow with credits.

| Event | Debit | Credit |
|---|---|---|
| Capture $100, 10% commission, PSP fee $3.20 | psp_clearing 96.80, psp_fees 3.20 | seller_payable 90.00, revenue 10.00 |
| PSP settles to our bank | bank 96.80 | psp_clearing 96.80 |
| Refund $30 (commission reversed pro rata) | seller_payable 27.00, revenue 3.00 | psp_clearing 30.00 |
| Payout to seller | seller_payable 63.00 | bank 63.00 |

A runnable ledger that enforces balanced, idempotent, immutable postings, plus a minimal reconciliation diff:

```python
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

EXPONENT = {"USD": 2, "EUR": 2, "JPY": 0, "KWD": 3}   # ISO 4217 minor units
DEBIT_NORMAL = {"asset", "expense"}


def to_minor(amount: str, currency: str) -> int:
    q = Decimal(amount).scaleb(EXPONENT[currency])
    if q != q.to_integral_value():
        raise ValueError(f"{amount} has too many decimals for {currency}")
    return int(q)


def bps_of(amount_minor: int, bps: int) -> int:
    """Fee in basis points, rounded once with banker's rounding."""
    exact = Decimal(amount_minor) * bps / Decimal(10_000)
    return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


@dataclass(frozen=True)
class Line:
    account: str
    debit: int = 0
    credit: int = 0


class Ledger:
    def __init__(self):
        self.accounts = {}          # name -> (kind, currency)
        self.txns = {}              # txn_id -> tuple of lines (append-only)

    def open(self, name, kind, currency):
        self.accounts[name] = (kind, currency)

    def post(self, txn_id, lines):
        lines = tuple(lines)
        if txn_id in self.txns:     # idempotent replay
            if self.txns[txn_id] != lines:
                raise ValueError("txn id reused with different lines")
            return False
        totals = defaultdict(int)
        for ln in lines:
            if ln.account not in self.accounts:
                raise KeyError(ln.account)
            if min(ln.debit, ln.credit) < 0 or bool(ln.debit) == bool(ln.credit):
                raise ValueError("each line is one positive debit or credit")
            totals[self.accounts[ln.account][1]] += ln.debit - ln.credit
        if any(totals.values()):
            raise ValueError(f"unbalanced transaction: {dict(totals)}")
        self.txns[txn_id] = lines
        return True

    def balance(self, name):
        kind = self.accounts[name][0]
        net = sum(ln.debit - ln.credit
                  for lines in self.txns.values() for ln in lines
                  if ln.account == name)
        return net if kind in DEBIT_NORMAL else -net

    def invariant_holds(self):
        net = defaultdict(int)
        for lines in self.txns.values():
            for ln in lines:
                net[self.accounts[ln.account][1]] += ln.debit - ln.credit
        return all(v == 0 for v in net.values())


def reconcile(ours, psp_report):
    """ours/psp_report: psp_reference -> amount_minor. Returns breaks."""
    breaks = []
    for ref in ours.keys() | psp_report.keys():
        a, b = ours.get(ref), psp_report.get(ref)
        if a is None:
            breaks.append(("missing_internal", ref))
        elif b is None:
            breaks.append(("missing_at_psp", ref))
        elif a != b:
            breaks.append(("amount_mismatch", ref))
    return sorted(breaks)


if __name__ == "__main__":
    led = Ledger()
    for name, kind in [("psp_clearing", "asset"), ("bank", "asset"),
                       ("psp_fees", "expense"), ("seller_payable", "liability"),
                       ("revenue", "revenue")]:
        led.open(name, kind, "USD")

    gross = to_minor("100.00", "USD")
    commission = bps_of(gross, 1000)                  # 10%
    fee = to_minor("3.20", "USD")
    capture = [Line("psp_clearing", debit=gross - fee), Line("psp_fees", debit=fee),
               Line("seller_payable", credit=gross - commission),
               Line("revenue", credit=commission)]
    assert led.post("capture:att_1", capture)
    assert not led.post("capture:att_1", capture)     # duplicate webhook: no-op
    led.post("settle:batch_9", [Line("bank", debit=9680),
                                Line("psp_clearing", credit=9680)])
    refund, refund_comm = 3000, bps_of(3000, 1000)
    led.post("refund:re_1", [Line("seller_payable", debit=refund - refund_comm),
                             Line("revenue", debit=refund_comm),
                             Line("psp_clearing", credit=refund)])
    led.post("payout:po_1", [Line("seller_payable", debit=6300),
                             Line("bank", credit=6300)])

    assert led.balance("seller_payable") == 0
    assert led.balance("revenue") == 700               # $7.00 net commission
    assert led.balance("psp_clearing") == -3000        # netted from next settlement
    assert led.invariant_holds()

    try:
        led.post("bad", [Line("bank", debit=100), Line("revenue", credit=99)])
        raise AssertionError("unbalanced post accepted")
    except ValueError:
        pass
    try:
        led.post("capture:att_1", capture[:2] + capture[3:] + capture[2:3])
        raise AssertionError("txn id reuse accepted")
    except ValueError:
        pass

    assert to_minor("1000", "JPY") == 1000 and to_minor("1.234", "KWD") == 1234
    assert bps_of(25, 1000) == 2 and bps_of(35, 1000) == 4   # half to even
    try:
        to_minor("19.999", "USD")
        raise AssertionError("sub-cent amount accepted")
    except ValueError:
        pass

    ours = {"ch_1": 4999, "ch_2": 1500, "ch_3": 700}
    psp = {"ch_1": 4999, "ch_2": 1400, "ch_4": 250}
    assert reconcile(ours, psp) == [("amount_mismatch", "ch_2"),
                                    ("missing_at_psp", "ch_3"),
                                    ("missing_internal", "ch_4")]
    print("ledger ok")
```

In PostgreSQL the same rules become constraints: positive amounts, a unique `(kind, source_ref)`, and the deferred balance trigger. For balances, either sum entries from a periodic snapshot or maintain an `account_balances` row in the same transaction. The second is fast to read but turns every posting to `revenue` or `psp_clearing` into a lock on one hot row. Split such accounts into N sub-accounts (`revenue_00` to `revenue_31`), pick one at random per posting, and sum them for reporting.

### Reconciliation

Webhooks get lost, PSPs change fees, and your own code has bugs. Reconciliation is the control that proves the ledger matches reality. Run it daily, three ways: internal ledger, PSP transaction and settlement reports, and bank statements.

| Break | Likely cause | Action |
|---|---|---|
| At PSP, missing internally | Lost webhook, crash before commit | Fetch from PSP, apply through the state machine |
| Internal, missing at PSP | Recorded success that never happened | Investigate; reverse the ledger entry |
| Amount mismatch | Fee change, FX, partial capture | Auto-post an adjustment within tolerance; else queue |
| Settled amount differs from expected | Refunds or disputes netted | Match against netting lines |
| Timing difference | Report cutoff in another time zone | Re-check next cycle before raising |

Track break rate and break age as metrics; a rising break rate is often the first sign of a payment-path bug.

### Webhooks

Inbound: verify the PSP signature over the raw body (typically HMAC-SHA256 with a timestamp; reject anything older than a few minutes; compare in constant time). Insert the event keyed by event id, return 2xx immediately, and process asynchronously. Events arrive duplicated and out of order, so the handler applies them through the forward-only state machine and, when in doubt, fetches the current object from the PSP API instead of trusting the payload. Never depend on webhooks alone: the resolver and reconciliation cover the ones that never arrive.

Outbound: state changes write an outbox row in the same transaction; a relay publishes to Kafka with the row id as event id; consumers deduplicate. Merchant-facing webhooks are signed and retried with backoff for days. See [Event-driven architecture]({{ '/backend/event-driven-architecture/' | relative_url }}).

### Refunds

- A refund is its own resource with its own idempotency key and PSP attempt.
- Reserve the amount before calling the PSP so two concurrent partial refunds cannot exceed the capture:

```sql
UPDATE payment_intents
SET refunded_minor = refunded_minor + $2
WHERE id = $1 AND refunded_minor + $2 <= captured_minor
RETURNING refunded_minor;
-- zero rows: over-refund rejected; release the reservation if the PSP declines
```

- Post ledger reversal lines on success. Refunds can fail days later (closed card); handle the `refund.failed` webhook by reversing the reversal and paying out another way.
- Before capture, prefer voiding the authorization: no money moves and fees are usually lower.
- Chargebacks are issuer-initiated, arrive by webhook, and get their own ledger lines for the disputed amount and dispute fee.

### Currency and Decimal

- Store integer minor units with the currency on every row. Exponents differ: JPY has 0 decimals, USD 2, KWD 3.
- Never use floats. `0.1 + 0.2 != 0.3`, and `Decimal(0.1)` captures the float's binary error; build `Decimal` from strings.
- Do intermediate math (basis-point fees, FX) in `Decimal`, round once with an explicit mode, and when splitting, give the remainder to one party so the parts sum to the total.
- For FX, store the rate, its source and timestamp, and both amounts. Post the difference to an FX gain/loss account. Balance per currency; never net across currencies.

### PCI scope reduction

PCI DSS covers every system that stores, processes or transmits card numbers, or can affect their security. The cheapest control is to never see them:

- Collect card data in PSP-hosted fields, iframes, payment pages or mobile SDKs. Fully outsourced card entry typically qualifies for the lightest self-assessment questionnaire (SAQ A); a page that hosts the script that posts directly to the PSP may fall under SAQ A-EP. Your acquirer or assessor makes the final call.
- Store only PSP tokens and customer ids. Never store CVV after authorization.
- Disable request-body logging on payment routes, scrub error trackers, and keep payment pages free of third-party scripts you do not inventory. PCI DSS v4.x added explicit requirements for managing payment page scripts.

## Scaling and bottlenecks

| Load | What breaks first | Fix |
|---|---|---|
| 10× (12k/s peak) | Single primary write volume; hot ledger accounts; idempotency table bloat; PSP rate limits | Shard by `merchant_id` so phase 1 and 3 stay single-shard; sub-account hot accounts; partition keys by day and drop old partitions; more PSP merchant accounts |
| 100× | Cross-shard money movement; blast radius of one cluster | Cell architecture by merchant; cross-cell transfers as two ledger transactions through a clearing account; batch postings or a purpose-built ledger store |

Status reads go to replicas, except right after a write: read the primary (or wait for the version you wrote) so a client never sees its payment revert to `processing`. See [Partitioning and sharding]({{ '/backend/partitioning-and-sharding/' | relative_url }}).

## Reliability and failure modes

| Failure | Effect | Mitigation |
|---|---|---|
| PSP timeout after charging | Outcome unknown | `unknown` status, same-key retry, resolver, reconciliation |
| Crash between PSP success and commit | Attempt stuck `pending` | Resolver queries by reference; webhook also arrives |
| Client retries checkout | Duplicate request | Idempotency key plus unique `(merchant_id, order_id)` |
| Duplicate or reordered webhook | Replayed transition | Event-id dedupe; forward-only compare-and-set |
| PSP outage | Authorizations fail | Circuit breaker; route new intents to a secondary PSP; queue captures (authorizations stay valid for days) |
| Primary database loss | Writes stop | Synchronous standby (RPO 0), automated failover |
| Bug posts wrong amounts | Ledger wrong | Immutable ledger, reversing entries, reconciliation catches it |

## Trade-offs

| Decision | Chosen? | Why |
|---|---|---|
| PostgreSQL over a NoSQL store | Yes | Multi-row atomicity and constraints matter more than write scale at this volume |
| Double-entry ledger over balance columns | Yes | Auditable, self-checking, explains every cent |
| 2PC between our DB and the PSP | No | PSPs do not join your transactions; idempotency, state machine and reconciliation instead |
| Idempotency key at every hop | Yes | A single edge key does not cover PSP calls or webhooks |
| Hosted card fields | Yes | Shrinks PCI scope to almost nothing |
| Running balance row per account | Partly | Fine for sellers; hot platform accounts use sub-accounts or snapshots |
| Automatic PSP failover | Only for unsent attempts | Failing over a timed-out attempt risks a double charge |

## Interview follow-ups

**How do you guarantee a customer is never charged twice?** Unique `(merchant_id, order_id)` on intents, an idempotency key at the API edge, one attempt row written before each PSP call whose id is the PSP idempotency key, and no new attempt while an earlier one is `unknown`. Reconciliation catches whatever slips through.

**The PSP times out. What does the user see?** "Processing", not "failed". The intent stays `processing`, the resolver settles it within a minute or two, and the client polls or receives a push. Telling the user it failed invites a second attempt that can double charge.

**Why not just a `balance` column?** It loses history and cannot prove itself correct. Double entry gives an invariant (debits equal credits) you can check continuously, and every balance is explainable from entries.

**A webhook arrives before your confirm call returns. What happens?** Both paths go through the compare-and-set state machine. Whichever lands first moves the state; the second sees zero rows updated, re-reads, and returns the current status.

**How do you add a second PSP?** An adapter per PSP that normalizes statuses and errors, a routing layer that picks a PSP per attempt (cost, authorization rate, health), and a strict rule that failover only applies to attempts that never reached the first PSP.

**How do you stop concurrent partial refunds exceeding the capture?** A conditional update that reserves `refunded_minor` only if it stays at or below `captured_minor`, plus the table's check constraint as a backstop.

**How do you fix a historical ledger error?** Never edit entries. Post a correcting transaction that references the original, so the audit trail shows both the mistake and the fix.
