---
title: Design a payment gateway
section: lld
order: 45
phase: "Phase 5 · Advanced"
summary: Payment intents with a strict state machine, method strategies, provider adapters, idempotency keys, retries with backoff and signed merchant webhooks.
---

A payment gateway (Stripe, Razorpay, Adyen) sits between merchants and card networks or banks. Every design choice serves money safety: never charge twice, never lose a charge, always tell the merchant.

## Requirements

Clarifying questions:
- Auth and capture as separate steps? Yes: authorize at checkout, capture on shipment.
- Which methods and providers? Cards and UPI, behind one or more provider SDKs.
- Client retries and provider timeouts? Idempotency keys on our API and on provider calls.

Functional:
- Create a payment intent (amount, currency, method) with an idempotency key.
- Confirm (authorize), capture and refund through the right provider.
- States: CREATED → AUTHORIZED → CAPTURED → REFUNDED; CREATED or AUTHORIZED → FAILED.
- A signed webhook per transition.

Non-functional:
- No double charge under retries, timeouts or concurrency.
- Exact money: `Decimal`, checked against the currency's minor units.
- Webhook failures never block payments.

Out of scope: partial capture/refund, 3-D Secure, payouts, FX, disputes, fraud.

## Entities and relationships

```text
+---------------------+ uses +-----------------+ is-a +-------------+
| PaymentGateway      |----->| PaymentMethod   |<-----| Card, Upi   |
| intents, providers  |      | validate()      |      +-------------+
| observers           | uses +-----------------+ is-a +-------------+
| create_intent(key)  |----->| PaymentProvider |<-----| AcmeAdapter |
| confirm()           |      | authorize()     |      | (AcmeSdk)   |
| capture(key)        |      | capture()       |      +-------------+
| refund(key)         |      | refund()        |
+----+-----------+----+      +-----------------+
     | 1..*      |
     |           +-- notifies --+
+----v-------------+  +---------v---------+ 1   * +-----------+
| PaymentIntent    |  | WebhookDispatcher |------>| Merchant  |
| amount, currency |  | queue, worker     |       | secret    |
| method, status   |  | dead_letters      |       | endpoint  |
| provider_ref     |  +-------------------+       +-----------+
+------------------+
```

## Patterns and why

| Pattern | Where | Why |
|---|---|---|
| Strategy | `PaymentMethod` (`Card`, `Upi`) | Per-method validation and routing, no `if` chains |
| Adapter | `AcmeAdapter` over `AcmeSdk` | Vendor quirks (minor units, dict replies) stay behind one port |
| State machine | `ALLOWED` + `move_to` | No capture before auth, no refund of a failed payment |
| Observer | `WebhookDispatcher` | Webhooks, ledger, analytics plug in without gateway changes |
| Idempotency key, retry | `_once`, `with_retries` | Retries replay the first result; backoff avoids retry storms |

## Code

