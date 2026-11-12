---
title: Design a movie ticket booking system
section: lld
order: 32
phase: "Phase 5 · Intermediate"
summary: BookMyShow-style booking with per-show seat maps, seat holds that expire, a payment step, pricing strategies, and no double booking under load.
---

## Requirements

Clarifying questions worth asking:

- Can a user pick specific seats, or do we auto-assign? (Pick specific seats.)
- How long is a seat held while the user pays? (A few minutes, configurable.)
- Is a multi-seat selection all-or-nothing? (Yes. Half a booking is useless.)
- Does price vary by seat category, time or day? (Category, plus a weekend surcharge.)
- If payment succeeds after the hold expired, what happens? (Confirm if nobody else took the seats, otherwise refund.)

Functional:

- Browse shows for a movie in a city (city → theatre → screen → show).
- View a show's seat map with what is still free.
- Hold seats, pay, confirm. Holds expire if payment does not finish.

Non-functional:

- **No double booking**, even when hundreds of users click the same seat on release day.
- Never hold a lock while waiting on the payment gateway.
- A double-clicked "pay" charges once.

Out of scope: cancellation and refund policy (see [hotel booking]({{ '/lld/hotel-booking/' | relative_url }})), search ranking, notifications.

## Entities and relationships

```text
+-----------+ has * +-----------+ has * +-------------------+
| Theatre   |------>| Screen    |------>| Seat              |
| id, city  |       | id        |       | id, category      |
+-----------+       +-----------+       +-------------------+
      ^                   ^                        ^
      | uses              | uses                   | wraps 1
+-----+-------------------+---+  has *  +----------+--------+
| Show                        |-------->| ShowSeat          |
| id, movie, starts_at        |         | status, holder    |
| lock: Lock (one per show)   |         | hold_until        |
+-----------------------------+         | version           |
      ^ refers to                       +-------------------+
      |
+-----+-----------------------+  uses   +-------------------+
| BookingService              |-------->| <<abstract>>      |
| hold(), pay(), find_shows() |         | PricingStrategy   |
| bookings: {id: Booking}     |         | price(show, seat) |
+-----------------------------+         +-------------------+
      | uses             | creates        ^ is-a
      v                  v                CategoryPricing
+----------------+  +----------------+    WeekendSurcharge (wraps one)
| PaymentGateway |  | Booking        |
| <<protocol>>   |  | id, user, show |
| charge/refund  |  | seats, amount  |
+----------------+  | status         |
                    +----------------+
```

> **Gotcha:** Seat status belongs to `(show, seat)`, not to `Seat`. The same physical seat is free at 6 pm and booked at 9 pm, so `ShowSeat` holds the status.

## Patterns and why

| Pattern | Where | Why (what it prevents) |
|---|---|---|
| Strategy | `PricingStrategy` | Price rules hard-coded in `hold()`, edited for every promotion |
| Decorator | `WeekendSurcharge` wraps another strategy | A subclass per combination (gold-weekend, silver-weekend) |
| State | `SeatStatus`, `BookingStatus` | Illegal moves such as BOOKED back to HELD |
| Protocol (port) | `PaymentGateway` | Booking logic tied to one payment provider's SDK |

## Code

