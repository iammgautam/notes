---
title: Design an online auction system
section: lld
order: 42
phase: "Phase 5 · Advanced"
summary: English auctions with minimum increments, scheduled-live-closed states, anti-sniping extensions, atomic bids, outbid notifications and winner settlement.
---

An online auction (eBay-style English auction) takes ascending bids until a closing time, then sells to the highest bidder. The hard parts are deciding which of two simultaneous bids wins, closing at the right moment, and settling when the winner doesn't pay.

## Requirements

Clarifying questions:
- Auction type? English (ascending, open). Not sealed-bid or Dutch.
- Minimum increment fixed per auction? Yes, plus a starting price and optional reserve price.
- Anti-sniping? Yes: a bid in the last N seconds extends the close to now + N.
- Proxy (auto) bidding? Out of scope for the core; discuss as an extension.
- What if the winner doesn't pay? Offer the item to the next-highest bidder.

Functional:
- Sellers list an item with start price, increment, start and end time, optional reserve.
- Auction moves SCHEDULED → LIVE → CLOSED on time.
- Users bid while LIVE; a bid must be at least start price, or highest + increment.
- Notify the previous leader when outbid; notify everyone on close.
- Settle: charge the winner; fall back to the next distinct bidder if payment fails.

Non-functional:
- Bids on one auction are strictly ordered; the highest valid bid wins even under contention.
- Bids are never accepted after the (possibly extended) close.
- Time is injectable so closing and extension are testable.

Out of scope: proxy bidding, search, shipping, fraud detection.

## Entities and relationships

```text
+------------------+ 1     * +------------------------+
| AuctionHouse     |-------->| Auction                |
| auctions {id: A} |         | item, seller           |
| create()         |         | start_price, increment |
| tick_all()       |         | reserve, snipe_window  |
+------------------+         | start_at, end_at       |
                             | status, bids, winner   |
                             | place_bid(), tick()    |
                             +---+---------------+----+
                                 | 1..*          | notifies
                          +------v-----+  +------v-------------------+
                          | Bid        |  | AuctionListener          |
                          | bidder     |  | on_outbid(a, user, bid)  |
                          | amount, at |  | on_closed(a, winner)     |
                          +------------+  +------^-------------------+
                                                 | is-a
                                     +-----------+-------------+
                                     | Notifier   | Settlement |
                                     +-------------------------+
AuctionStatus: SCHEDULED -> LIVE -> CLOSED
```

## Patterns and why

| Pattern | Where | Why |
|---|---|---|
| State machine | `Auction.status` driven by `tick()` | Bids are only valid while LIVE; closing happens exactly once |
| Observer | `AuctionListener` | Outbid emails, settlement and analytics plug in without touching bidding |
| Monitor (lock per auction) | `Auction._lock` | Check-highest-then-append is atomic; auctions don't block each other |
| Injected clock | `clock` callable | Deterministic tests for closing and anti-sniping |

## Code

