---
title: Design a hotel booking system
section: lld
order: 33
phase: "Phase 5 · Intermediate"
summary: Room types, availability over date ranges with half-open interval checks, hold/confirm/cancel, cancellation policy strategies and the overbooking trade-off.
---

## Requirements

Clarifying questions worth asking:

- Does the guest book a room type or a specific room? (A type; the system picks the room.)
- Is check-out day free for the next guest? (Yes. A stay is the half-open range `[check_in, check_out)`.)
- How long is a room held while the guest pays? (15 minutes.)
- Are cancellation rules per hotel or per rate plan? (Per rate plan: a non-refundable rate is cheaper.)
- Do we overbook? (Not in the base design; discussed in follow-ups.)

Functional:

- Search: how many rooms of a type are free for a date range.
- Hold a room for a date range, confirm after payment, cancel.
- Refund on cancellation according to the reservation's policy.

Non-functional:

- Never give one room to two overlapping stays.
- Holds that are never paid release the room automatically.
- Confirm and cancel are safe to retry.

Out of scope: multi-hotel search, dynamic pricing, payment gateway internals, loyalty.

## Entities and relationships

```text
+------------------------------+  has *  +----------------------+
| Hotel                        |-------->| Room                 |
|------------------------------|         | number, type         |
| rates: {RoomType: Decimal}   |         +----------------------+
| by_room: {number: [Res]}     |                    ^ refers to 1
| lock: Lock                   |  has *  +----------+-----------+
| availability(), hold()       |-------->| Reservation          |
| confirm(), cancel()          |         |----------------------|
+------------------------------+         | guest, room          |
                                         | check_in, check_out  |
                                         | rate, status         |
                                         | hold_until, refunded |
                                         | overlaps()           |
                                         | blocks_room(now)     |
                                         +----------+-----------+
                                                    | uses 1
                                                    v
                                         +----------------------+
                                         | <<abstract>>         |
                                         | CancellationPolicy   |
                                         | refund(res, now)     |
                                         +----------------------+
                                            ^ is-a
                                  NonRefundable, FreeCancellation
```

- Two stays overlap when `a.check_in < b.check_out and b.check_in < a.check_out`. With half-open ranges, a check-out on the 4th and a check-in on the 4th do not clash.
- A reservation blocks its room only while it is `CONFIRMED`, or `HELD` with an unexpired hold.

## Patterns and why

| Pattern | Where | Why (what it prevents) |
|---|---|---|
| Strategy | `CancellationPolicy` | Refund rules as `if rate_plan == ...` branches inside `cancel()` |
| State | `Status` with guarded transitions | Confirming a cancelled stay, or refunding twice |
| Clock injection | `clock` argument | Tests that sleep 15 minutes to see a hold expire |
| Aggregate with one lock | `Hotel` | Two threads both seeing a room as free and booking it |

## Code

