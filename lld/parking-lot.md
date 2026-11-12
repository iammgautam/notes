---
title: Design a parking lot
section: lld
order: 22
phase: "Phase 5 · Intermediate"
summary: Multi-floor lot with spot and vehicle types, swappable allocation and pricing strategies, gates, tickets, and race-free spot assignment.
---

## Requirements

Clarifying questions to ask first:

- How many floors and gates? Do all gates talk to one lot service? (Yes.)
- Which vehicle and spot types? Can a bike take a car spot? (Yes, smallest fitting spot first.)
- How is the fee computed? Hourly per vehicle type, partial hours rounded up?
- Is payment taken at the exit gate? (Compute the amount; the payment gateway is out of scope.)

**Functional**

- Floors with small, compact and large spots. Bikes, cars and trucks.
- Entry gate assigns a spot and issues a ticket, or rejects when no fitting spot is free.
- Exit gate closes the ticket, computes the fee and frees the spot.
- Free-spot counts per floor and type for the display boards.
- Allocation rule and pricing rule can change without touching the lot.

**Non-functional**

- Several gates run at once and must never put two vehicles in one spot.
- Allocation is `O(floors × spot types)`, not a scan of every spot.
- Money is `Decimal`.

**Out of scope:** payment gateway, reservations, cameras and sensors, UI.

## Entities and relationships

```text
+-----------+   +----------+
| EntryGate |   | ExitGate |
+-----------+   +----------+
      | uses         | uses
      v              v
+-------------------------------+      +---------------------------+
| ParkingLot                    | uses | AllocationStrategy (ABC)  |
| floors: list[Floor]           |----->| choose(floors, v) -> Spot |
| tickets: dict[str, Ticket]    |      +---------------------------+
| parked: dict[plate, ticket]   |        ^ is-a: LowestFloorFirst,
| park(vehicle, gate) -> Ticket |        |       EmptiestFloorFirst
| unpark(ticket_id) -> Ticket   | uses +---------------------------+
| availability()                |----->| PricingStrategy (ABC)     |
+-------------------------------+      | fee(ticket) -> Decimal    |
      | has 1..*        | has 0..*     +---------------------------+
      v                 v                ^ is-a: HourlyPricing,
+-----------------+  +--------------+    |       FlatPricing
| Floor           |  | Ticket       |
| number          |  | id, gate     |
| free: SpotType  |  | vehicle,spot |
|   -> set[Spot]  |  | entry_at     |
+-----------------+  | exit_at, fee |
      | has 1..*     +--------------+
      v
+-----------------+  +--------------+
| Spot (frozen)   |  | Vehicle      |
| id, floor, type |  | plate, type  |
+-----------------+  +--------------+
```

A spot does not store its occupant. "Is it free?" has one answer: whether it sits in its floor's free set. That keeps the lock in one place.

## Patterns and why

| Pattern | Where | Why |
|---|---|---|
| Strategy | `AllocationStrategy` | Fill-from-ground vs spread-the-load changes per site; the lot never branches on it |
| Strategy | `PricingStrategy` | Hourly, flat, weekend rates change often; new rule = new class |
| Value object | `Vehicle`, `Spot` (frozen) | Hashable, safe to share across threads, usable in sets |
| Singleton, avoided | `ParkingLot` | Inject one instance; a global makes tests and multi-lot support painful |

## Code