```python
from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Callable, Protocol


class AuctionStatus(Enum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    CLOSED = "closed"


@dataclass(frozen=True)
class Bid:
    bidder: str
    amount: Decimal
    at: float


class BidRejected(Exception):
    pass


class AuctionListener(Protocol):
    def on_outbid(self, auction: Auction, user: str, new_bid: Bid) -> None: ...
    def on_closed(self, auction: Auction, winner: Bid | None) -> None: ...


class Auction:
    def __init__(self, id: int, item: str, seller: str, start_price: Decimal,
                 increment: Decimal, start_at: float, end_at: float,
                 reserve: Decimal | None = None, snipe_window: float = 60.0,
                 clock: Callable[[], float] = time.time):
        self.id, self.item, self.seller = id, item, seller
        self.start_price, self.increment, self.reserve = start_price, increment, reserve
        self.start_at, self.end_at, self.snipe_window = start_at, end_at, snipe_window
        self.clock = clock
        self.status = AuctionStatus.SCHEDULED
        self.bids: list[Bid] = []  # strictly increasing amounts
        self.winner: Bid | None = None
        self.listeners: list[AuctionListener] = []
        self._lock = threading.Lock()

    def min_next_bid(self) -> Decimal:
        return self.bids[-1].amount + self.increment if self.bids else self.start_price

    def tick(self) -> None:
        """Advance state by the clock. Called by a scheduler and before each bid."""
        with self._lock:
            now = self.clock()
            if self.status is AuctionStatus.SCHEDULED and now >= self.start_at:
                self.status = AuctionStatus.LIVE
            closing = self.status is AuctionStatus.LIVE and now >= self.end_at
            if closing:
                self.status = AuctionStatus.CLOSED
                top = self.bids[-1] if self.bids else None
                if top and (self.reserve is None or top.amount >= self.reserve):
                    self.winner = top
        if closing:  # notify outside the lock: slow listeners can't block bids
            for listener in self.listeners:
                listener.on_closed(self, self.winner)

    def place_bid(self, bidder: str, amount: Decimal) -> Bid:
        self.tick()
        with self._lock:
            now = self.clock()
            if self.status is not AuctionStatus.LIVE or now >= self.end_at:
                raise BidRejected("auction is not live")
            if bidder == self.seller:
                raise BidRejected("seller cannot bid")
            if amount < self.min_next_bid():
                raise BidRejected(f"bid must be at least {self.min_next_bid()}")
            previous = self.bids[-1] if self.bids else None
            bid = Bid(bidder, amount, now)
            self.bids.append(bid)
            if self.end_at - now < self.snipe_window:  # anti-sniping
                self.end_at = now + self.snipe_window
        if previous and previous.bidder != bidder:
            for listener in self.listeners:
                listener.on_outbid(self, previous.bidder, bid)
        return bid


class AuctionHouse:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.auctions: dict[int, Auction] = {}
        self._ids = itertools.count(1)

    def create(self, item, seller, start_price, increment, start_at, end_at, **kw) -> Auction:
        if end_at <= start_at or start_price <= 0 or increment <= 0:
            raise ValueError("bad auction parameters")
        a = Auction(next(self._ids), item, seller, start_price, increment,
                    start_at, end_at, clock=self.clock, **kw)
        self.auctions[a.id] = a
        return a

    def tick_all(self) -> None:  # Simplification: scan all; use a heap keyed by next deadline at scale
        for a in list(self.auctions.values()):
            a.tick()


class Notifier:
    def __init__(self):
        self.sent: list[str] = []

    def on_outbid(self, auction, user, new_bid):
        self.sent.append(f"{user}: outbid at {new_bid.amount}")

    def on_closed(self, auction, winner):
        self.sent.append(f"closed: {winner.bidder if winner else 'unsold'}")


class Settlement:
    """Charge the winner; if that fails, offer it to the next distinct bidder."""

    def __init__(self, charge: Callable[[str, Decimal], bool]):
        self.charge = charge
        self.sold: dict[int, tuple[str, Decimal]] = {}

    def on_outbid(self, auction, user, new_bid):
        pass

    def on_closed(self, auction, winner):
        if winner is None:
            return
        tried = set()
        for bid in reversed(auction.bids):
            if bid.bidder in tried or (auction.reserve and bid.amount < auction.reserve):
                continue
            tried.add(bid.bidder)
            if self.charge(bid.bidder, bid.amount):
                self.sold[auction.id] = (bid.bidder, bid.amount)
                return


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


if __name__ == "__main__":
    clock = FakeClock(0)
    house = AuctionHouse(clock)
    notes = Notifier()
    settle = Settlement(charge=lambda user, amt: user != "bob")  # bob's card fails
    a = house.create("guitar", "sam", Decimal("100"), Decimal("10"),
                     start_at=10, end_at=1000, reserve=Decimal("120"))
    a.listeners += [notes, settle]

    try:
        a.place_bid("ann", Decimal("100"))
        raise AssertionError("bid before start")
    except BidRejected:
        pass
    clock.now = 20
    a.place_bid("ann", Decimal("100"))
    for bad in (("bob", Decimal("105")), ("sam", Decimal("500"))):
        try:
            a.place_bid(*bad)  # below increment; seller bidding
            raise AssertionError(bad)
        except BidRejected:
            pass
    a.place_bid("cat", Decimal("130"))
    assert notes.sent == ["ann: outbid at 130"]

    clock.now = 970  # 30 s before close: extension kicks in
    a.place_bid("bob", Decimal("140"))
    assert a.end_at == 1030
    clock.now = 1010
    house.tick_all()
    assert a.status is AuctionStatus.LIVE  # still open thanks to extension

    # stress: 16 threads bid concurrently; amounts must stay strictly ordered
    def bidder(i: int) -> None:
        for _ in range(200):
            try:
                a.place_bid(f"u{i}", a.min_next_bid())
            except BidRejected:
                pass  # lost the race to a higher bid; real clients re-read and retry

    threads = [threading.Thread(target=bidder, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    amounts = [b.amount for b in a.bids]
    assert all(y - x >= a.increment for x, y in zip(amounts, amounts[1:]))

    clock.now = 2000
    house.tick_all()
    house.tick_all()  # closing happens once
    assert a.status is AuctionStatus.CLOSED and a.winner == a.bids[-1]
    assert notes.sent.count(f"closed: {a.winner.bidder}") == 1
    assert settle.sold[a.id][0] == a.winner.bidder  # winner isn't bob, so paid
    try:
        a.place_bid("ann", Decimal("99999"))
        raise AssertionError("bid after close")
    except BidRejected:
        pass

    # second chance: the winner's payment fails, next distinct bidder pays
    b = house.create("lamp", "sam", Decimal("10"), Decimal("1"), start_at=2000, end_at=2100)
    b.listeners.append(settle)
    b.place_bid("cat", Decimal("10"))
    b.place_bid("bob", Decimal("15"))
    clock.now = 2200
    house.tick_all()
    assert b.winner.bidder == "bob" and settle.sold[b.id] == ("cat", Decimal("10"))
    print("ok")
```