```python
from __future__ import annotations

import threading
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Callable


class RoomType(Enum):
    SINGLE = "single"
    DOUBLE = "double"
    SUITE = "suite"


class Status(Enum):
    HELD = "held"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class NoAvailability(Exception):
    pass


class HoldExpired(Exception):
    pass


@dataclass(frozen=True)
class Room:
    number: str
    type: RoomType


class CancellationPolicy(ABC):
    @abstractmethod
    def refund(self, res: Reservation, now: datetime) -> Decimal: ...


class NonRefundable(CancellationPolicy):
    def refund(self, res, now):
        return Decimal(0)


class FreeCancellation(CancellationPolicy):
    """Full refund up to `days` before check-in; after that the first night is kept."""

    def __init__(self, days: int) -> None:
        self.days = days

    def refund(self, res, now):
        if (res.check_in - now.date()).days >= self.days:
            return res.amount
        return res.amount - res.rate


@dataclass
class Reservation:
    id: str
    guest: str
    room: Room
    check_in: date
    check_out: date                     # exclusive: guest leaves that morning
    rate: Decimal                       # per night
    policy: CancellationPolicy
    hold_until: datetime
    status: Status = Status.HELD
    refunded: Decimal = Decimal(0)

    @property
    def amount(self) -> Decimal:
        return self.rate * (self.check_out - self.check_in).days

    def overlaps(self, check_in: date, check_out: date) -> bool:
        return self.check_in < check_out and check_in < self.check_out

    def blocks_room(self, now: datetime) -> bool:
        return self.status is Status.CONFIRMED or (
            self.status is Status.HELD and self.hold_until > now)


class Hotel:
    def __init__(self, rooms: list[Room], rates: dict[RoomType, Decimal],
                 hold_minutes: int = 15,
                 clock: Callable[[], datetime] = datetime.now) -> None:
        self.rooms, self.rates, self.clock = rooms, rates, clock
        self.hold_for = timedelta(minutes=hold_minutes)
        self.by_room: dict[str, list[Reservation]] = defaultdict(list)
        self.reservations: dict[str, Reservation] = {}
        self.lock = threading.Lock()    # one per hotel; split per room type if contended

    def _is_free(self, room: Room, check_in: date, check_out: date,
                 ignore: Reservation | None = None) -> bool:
        now = self.clock()
        return not any(r is not ignore and r.overlaps(check_in, check_out)
                       and r.blocks_room(now) for r in self.by_room[room.number])

    def availability(self, room_type: RoomType, check_in: date, check_out: date) -> int:
        with self.lock:
            return sum(self._is_free(r, check_in, check_out)
                       for r in self.rooms if r.type is room_type)

    def hold(self, guest: str, room_type: RoomType, check_in: date,
             check_out: date, policy: CancellationPolicy) -> Reservation:
        if not self.clock().date() <= check_in < check_out:
            raise ValueError("check-in must be today or later and before check-out")
        with self.lock:                 # find + insert must be one atomic step
            room = next((r for r in self.rooms if r.type is room_type
                         and self._is_free(r, check_in, check_out)), None)
            if room is None:
                raise NoAvailability(room_type)
            res = Reservation(uuid.uuid4().hex, guest, room, check_in, check_out,
                              self.rates[room_type], policy,
                              self.clock() + self.hold_for)
            self.by_room[room.number].append(res)
            self.reservations[res.id] = res
            return res

    def confirm(self, res_id: str) -> Reservation:
        """Called once payment succeeded. Safe to call twice."""
        with self.lock:
            res = self.reservations[res_id]
            if res.status is Status.CONFIRMED:
                return res
            # An expired hold still wins if nobody else took the room meanwhile.
            if res.status is not Status.HELD or not self._is_free(
                    res.room, res.check_in, res.check_out, ignore=res):
                res.status = Status.CANCELLED
                raise HoldExpired(res_id)
            res.status = Status.CONFIRMED
            return res

    def cancel(self, res_id: str) -> Decimal:
        """Returns the refund. A repeated cancel returns the same refund, once."""
        with self.lock:
            res = self.reservations[res_id]
            if res.status is Status.CONFIRMED:
                res.refunded = res.policy.refund(res, self.clock())
            res.status = Status.CANCELLED
            return res.refunded


if __name__ == "__main__":
    import random
    from concurrent.futures import ThreadPoolExecutor
    from itertools import combinations

    now = [datetime(2026, 11, 12, 10, 0)]
    D = lambda day: date(2026, 12, day)
    rooms = [Room("D1", RoomType.DOUBLE), Room("D2", RoomType.DOUBLE)]
    hotel = Hotel(rooms, {RoomType.DOUBLE: Decimal("120.00")}, clock=lambda: now[0])
    flex, strict = FreeCancellation(days=2), NonRefundable()

    a = hotel.hold("ann", RoomType.DOUBLE, D(1), D(4), flex)
    b = hotel.hold("bob", RoomType.DOUBLE, D(3), D(5), flex)
    assert (a.room.number, b.room.number) == ("D1", "D2")
    assert hotel.availability(RoomType.DOUBLE, D(3), D(4)) == 0
    c = hotel.hold("cat", RoomType.DOUBLE, D(4), D(6), strict)  # back-to-back with ann
    assert c.room.number == "D1"
    for r in (a, c, c):                                   # second confirm is a no-op
        hotel.confirm(r.id)

    now[0] += timedelta(minutes=16)                       # bob never paid
    e = hotel.hold("eve", RoomType.DOUBLE, D(3), D(5), flex)
    assert e.room.number == "D2"
    try:
        hotel.confirm(b.id)
        raise AssertionError("expired hold confirmed")
    except HoldExpired:
        pass

    assert hotel.cancel(a.id) == Decimal("360.00")        # 19 days out: full refund
    assert hotel.cancel(a.id) == Decimal("360.00")        # retry: same answer
    assert hotel.cancel(c.id) == Decimal(0)               # non-refundable
    hotel.confirm(e.id)
    now[0] = datetime(2026, 12, 2, 9, 0)                  # one day before check-in
    assert hotel.cancel(e.id) == Decimal("120.00")        # first night kept

    # Stress: 5 rooms, 300 random stays from 16 threads; no room double booked.
    big = Hotel([Room(f"R{i}", RoomType.SINGLE) for i in range(5)],
                {RoomType.SINGLE: Decimal("80")}, clock=lambda: now[0])

    def book(_: int) -> None:
        start = random.randint(5, 25)
        try:
            big.confirm(big.hold("g", RoomType.SINGLE, D(start),
                                 D(start + random.randint(1, 4)), flex).id)
        except NoAvailability:
            pass

    with ThreadPoolExecutor(16) as pool:
        list(pool.map(book, range(300)))
    for stays in big.by_room.values():
        for x, y in combinations(stays, 2):
            assert not x.overlaps(y.check_in, y.check_out)
    print("hotel booking ok")
```