```python
from __future__ import annotations

import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Callable, Protocol

CENT = Decimal("0.01")


class SeatStatus(Enum):
    AVAILABLE = "available"
    HELD = "held"
    BOOKED = "booked"


class BookingStatus(Enum):
    PENDING = "pending"       # seats held, waiting for payment
    PAYING = "paying"         # charge in flight; blocks a second pay() call
    CONFIRMED = "confirmed"
    FAILED = "failed"         # payment declined or seats lost


class SeatUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Seat:
    id: str
    category: str


@dataclass
class Screen:
    id: str
    seats: list[Seat]


@dataclass
class Theatre:
    id: str
    city: str
    screens: list[Screen]


@dataclass
class ShowSeat:
    seat: Seat
    status: SeatStatus = SeatStatus.AVAILABLE
    holder: str | None = None          # booking id
    hold_until: float = 0.0
    version: int = 0                   # bumped on every change (DB optimistic lock)


@dataclass
class Show:
    id: str
    movie: str
    theatre: Theatre
    screen: Screen
    starts_at: datetime
    seats: dict[str, ShowSeat] = field(init=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.seats = {s.id: ShowSeat(s) for s in self.screen.seats}


class PricingStrategy(ABC):
    @abstractmethod
    def price(self, show: Show, seat: Seat) -> Decimal: ...


class CategoryPricing(PricingStrategy):
    def __init__(self, base: dict[str, Decimal]) -> None:
        self.base = base

    def price(self, show, seat):
        return self.base[seat.category]


class WeekendSurcharge(PricingStrategy):
    def __init__(self, inner: PricingStrategy, factor: Decimal) -> None:
        self.inner, self.factor = inner, factor

    def price(self, show, seat):
        p = self.inner.price(show, seat)
        return (p * self.factor).quantize(CENT) if show.starts_at.weekday() >= 5 else p


class PaymentGateway(Protocol):
    def charge(self, idempotency_key: str, amount: Decimal) -> bool: ...
    def refund(self, idempotency_key: str, amount: Decimal) -> None: ...


@dataclass
class Booking:
    id: str
    user_id: str
    show: Show
    seat_ids: tuple[str, ...]
    amount: Decimal
    status: BookingStatus = BookingStatus.PENDING


class BookingService:
    def __init__(self, pricing: PricingStrategy, payments: PaymentGateway,
                 hold_seconds: float = 300,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.pricing, self.payments = pricing, payments
        self.hold_seconds, self.clock = hold_seconds, clock
        self.shows: dict[str, Show] = {}
        self.bookings: dict[str, Booking] = {}

    def add_show(self, show: Show) -> None:
        self.shows[show.id] = show

    def find_shows(self, city: str, movie: str) -> list[Show]:
        return [s for s in self.shows.values()
                if s.theatre.city == city and s.movie == movie]

    def _free(self, ss: ShowSeat) -> bool:
        return ss.status is SeatStatus.AVAILABLE or (
            ss.status is SeatStatus.HELD and ss.hold_until <= self.clock())

    def available(self, show_id: str) -> list[str]:
        show = self.shows[show_id]
        with show.lock:
            return [sid for sid, ss in show.seats.items() if self._free(ss)]

    def hold(self, user_id: str, show_id: str, seat_ids: list[str]) -> Booking:
        show, booking_id = self.shows[show_id], uuid.uuid4().hex
        with show.lock:                                  # all-or-nothing
            seats = [show.seats[s] for s in seat_ids]
            if not all(self._free(ss) for ss in seats):
                raise SeatUnavailable(seat_ids)
            until = self.clock() + self.hold_seconds
            for ss in seats:
                ss.status, ss.holder, ss.hold_until = SeatStatus.HELD, booking_id, until
                ss.version += 1
        amount = sum((self.pricing.price(show, ss.seat) for ss in seats), Decimal(0))
        booking = Booking(booking_id, user_id, show, tuple(seat_ids), amount)
        self.bookings[booking_id] = booking
        return booking

    def pay(self, booking_id: str) -> Booking:
        booking = self.bookings[booking_id]
        show = booking.show
        with show.lock:
            if booking.status is not BookingStatus.PENDING:
                return booking                           # double click: no-op
            booking.status = BookingStatus.PAYING
        paid = self.payments.charge(booking_id, booking.amount)   # no lock held
        with show.lock:
            seats = [show.seats[s] for s in booking.seat_ids]
            # An expired hold is only lost if someone else claimed the seat.
            still_ours = all(ss.holder == booking_id and
                             ss.status is SeatStatus.HELD for ss in seats)
            for ss in seats:
                if ss.holder == booking_id and ss.status is SeatStatus.HELD:
                    if paid and still_ours:
                        ss.status = SeatStatus.BOOKED
                    else:
                        ss.status, ss.holder = SeatStatus.AVAILABLE, None
                    ss.version += 1
            ok = paid and still_ours
            booking.status = BookingStatus.CONFIRMED if ok else BookingStatus.FAILED
        if paid and not ok:
            self.payments.refund(booking_id, booking.amount)
        return booking


if __name__ == "__main__":
    import random
    from concurrent.futures import ThreadPoolExecutor

    class FakeGateway:
        def __init__(self) -> None:
            self.charged, self.refunded, self.decline = {}, set(), set()

        def charge(self, key, amount):
            if key in self.decline:
                return False
            self.charged.setdefault(key, amount)         # idempotent by key
            return True

        def refund(self, key, amount):
            self.refunded.add(key)

    now = [0.0]
    gw = FakeGateway()
    pricing = WeekendSurcharge(CategoryPricing(
        {"gold": Decimal("250"), "silver": Decimal("150")}), Decimal("1.2"))
    svc = BookingService(pricing, gw, hold_seconds=300, clock=lambda: now[0])
    seats = [Seat(f"A{i}", "gold" if i < 5 else "silver") for i in range(20)]
    pvr = Theatre("pvr", "Pune", [Screen("s1", seats)])
    svc.add_show(Show("sat", "Dune", pvr, pvr.screens[0], datetime(2026, 11, 14, 18)))
    assert [s.id for s in svc.find_shows("Pune", "Dune")] == ["sat"]

    b1 = svc.hold("u1", "sat", ["A1", "A6"])
    assert b1.amount == Decimal("480.00")                # (250 + 150) * 1.2
    try:
        svc.hold("u2", "sat", ["A6", "A7"])
        raise AssertionError("double hold")
    except SeatUnavailable:
        pass
    assert svc.pay(b1.id).status is BookingStatus.CONFIRMED
    assert svc.pay(b1.id).status is BookingStatus.CONFIRMED and len(gw.charged) == 1

    b2 = svc.hold("u2", "sat", ["A2"])
    now[0] += 301                                        # u2's hold expires
    b3 = svc.hold("u3", "sat", ["A2"])                   # u3 claims the seat
    assert svc.pay(b2.id).status is BookingStatus.FAILED and b2.id in gw.refunded
    assert svc.pay(b3.id).status is BookingStatus.CONFIRMED

    b4 = svc.hold("u4", "sat", ["A3"])                   # declined card frees seats
    gw.decline.add(b4.id)
    assert svc.pay(b4.id).status is BookingStatus.FAILED and "A3" in svc.available("sat")

    # Stress: 200 users grab random overlapping pairs; no seat sold twice.
    pool_seats = [f"A{n}" for n in range(8, 20)]

    def attempt(i: int) -> Booking | None:
        pair = random.sample(pool_seats, 2)
        try:
            return svc.pay(svc.hold(f"user{i}", "sat", pair).id)
        except SeatUnavailable:
            return None

    with ThreadPoolExecutor(16) as pool:
        won = [b for b in pool.map(attempt, range(200)) if b]
    sold = [s for b in won if b.status is BookingStatus.CONFIRMED for s in b.seat_ids]
    assert len(sold) == len(set(sold))
    seat_map = svc.shows["sat"].seats
    assert {s for s in pool_seats if seat_map[s].status is SeatStatus.BOOKED} == set(sold)
    print("movie booking ok")
```