```python
import math
import threading
import uuid
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Callable


class VehicleType(Enum):
    BIKE = "bike"
    CAR = "car"
    TRUCK = "truck"


class SpotType(Enum):
    SMALL = 1
    COMPACT = 2
    LARGE = 3


# Acceptable spot types per vehicle, smallest first.
FITS = {
    VehicleType.BIKE: [SpotType.SMALL, SpotType.COMPACT, SpotType.LARGE],
    VehicleType.CAR: [SpotType.COMPACT, SpotType.LARGE],
    VehicleType.TRUCK: [SpotType.LARGE],
}


@dataclass(frozen=True)
class Vehicle:
    plate: str
    type: VehicleType


@dataclass(frozen=True)
class Spot:
    id: str
    floor: int
    type: SpotType


class Floor:
    def __init__(self, number: int, counts: dict[SpotType, int]):
        self.number = number
        self.free: dict[SpotType, set[Spot]] = {t: set() for t in SpotType}
        for t, n in counts.items():
            self.free[t] = {Spot(f"F{number}-{t.name}-{i}", number, t)
                            for i in range(n)}


@dataclass
class Ticket:
    vehicle: Vehicle
    spot: Spot
    entry_at: datetime
    gate: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    exit_at: datetime | None = None
    fee: Decimal | None = None


class AllocationStrategy(ABC):
    @abstractmethod
    def choose(self, floors: list[Floor], v: Vehicle) -> Spot | None: ...


class LowestFloorFirst(AllocationStrategy):
    def choose(self, floors, v):
        for f in floors:
            for t in FITS[v.type]:
                if f.free[t]:
                    return next(iter(f.free[t]))
        return None


class EmptiestFloorFirst(AllocationStrategy):
    def choose(self, floors, v):
        best = max(floors, key=lambda f: sum(len(f.free[t]) for t in FITS[v.type]))
        return LowestFloorFirst().choose([best], v)


class PricingStrategy(ABC):
    @abstractmethod
    def fee(self, t: Ticket) -> Decimal: ...


class HourlyPricing(PricingStrategy):
    """Every started hour is billed, minimum one hour."""

    def __init__(self, rates: dict[VehicleType, Decimal]):
        self.rates = rates

    def fee(self, t):
        hours = max(1, math.ceil((t.exit_at - t.entry_at).total_seconds() / 3600))
        return self.rates[t.vehicle.type] * hours


class FlatPricing(PricingStrategy):
    def __init__(self, amount: Decimal):
        self.amount = amount

    def fee(self, t):
        return self.amount


class LotFull(Exception):
    pass


class ParkingLot:
    def __init__(self, floors: list[Floor], allocator: AllocationStrategy,
                 pricing: PricingStrategy, clock: Callable[[], datetime] = datetime.now):
        self.floors = floors
        self._floor = {f.number: f for f in floors}
        self.allocator, self.pricing, self.clock = allocator, pricing, clock
        self.tickets: dict[str, Ticket] = {}
        self.parked: dict[str, str] = {}  # plate -> open ticket id
        # Simplification: one lock for the whole lot; per-floor locks only if gates contend
        self._lock = threading.Lock()

    def park(self, v: Vehicle, gate: str) -> Ticket:
        with self._lock:
            if v.plate in self.parked:  # gate retry: same ticket, no second spot
                return self.tickets[self.parked[v.plate]]
            spot = self.allocator.choose(self.floors, v)
            if spot is None:
                raise LotFull(v.type.value)
            self._floor[spot.floor].free[spot.type].remove(spot)
            t = Ticket(v, spot, self.clock(), gate)
            self.tickets[t.id] = t
            self.parked[v.plate] = t.id
            return t

    def unpark(self, ticket_id: str) -> Ticket:
        with self._lock:
            t = self.tickets[ticket_id]
            if t.exit_at is None:  # second call is a no-op
                t.exit_at = self.clock()
                t.fee = self.pricing.fee(t)
                self._floor[t.spot.floor].free[t.spot.type].add(t.spot)
                del self.parked[t.vehicle.plate]
            return t

    def availability(self) -> dict[tuple[int, str], int]:
        with self._lock:
            return {(f.number, t.name): len(f.free[t])
                    for f in self.floors for t in SpotType}


@dataclass
class EntryGate:
    id: str
    lot: ParkingLot

    def admit(self, v: Vehicle) -> Ticket:
        return self.lot.park(v, self.id)


@dataclass
class ExitGate:
    id: str
    lot: ParkingLot

    def release(self, ticket_id: str) -> Ticket:
        return self.lot.unpark(ticket_id)


if __name__ == "__main__":
    now = [datetime(2026, 1, 1, 9, 0)]
    floors = [Floor(0, {SpotType.SMALL: 1, SpotType.COMPACT: 2, SpotType.LARGE: 1}),
              Floor(1, {SpotType.COMPACT: 2, SpotType.LARGE: 1})]
    rates = {VehicleType.BIKE: Decimal("10"), VehicleType.CAR: Decimal("20"),
             VehicleType.TRUCK: Decimal("50")}
    lot = ParkingLot(floors, LowestFloorFirst(), HourlyPricing(rates), lambda: now[0])
    entry, exit_ = EntryGate("E1", lot), ExitGate("X1", lot)

    car = entry.admit(Vehicle("KA01", VehicleType.CAR))
    assert (car.spot.floor, car.spot.type) == (0, SpotType.COMPACT)
    assert entry.admit(Vehicle("KA01", VehicleType.CAR)) is car  # retried entry
    entry.admit(Vehicle("T1", VehicleType.TRUCK))
    entry.admit(Vehicle("T2", VehicleType.TRUCK))
    try:
        entry.admit(Vehicle("T3", VehicleType.TRUCK))
        raise AssertionError("expected LotFull")
    except LotFull:
        pass

    now[0] += timedelta(hours=2, minutes=1)
    assert exit_.release(car.id).fee == Decimal("60")  # 3 started hours x 20
    assert exit_.release(car.id).fee == Decimal("60")  # retried exit
    assert lot.availability()[(0, "COMPACT")] == 2

    spread = ParkingLot([Floor(0, {SpotType.COMPACT: 1}), Floor(1, {SpotType.COMPACT: 3})],
                        EmptiestFloorFirst(), FlatPricing(Decimal("5")))
    assert spread.park(Vehicle("X", VehicleType.CAR), "E1").spot.floor == 1

    # Stress: 40 cars race through 8 gates for 4 car spots, 20 rounds.
    def try_park(i):
        try:
            return lot.park(Vehicle(f"C{i}", VehicleType.CAR), f"E{i % 8}").spot
        except LotFull:
            return None

    for _ in range(20):
        lot = ParkingLot([Floor(0, {SpotType.COMPACT: 2}), Floor(1, {SpotType.LARGE: 2})],
                         LowestFloorFirst(), HourlyPricing(rates))
        with ThreadPoolExecutor(16) as pool:
            spots = [s for s in pool.map(try_park, range(40)) if s]
        assert len(spots) == len(set(spots)) == 4
        assert sum(lot.availability().values()) == 0
    print("parking lot ok")
```