## Key flows

```text
Guest        Hotel            Hotel.lock      Payment       Policy
  | hold(DOUBLE, 1-4 Dec)       |               |             |
  |---------->| acquire ------->|               |             |
  |           | first room with no blocking overlap           |
  |           | add HELD res, hold_until = now + 15m          |
  |           | release ------->|               |             |
  |<-- res ---|                 |               |             |
  | pay ---------------------------------------->|             |
  |<----------------------------------- ok ------|             |
  | confirm(res)                |               |             |
  |---------->| under lock: HELD and room still free?         |
  |           | -> CONFIRMED (else CANCELLED + HoldExpired)   |
  |<-- res ---|                 |               |             |
```

```text
Guest        Hotel                         CancellationPolicy
  | cancel(res)  |                                 |
  |------------->| under lock                      |
  |              | CONFIRMED? refund(res, now) --->|
  |              |<------ amount (full / minus 1st night / 0)
  |              | status = CANCELLED, store refunded
  |<-- refund ---| (second cancel returns stored refund)
```

## Edge cases

- Check-out on the same day as the next check-in: allowed because ranges are half-open.
- `check_in >= check_out`, or check-in in the past: reject.
- Hold expired but nobody took the room: confirm still succeeds. Taken: cancel the hold and refund.
- Cancel after check-in or after the stay: the policy decides; usually no refund.
- Cancelling a `HELD` reservation: no charge happened, refund is zero.
- Long stays across a price change: store a rate per night instead of one `rate` if prices vary.
- Expired holds stay in the list until a sweeper removes them; they never block.

## Production follow-ups

**How do you stop double booking under concurrency?** Finding a free room and inserting the reservation must be one atomic step, so both happen under the hotel lock. The GIL does not give you that: two threads can both run the "is it free?" scan before either appends. In PostgreSQL, use an exclusion constraint so the database rejects overlaps itself: `EXCLUDE USING gist (room_id WITH =, daterange(check_in, check_out) WITH &&) WHERE (status <> 'cancelled')`.

**What survives a restart?** Reservations, with status and `hold_until` as a database timestamp, are the source of truth; `by_room` is just an index you rebuild with one query on startup. Expired holds need no recovery because they never block.

**How are retries handled?** `confirm` returns the reservation if it is already confirmed, and `cancel` returns the stored `refunded` amount, so both are idempotent. The client sends an idempotency key with `hold` so a retried request does not create two holds; the payment step uses the reservation id as the gateway's idempotency key.

**How do you add a new cancellation rule?** Add a `CancellationPolicy` subclass (tiered by days, keep 50%) and attach it to a rate plan. `Hotel.cancel` never changes. The same goes for pricing: a `PricingStrategy` per rate plan (weekend, length-of-stay discount).

**What breaks at 10×, or with many hotels?** The scan is `O(rooms × reservations)`; real systems store inventory as a counter per `(hotel, room_type, night)` and decrement every night of the stay in one transaction with `UPDATE ... SET sold = sold + 1 WHERE sold < capacity` per night. The physical room is assigned at check-in. Hotels are independent, so shard by `hotel_id`; search across hotels reads a cached availability index that may be slightly stale, and the hold re-checks the truth.

**What do you log and measure?** Log hold, confirm, expire and cancel with reservation id, room, dates and policy. Measure hold-to-confirm conversion, expired holds, `HoldExpired` after payment (each one is a refund and an angry guest), occupancy per night and refund totals.

**Should you overbook?** Hotels often sell around 5% more rooms than they have, because a predictable share of guests never arrive. With per-night counters that is `sold < capacity * 1.05`, rooms are assigned at check-in, and the rare guest without a room is moved to a partner hotel at your cost. It is a business decision driven by the no-show rate; the design just needs the counter model and a "walk the guest" flow.

**What about time zones?** Dates are local to the hotel. Compute "days before check-in" in the hotel's time zone, not the server's, or a refund cutoff shifts by a day.

## Interview follow-ups

- **Which room do you pick when several are free?** First fit is fine; a best-fit strategy picks the room whose neighboring gaps are smallest, so short gaps between stays do not become unsellable.
- **How do you check overlaps fast?** Keep each room's reservations sorted by `check_in` and binary search the neighbours, as in the [meeting room scheduler]({{ '/lld/meeting-room-scheduler/' | relative_url }}). It becomes `O(log n)` per room.
- **Modify a booking (extend a stay)?** Treat it as hold-new-range then cancel-old inside one lock, ignoring the old reservation when checking overlap, so the guest never loses the room in between.
- **How is this different from movie booking?** Seats are a fixed set per show; rooms are reused across overlapping date ranges, so the core check is interval overlap instead of a status flag. See [movie ticket booking]({{ '/lld/movie-ticket-booking/' | relative_url }}).