```python
from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import queue
import random
import re
import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Callable

MINOR_UNITS = {"USD": 2, "INR": 2, "JPY": 0, "KWD": 3}


def to_minor(amount: Decimal, currency: str) -> int:
    if currency not in MINOR_UNITS:
        raise ValueError(f"unsupported currency {currency}")
    minor = amount.scaleb(MINOR_UNITS[currency])
    if amount <= 0 or minor != minor.to_integral_value():
        raise ValueError(f"invalid amount {amount} {currency}")
    return int(minor)


class Status(Enum):
    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"


ALLOWED = {
    Status.CREATED: {Status.AUTHORIZED, Status.FAILED},
    Status.AUTHORIZED: {Status.CAPTURED, Status.FAILED},
    Status.CAPTURED: {Status.REFUNDED},
}


class InvalidTransition(Exception): ...
class IdempotencyError(Exception): ...
class ProviderTimeout(Exception): ...
class Declined(Exception): ...


class PaymentMethod(ABC):
    provider = "acme"  # which adapter handles this method

    @abstractmethod
    def validate(self) -> None: ...


@dataclass(frozen=True)
class Card(PaymentMethod):
    number: str = field(repr=False)  # in production: a vault token, never the raw PAN

    def validate(self) -> None:  # Luhn checksum
        d = [int(c) for c in self.number] if self.number.isdigit() else []
        total = sum(d[-1::-2]) + sum(sum(divmod(2 * x, 10)) for x in d[-2::-2])
        if len(d) < 12 or total % 10:
            raise ValueError("invalid card number")


@dataclass(frozen=True)
class Upi(PaymentMethod):
    vpa: str

    def validate(self) -> None:
        if not re.fullmatch(r"[\w.-]+@[a-z]+", self.vpa):
            raise ValueError("invalid UPI id")


@dataclass(eq=False)
class PaymentIntent:
    id: str
    merchant_id: str
    amount: Decimal
    currency: str
    method: PaymentMethod
    status: Status = Status.CREATED
    provider_ref: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def move_to(self, new: Status) -> None:
        if new not in ALLOWED.get(self.status, set()):
            raise InvalidTransition(f"{self.status.value} -> {new.value}")
        self.status = new


class PaymentProvider(ABC):  # each op returns a provider ref, or None if declined
    @abstractmethod
    def authorize(self, intent: PaymentIntent, key: str) -> str | None: ...
    @abstractmethod
    def capture(self, intent: PaymentIntent, key: str) -> str | None: ...
    @abstractmethod
    def refund(self, intent: PaymentIntent, key: str) -> str | None: ...


class AcmeSdk:  # fake vendor SDK: minor units, dict replies, dedupes by request id
    def __init__(self, lost_replies: int = 0):
        self.lost_replies, self.done = lost_replies, {}
        self._lock = threading.Lock()

    def call(self, op: str, amount_minor: int, currency: str, request_id: str) -> dict:
        with self._lock:
            if request_id not in self.done:
                ok = amount_minor <= 500_000
                self.done[request_id] = {"status": "succeeded" if ok else "declined",
                                         "id": f"acme_{len(self.done)}"}
            if self.lost_replies:  # processed, but the reply is lost on the wire
                self.lost_replies -= 1
                raise TimeoutError(op)
            return self.done[request_id]


class AcmeAdapter(PaymentProvider):
    def __init__(self, sdk: AcmeSdk):
        self.sdk = sdk

    def _call(self, op: str, intent: PaymentIntent, key: str) -> str | None:
        try:
            reply = self.sdk.call(op, to_minor(intent.amount, intent.currency),
                                  intent.currency, key)
        except TimeoutError as e:
            raise ProviderTimeout(op) from e
        return reply["id"] if reply["status"] == "succeeded" else None

    def authorize(self, intent, key): return self._call("auth", intent, key)
    def capture(self, intent, key): return self._call("capture", intent, key)
    def refund(self, intent, key): return self._call("refund", intent, key)


def with_retries(fn, attempts: int = 4, base: float = 0.01):
    for i in range(attempts):
        try:
            return fn()
        except ProviderTimeout:
            if i == attempts - 1:
                raise  # outcome unknown: reconcile later
            time.sleep(base * 2**i + random.uniform(0, base))  # backoff + jitter


_IN_FLIGHT = object()


class PaymentGateway:
    def __init__(self, providers: dict[str, PaymentProvider]):
        self.providers = providers
        self.intents: dict[str, PaymentIntent] = {}
        self.observers: list[Callable[[str, dict], None]] = []
        self._idem: dict[tuple[str, str], tuple[tuple, object]] = {}
        self._lock = threading.Lock()  # guards _idem only; held briefly
        self._ids = itertools.count(1)

    def create_intent(self, merchant_id, amount: Decimal, currency, method, key):
        def create():
            to_minor(amount, currency)
            method.validate()
            intent = PaymentIntent(f"pi_{next(self._ids)}", merchant_id, amount, currency, method)
            self.intents[intent.id] = intent
            return intent
        return self._once(merchant_id, key, ("create", amount, currency, method), create)

    def confirm(self, intent_id: str) -> PaymentIntent:
        return self._step(self.intents[intent_id], "authorize",
                          Status.CREATED, Status.AUTHORIZED, Status.FAILED)

    def capture(self, intent_id: str, key: str) -> PaymentIntent:
        intent = self.intents[intent_id]
        return self._once(intent.merchant_id, key, ("capture", intent_id), lambda: self._step(
            intent, "capture", Status.AUTHORIZED, Status.CAPTURED, Status.FAILED))

    def refund(self, intent_id: str, key: str) -> PaymentIntent:
        intent = self.intents[intent_id]
        return self._once(intent.merchant_id, key, ("refund", intent_id), lambda: self._step(
            intent, "refund", Status.CAPTURED, Status.REFUNDED, None))

    def _step(self, intent, op, need, done, on_decline) -> PaymentIntent:
        with intent.lock:  # held across the provider call; only this intent waits
            if intent.status is done:
                return intent  # repeat call: no-op
            if intent.status is not need:
                raise InvalidTransition(f"{op} needs {need.value}, is {intent.status.value}")
            provider = self.providers[intent.method.provider]
            # derived key: even a retry after our restart is deduped by the provider
            ref = with_retries(lambda: getattr(provider, op)(intent, f"{intent.id}:{op}"))
            if ref is None and on_decline is None:
                raise Declined(op)
            intent.move_to(done if ref else on_decline)
            intent.provider_ref = intent.provider_ref or ref
            event = {"id": intent.id, "status": intent.status.value,
                     "amount": str(intent.amount), "currency": intent.currency}
        for observer in self.observers:  # outside the lock
            observer(intent.merchant_id, event)
        return intent

    def _once(self, merchant_id: str, key: str, request: tuple, fn):
        slot = (merchant_id, key)
        with self._lock:
            if slot in self._idem:
                seen_request, result = self._idem[slot]
                if seen_request != request:
                    raise IdempotencyError("key reused with a different request")
                if result is _IN_FLIGHT:
                    raise IdempotencyError("request with this key is in progress")
                return result
            self._idem[slot] = (request, _IN_FLIGHT)
        try:
            result = fn()
        except Exception:
            with self._lock:
                del self._idem[slot]  # nothing stored: the client may retry
            raise
        with self._lock:
            self._idem[slot] = (request, result)
        return result


@dataclass
class Merchant:
    id: str
    secret: bytes
    endpoint: Callable[[bytes, str], bool]  # stands in for HTTPS POST; True = 2xx


class WebhookDispatcher:
    """Observer: queue each event; one worker delivers it with retries."""

    def __init__(self, merchants: dict[str, Merchant], attempts=5, base=0.01):
        self.merchants, self.attempts, self.base = merchants, attempts, base
        self.queue: queue.Queue[tuple[str, bytes]] = queue.Queue()
        self.dead_letters: list[tuple[str, bytes]] = []
        threading.Thread(target=self._worker, daemon=True).start()

    def __call__(self, merchant_id: str, event: dict) -> None:
        self.queue.put((merchant_id, json.dumps(event).encode()))

    def _worker(self) -> None:
        # Simplification: one worker retrying inline; a dead endpoint delays others.
        # At scale: a retry table with next_attempt_at and a worker pool.
        while True:
            merchant_id, body = self.queue.get()
            m = self.merchants[merchant_id]
            sig = hmac.new(m.secret, body, hashlib.sha256).hexdigest()
            for i in range(self.attempts):
                try:
                    if m.endpoint(body, sig):
                        break
                except Exception:
                    pass  # network error counts as a failed attempt
                time.sleep(self.base * 2**i)
            else:
                self.dead_letters.append((merchant_id, body))
            self.queue.task_done()


def raises(fn, exc=Exception) -> bool:
    try:
        fn()
    except exc:
        return True
    return False


def run_threads(fn, n: int) -> list:
    with ThreadPoolExecutor(n) as pool:  # Java: ExecutorService
        return list(pool.map(fn, range(n)))


if __name__ == "__main__":
    delivered, attempts = [], itertools.count()

    def endpoint(body: bytes, sig: str) -> bool:
        if next(attempts) < 2:
            return False  # merchant is down for the first two attempts
        assert hmac.compare_digest(sig, hmac.new(b"whsec", body, hashlib.sha256).hexdigest())
        delivered.append(json.loads(body)["status"])
        return True

    sdk = AcmeSdk(lost_replies=2)
    gw = PaymentGateway({"acme": AcmeAdapter(sdk)})
    hooks = WebhookDispatcher({"m1": Merchant("m1", b"whsec", endpoint)})
    gw.observers.append(hooks)
    card = Card("4242424242424242")

    def pay(amount: str, key: str, method=card, currency="USD") -> PaymentIntent:
        return gw.create_intent("m1", Decimal(amount), currency, method, key)

    results = []  # 20 concurrent creates, one key
    run_threads(lambda _: raises(lambda: results.append(pay("49.99", "order-7"))), 20)
    assert len(gw.intents) == 1 and all(r is results[0] for r in results)
    intent = results[0]
    assert pay("49.990", "order-7") is intent  # replay returns the same intent
    assert raises(lambda: pay("50", "order-7"), IdempotencyError)  # same key, new body

    gw.confirm(intent.id)  # two lost replies, retried with the same provider key
    assert intent.status is Status.AUTHORIZED
    assert sum(k.endswith(":authorize") for k in sdk.done) == 1

    run_threads(lambda i: gw.capture(intent.id, f"cap-{i}"), 10)  # 10 racing captures
    assert intent.status is Status.CAPTURED
    assert sum(k.endswith(":capture") for k in sdk.done) == 1  # charged once

    gw.refund(intent.id, "ref-1")
    assert gw.capture(intent.id, "cap-3") is intent  # replay after refund: no error
    assert intent.status is Status.REFUNDED

    assert raises(lambda: pay("10.5", "k2", card, "JPY"), ValueError)  # JPY has no cents
    assert raises(lambda: pay("10", "k3", Card("4242424242424241")), ValueError)  # Luhn

    big = pay("9000.00", "k5", Upi("ann@okbank"))
    gw.confirm(big.id)  # provider declines
    assert big.status is Status.FAILED

    hooks.queue.join()
    assert delivered == ["authorized", "captured", "refunded", "failed"]
    assert not hooks.dead_letters
    print("ok")
```