The in-memory lock is the interview answer. In PostgreSQL the same "hold" is an optimistic update: the seat map the user saw carried each seat's `version`, and the hold succeeds only if nobody changed those rows since.

```sql
UPDATE show_seat
   SET status = 'held', booking_id = $1, version = version + 1,
       hold_until = now() + interval '5 minutes'
 WHERE show_id = $2
   AND (seat_id, version) IN (('A1', 3), ('A6', 7));
-- rowcount must equal 2; otherwise ROLLBACK and show "seats taken"
```

## Key flows

```text
User        BookingService      Show.lock     Pricing      PaymentGateway
 | hold(A1,A6)    |                 |            |                |
 |--------------->| acquire ------->|            |                |
 |                | all free? mark HELD,         |                |
 |                | holder=booking, until=now+5m |                |
 |                | release ------->|            |                |
 |                | price(seat) x2 ------------->|                |
 |<-- Booking ----|                 |            |                |
 | pay(booking)   |                 |            |                |
 |--------------->| PENDING->PAYING under lock   |                |
 |                | charge(booking_id, amount) ------------------>|
 |                |<------------------------------------ ok ------|
 |                | acquire; still ours? HELD->BOOKED; release    |
 |<-- CONFIRMED --|                 |            |                |
```

```text
User A                 BookingService                 User B
 | hold(A2) ok (t=0)         |                          |
 |   ... pays slowly ...     | t=301: A's hold expired  |
 |                           |<------ hold(A2) ---------|
 |                           | A2 free (expired) -> B   |
 | pay() ------------------->| charge ok, holder != A   |
 |<-- FAILED + refund -------|                          |
```