## Key flows

Two bids race near the close:

```text
Bidder A        Bidder B        Auction                      Listeners
   | bid 150       |               |                             |
   |------------------------------>| tick(); lock                |
   |               | bid 150 ----->| B blocks on the lock        |
   |               |               | LIVE, 150 >= 140+10: append |
   |               |               | 20 s left: end = now + 60   |
   |<--------- accepted -----------| unlock                      |
   |               |               | on_outbid(old leader) ----->|
   |               |               | B locks: 150 < 150+10       |
   |               |<- rejected ---| unlock                      |
```

Close: scheduler calls `tick_all()` → each `tick()` takes the lock → LIVE and `now >= end_at` → CLOSED, winner chosen against the reserve → `on_closed` outside the lock → `Settlement` charges the winner, falling back down the bid list.

## Edge cases

- Two bids at the same amount: the first to take the lock wins; the second fails the increment check.
- Leader raising their own bid: allowed, no outbid notification to themselves.
- Bid arrives just after `end_at` but before the scheduler ticked: `place_bid` ticks first and also checks `now >= end_at` under the lock.
- Reserve not met: auction closes unsold (`winner is None`).
- No bids: closes unsold; seller can relist.
- Anti-sniping loop: each late bid extends by the window; cap total extensions if the product wants a hard stop.

## Production follow-ups

**Thread safety / concurrency?** Each auction has its own lock, so "read highest, validate, append" is atomic and auctions never contend with each other. Listeners run after the lock is released so a slow email call can't delay bids. In a database the same guarantee is a conditional update: `UPDATE auctions SET high_bid=%s, high_bidder=%s WHERE id=%s AND high_bid <= %s - increment AND status='live' AND end_at > now()`, then insert the bid if one row changed.

**Persistence and restart?** Store auctions and bids in PostgreSQL; the bid insert and the auction's high-bid update happen in one transaction. Deadlines live in the row, so after a restart the scheduler simply queries `status='live' AND end_at <= now()` and closes them. Settlement runs from an outbox so a crash between close and charge retries the charge.

**Idempotency and retries?** Bids carry a client bid id; a retried request returns the stored result instead of placing a second bid. Closing is idempotent because `tick()` only acts on LIVE. Charges use an idempotency key like `auction:<id>:bidder:<user>` so a retried settlement never double-charges.

**Extensibility?** Proxy bidding: store each user's max and, on every new bid, auto-raise the leader's proxy up to their max (a new component reacting to bids). Other auction formats (Dutch, sealed-bid) become different `Auction` subclasses sharing the listener contract.

**Scale (10×, across machines)?** A hot auction's last minute concentrates traffic on one row. Route all bids for an auction to one owner (consistent hashing on auction id) that serializes them in memory and writes through, or use Redis with a Lua script for the compare-and-set. Fan out outbid notifications and live price updates through pub-sub and WebSockets.

**Observability?** Bid latency and rejection reasons (too low, closed), extension counts per auction, lag between `end_at` and actual close, settlement failures and second-chance rate. Log every accepted bid with server timestamp for dispute resolution.

**Clock skew?** Only the server's clock decides validity; client timestamps are display-only. With several servers, either route an auction to one owner or let the database's `now()` be the single clock in the conditional update.

**Money precision?** Amounts are `Decimal` and increments are compared exactly, so 0.1 + 0.2 issues never let a too-small bid through.

## Interview follow-ups

- **Why extend instead of a hard close?** Sniping in the last second denies others a chance to respond; extension turns the end into "no bids for N seconds", which tends toward the true market price.
- **How do you close auctions on time?** A scheduler with a min-heap of deadlines (or a DB query on an indexed `end_at`), plus a lazy check in `place_bid` so a late tick can't accept a late bid.
- **Highest bid wins atomically, how?** Validation and append happen under one lock (or one conditional update), so no second bid can slip in between reading the high bid and writing a new one.
- **What if the winner never pays?** Second-chance offer to the next distinct bidder at their own bid, as `Settlement` does, after a payment window.