## Key flows

Confirm with a lost provider reply:

```text
Merchant     Gateway                            AcmeAdapter/SDK
  | confirm     |                                     |
  |------------>| lock intent                         |
  |             | authorize(key="pi_1:authorize") --->| processed,
  |             |<------------------ ProviderTimeout -| reply lost
  |             | sleep 10 ms + jitter                |
  |             | authorize(same key) --------------->| key seen:
  |             |<-------------------------- acme_0 --| same reply
  |             | CREATED -> AUTHORIZED, unlock       |
  |             | emit event -> webhook queue         |
  |<- intent ---|                                     |
```

Capture: `_once` claims the key → `_step` locks the intent → return early if CAPTURED, else call the provider with `pi_1:capture` → store the result under the key.

## Edge cases

- Same key, different body: `IdempotencyError`. Same key still running: "in progress".
- Provider times out on every attempt: state unchanged, error raised. Never assume failure; reconciliation decides.
- Refund declined: stays CAPTURED, `Declined` raised.
- 10.5 JPY or a non-positive amount: rejected at create.
- Webhook endpoint down past the retry budget: `dead_letters`, replayed later.

## Production follow-ups

**Thread safety / concurrency?** A short gateway lock guards the idempotency table; a per-intent lock, held across the provider call, serializes capture/refund per intent. Across nodes: claim keys with `INSERT ... ON CONFLICT DO NOTHING` on `(merchant_id, key)`, and transition intents with a conditional `UPDATE ... WHERE status='authorized'`.