## Edge cases

- Selection mixes a free seat and a taken seat: reject the whole selection, hold nothing.
- Payment lands after the hold expired but nobody took the seats: confirm. The hold is lost only if someone else claimed a seat.
- Payment declined: release the seats immediately instead of waiting for expiry.
- Double-clicked "pay": the second call sees `PAYING` or `CONFIRMED` and returns; the gateway also dedups by booking id.
- Seat map is stale by the time the user clicks: `hold` re-checks under the lock; the UI shows "just taken".

## Production follow-ups

**How do you stop double booking?** One lock per show makes "check all seats free, then mark them held" atomic, and there is no lock-ordering risk because a booking never spans two shows. The GIL does not make that check-then-act atomic on its own. Across servers, use the optimistic `version` update above, or `SELECT ... FOR UPDATE` on the seat rows ordered by seat id, and back it with `UNIQUE(show_id, seat_id)` on the `booking_seat` table as the last line of defense.

**What survives a restart?** Seat status, holder, `hold_until` and booking status live in the database, so a restart loses nothing. Use wall-clock `hold_until` timestamps from the database (`now()`) rather than each server's monotonic clock, so all servers agree when a hold expires.

**What about retries around payment?** The booking id is the gateway's idempotency key, so a retried charge never bills twice. If the app crashes between "charge ok" and "mark booked", a reconciliation job reads `PAYING` bookings older than a minute, asks the gateway for the charge status and finishes or refunds. Webhooks from the gateway are deduplicated by event id.

**How do you add a new pricing rule?** Write another `PricingStrategy` (early-bird, dynamic by occupancy) or another decorator, and wire it where `BookingService` is built. `hold()` and `pay()` stay untouched.

**What breaks at 10×?** A blockbuster release puts thousands of users on one show, so that one lock (or row set) becomes the hot spot. Keep the critical section tiny (no I/O inside it), hold seats in Redis with `SET seat_key booking_id NX PX 300000` per seat if the database cannot keep up, and put a virtual waiting room in front of the sale. Shows are independent, so shard by `show_id`.

**What do you log and measure?** Log every hold, confirm, release and refund with booking id, show id and seats. Measure hold success rate, hold-to-pay conversion, holds that expired, late payments refunded, and lock wait time per show. Alert on any confirmed seat appearing in two bookings.

**How do expired holds get cleaned up?** Lazy expiry is enough for correctness: `_free` treats an expired hold as free. A periodic sweeper resets them so occupancy metrics stay accurate.

## Interview follow-ups

- **Per-seat locks instead of per-show?** Possible, but you must acquire them in sorted seat-id order to avoid deadlock. Per-show is simpler and contention is rarely the bottleneck.
- **Why not hold a database lock until payment finishes?** Payment takes seconds to minutes. A row lock that long blocks every other buyer and exhausts connections; a `HELD` status with an expiry does the same job without a lock.
- **Seat recommendations ("best 4 together")?** A strategy that scans each row of the layout for runs of free seats and scores them by distance from the center.
- **Waitlist when sold out?** Observers on seat release notify the next user in a FIFO queue, who gets a short hold.
- **Cancellation?** Release `BOOKED` seats back to `AVAILABLE` under the show lock, then refund through a cancellation-policy strategy keyed on time to showtime.
