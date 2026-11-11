---
title: Design a movie ticket booking system
section: lld
order: 13
summary: BookMyShow-style booking with shows, seat maps, temporary seat holds with expiry, payment, and concurrency-safe locking to prevent double booking.
---

## Requirements

- Browse movies by city → theaters → shows (screen + time).
- View a show's seat map with status and price per seat category.
- Select seats → **hold** them for a few minutes → pay → confirm booking.
- Hold expires if payment doesn't complete; seats return to available.
- Cancel a booking (refund policy out of scope).
- **No double booking** under heavy concurrency (popular releases).
- Out of scope: real payments, search ranking, notifications (mention).

## Entities

| Entity | Responsibility |
|--------|----------------|
| `Movie`, `Theater`, `Screen` | Catalog; screen has a seat layout |
| `Seat` | Row, number, `SeatCategory` (physical seat) |
| `Show` | Movie + screen + start time; owns per-show seat status |
| `ShowSeat` | Status for one seat in one show: `AVAILABLE`, `HELD`, `BOOKED` |
| `Booking` | User, show, seats, amount, status |
| `SeatLockProvider` | Hold/release seats with TTL |
| `PaymentService` | Charge (strategy per method) |
| `BookingService` | Orchestrates hold → pay → confirm |

> **Gotcha:** Seat status belongs to `(show, seat)`, not to `Seat`. The same physical seat is free at 6 pm and booked at 9 pm.

## Class sketch

```python
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class SeatStatus(Enum):
    AVAILABLE = "available"
    HELD = "held"
    BOOKED = "booked"


class BookingStatus(Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Seat:
    id: str
    row: str
    number: int
    category: str          # "silver", "gold"


@dataclass
class ShowSeat:
    seat: Seat
    price: int
    status: SeatStatus = SeatStatus.AVAILABLE
    held_by: str | None = None
    hold_expires: float = 0.0


@dataclass
class Show:
    id: str
    movie_id: str
    screen_id: str
    start: float
    seats: dict[str, ShowSeat]
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass
class Booking:
    user_id: str
    show_id: str
    seat_ids: list[str]
    amount: int
    status: BookingStatus = BookingStatus.PENDING
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
```

## Seat locking

```python
class SeatLockProvider:
    def __init__(self, ttl_seconds: int = 300, clock=time.monotonic):
        self.ttl, self.clock = ttl_seconds, clock

    def _free(self, ss: ShowSeat, now: float) -> bool:
        if ss.status is SeatStatus.AVAILABLE:
            return True
        # expired hold counts as free (lazy expiry)
        return ss.status is SeatStatus.HELD and ss.hold_expires <= now

    def hold(self, show: Show, seat_ids: list[str], user_id: str) -> None:
        with show.lock:                                 # all-or-nothing
            now = self.clock()
            if not all(self._free(show.seats[s], now) for s in seat_ids):
                raise ValueError("some seats unavailable")
            for s in seat_ids:
                ss = show.seats[s]
                ss.status, ss.held_by = SeatStatus.HELD, user_id
                ss.hold_expires = now + self.ttl

    def confirm(self, show: Show, seat_ids: list[str], user_id: str) -> None:
        with show.lock:
            now = self.clock()
            for s in seat_ids:
                ss = show.seats[s]
                if ss.held_by != user_id or ss.hold_expires <= now:
                    raise ValueError("hold expired")
            for s in seat_ids:
                show.seats[s].status = SeatStatus.BOOKED

    def release(self, show: Show, seat_ids: list[str], user_id: str) -> None:
        with show.lock:
            for s in seat_ids:
                ss = show.seats[s]
                if ss.held_by == user_id and ss.status is SeatStatus.HELD:
                    ss.status, ss.held_by = SeatStatus.AVAILABLE, None
```

- One lock **per show** keeps check-and-hold atomic for a multi-seat selection without lock-ordering issues.
- Finer grain: per-seat locks acquired in sorted seat-id order to avoid deadlock.

## Booking flow

```python
class BookingService:
    def __init__(self, shows: dict[str, Show], locks: SeatLockProvider, payments):
        self.shows, self.locks, self.payments = shows, locks, payments
        self.bookings: dict[str, Booking] = {}

    def start(self, user_id: str, show_id: str, seat_ids: list[str]) -> Booking:
        show = self.shows[show_id]
        self.locks.hold(show, seat_ids, user_id)
        amount = sum(show.seats[s].price for s in seat_ids)
        b = Booking(user_id, show_id, seat_ids, amount)
        self.bookings[b.id] = b
        return b

    def pay(self, booking_id: str, method) -> Booking:
        b = self.bookings[booking_id]
        show = self.shows[b.show_id]
        if not self.payments.charge(b.user_id, b.amount, method):   # no lock held
            self.locks.release(show, b.seat_ids, b.user_id)
            b.status = BookingStatus.CANCELLED
            return b
        try:
            self.locks.confirm(show, b.seat_ids, b.user_id)
        except ValueError:
            self.payments.refund(b.user_id, b.amount)   # paid after hold expired
            b.status = BookingStatus.CANCELLED
            raise
        b.status = BookingStatus.CONFIRMED
        return b
```

1. User selects seats → `start` holds them (or fails fast).
2. User pays within TTL → `pay` charges **outside** the lock → `confirm` re-checks the hold.
3. Payment fails → release. Payment succeeds after expiry → refund.
4. Expired holds are treated as free lazily; a background sweeper can also reset them for accurate seat maps.

## Patterns used

| Pattern | Where | Why |
|---------|-------|-----|
| Strategy | Payment method, pricing (weekday/weekend, dynamic) | Vary without touching booking |
| State | `SeatStatus`, `BookingStatus` | Explicit legal transitions |
| Facade | `BookingService` | One API over locks, payment, notification |
| Observer | Notify waitlist / user on confirm or release | Decoupled side effects |
| Repository | Shows, bookings | In-memory now, DB later |

## Concurrency in production

- **DB constraint:** `UNIQUE(show_id, seat_id)` on `booking_seat` makes a double book fail at insert.
- **Pessimistic:** `select_for_update()` on `show_seat` rows filtered by ids, ordered by id.
- **Optimistic:** `UPDATE show_seat SET status='held', held_by=?, version=version+1 WHERE id IN (...) AND status='available'`; if rows affected != requested, roll back.
- **Distributed hold:** Redis `SET show:{id}:seat:{s} user NX PX 300000` per seat; release with a check-and-delete Lua script.

> **In production:** never hold a DB row lock across a payment gateway call. Hold with a status + expiry, commit, then pay.

## Extensions interviewers ask

- **Hold expiry at scale:** delayed job or Redis key expiry events; or just lazy expiry on read.
- **Seat recommendation:** "best available N together" strategy over the layout grid.
- **Dynamic pricing:** pricing strategy using occupancy and time to show.
- **Coupons / offers:** decorator or chain over the base price.
- **Cancellations and refunds:** refund policy strategy based on time to show.
- **Waitlist:** observer on seat release.