**Persistence and restart?** Intents, idempotency records and an event outbox live in PostgreSQL; a transition and its outbox row commit together. If the process dies mid-call, a recovery job re-sends the same derived provider key to learn the outcome.

**Idempotency and retries?** Two layers. Client to gateway: the merchant's key plus a request fingerprint, kept 24 hours. Gateway to provider: a key derived from intent id and operation. Retry only timeouts and 5xx, never declines, with exponential backoff, jitter and a cap.

**Extensibility?** New provider = new adapter in `providers`; new method (wallet, bank transfer) = new `PaymentMethod`. Smarter routing (by currency, cost, success rate) is a strategy that leaves `_step` alone.

**Scale (10×, across machines)?** Stateless API nodes; shard intents and keys by merchant id. A circuit breaker per provider fails over quickly. Webhooks move to a worker pool reading a retry table (`next_attempt_at`) so one dead endpoint doesn't slow everyone.

**Observability?** Auth success rate by provider, method and card BIN; provider p99 latency and timeout rate; retries; idempotency conflicts; webhook lag and dead letters. Log intent and merchant ids, never card numbers or secrets.

**Reconciliation?** Daily, match the provider's settlement file to your ledger by provider ref. Classify: matched; captured there but not here (an ambiguous timeout: fix state, notify the merchant); captured here but missing there (investigate or void); amount mismatch. Exceptions go to an ops queue. It is the safety net for every unknown outcome.

**Money precision?** `Decimal` everywhere, integer minor units only at the provider boundary, with a per-currency exponent. Reject extra precision; never round silently.

**Webhook security?** Sign body plus timestamp with HMAC-SHA256; merchants verify with a constant-time compare and reject stale timestamps.

## Interview follow-ups

- **The provider timed out on capture. Did it charge?** Unknown. Retry with the same provider key to get the real answer; if retries run out, keep the intent AUTHORIZED, flag it, and let reconciliation settle it.
- **Isn't the state check enough for capture?** It stops a double charge, but only the key lets a replay after a later transition (capture after refund) return the original answer instead of an error.