## Key flows

```text
Driver    EntryGate       ParkingLot           AllocationStrategy
  | arrive    |                |                        |
  |---------->| park(v, "E1")  |                        |
  |           |--------------->| lock                   |
  |           |                | plate already parked?  |
  |           |                | choose(floors, v)      |
  |           |                |----------------------->|
  |           |                |<--------- spot --------|
  |           |                | remove spot from free set
  |           |                | create Ticket, unlock  |
  |           |<--- ticket ----|                        |
  |<- ticket -|                |                        |

Driver    ExitGate        ParkingLot           PricingStrategy
  | ticket    |                |                        |
  |---------->| unpark(id)     |                        |
  |           |--------------->| lock; closed? return   |
  |           |                | exit_at = clock()      |
  |           |                | fee(ticket)            |
  |           |                |----------------------->|
  |           |                |<------- Decimal -------|
  |           |                | spot back to free set, unlock
  |           |<- ticket, fee -|                        |
```

## Edge cases

- Same plate arrives again while parked (gate retry, cloned plate): return the open ticket instead of taking a second spot.
- No fitting spot even though smaller spots are free: `LotFull`; boards show counts per type.
- Exit pressed twice: the second call returns the closed ticket and does not free the spot again.
- Unknown ticket id: lost-ticket flow, look up by plate and charge a daily maximum.
- Stay of 30 seconds: billed one hour by policy. Ask, don't assume.
- Clock moves backward after an NTP correction: the duration goes negative, `max(1, ...)` bills one hour. Store UTC.
- A bike in the last large spot blocks a truck. Smallest-first allocation reduces it; a stricter strategy can refuse oversize spots when the lot is above some fill level.

## Production follow-ups

**How is this thread-safe, and what changes with a database?** One lock covers choose-and-remove, a few dictionary operations with no I/O, so gates rarely wait. With PostgreSQL, claim the row atomically: `UPDATE spot SET ticket_id = :t WHERE id = (SELECT id FROM spot WHERE type = ANY(:types) AND ticket_id IS NULL ORDER BY floor LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id`. `SKIP LOCKED` lets concurrent gates take different rows instead of queueing on one. See [Concurrency in LLD]({{ '/lld/concurrency-in-lld/' | relative_url }}).

**What survives a restart?** Spots and tickets are rows; the free sets are a cache rebuilt at boot from spots that have no open ticket. An open ticket needs nothing in memory to be closed later, so a crash between entry and exit loses nothing.

**How do retries stay safe?** Entry is keyed by plate and returns the open ticket; a partial unique index `UNIQUE (plate) WHERE exit_at IS NULL` enforces it in the database. Exit is a no-op on a closed ticket. The payment call uses the ticket id as its idempotency key so a retried charge bills once.

**How do you add a vehicle or pricing type without editing old code?** A new rule (weekend, EV charging) is a new `PricingStrategy`; a decorator can add a surcharge on top of any base pricing. A new vehicle type is one enum member and one `FITS` row; move `FITS` to a config table so it becomes data.

**What breaks at 10× or across machines?** One lot is a few thousand spots, which one process handles easily. A city operator with hundreds of lots partitions by `lot_id`: allocation never crosses lots, so there is no global lock. Display boards read counters from Redis updated on park and unpark events instead of counting rows.

**What do you log and measure?** Occupancy per floor and type, `LotFull` rejections by vehicle type (a high count means the spot mix is wrong), park and unpark latency, tickets open longer than 24 hours, revenue per hour. Log every ticket transition with ticket id, plate and gate.

**How do you keep money right?** `Decimal` end to end, quantized once when you show the total. Store the rate card version on the ticket so a price change mid-stay bills the rate the driver saw at entry.

## Interview follow-ups

- **Add EV charging spots?** `SpotType.EV`, an EV vehicle type whose `FITS` list starts with `EV`, and a pricing decorator that adds the kWh charge.
- **Nearest spot to the entrance?** Replace each free set with a heap keyed by distance; `choose` peeks, `park` pops.
- **Reservations?** A `Reservation` with a time window removes a spot from the free set from the window start until a grace period expires.
- **Why doesn't `Spot` have an `occupied` flag?** Two sources of truth drift. Free-set membership is the only state, and the lot's lock guards it.
- **Why a lot-wide lock instead of per-spot locks?** The critical section is microseconds. Per-spot locks need a scan-and-retry loop and only pay off when profiling shows contention.
