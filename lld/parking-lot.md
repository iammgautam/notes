---
title: Design a parking lot
section: lld
order: 8
summary: Multi-floor parking lot with spot types, tickets, pluggable spot allocation and pricing strategies, and thread-safe parking.
---

## Requirements

Clarify and pin down:

- Multiple floors; each floor has spots of types: small (bike), compact (car), large (truck).
- Vehicle types: bike, car, truck. A vehicle fits its own type (optionally larger).
- Entry: assign a spot, issue a ticket. Exit: compute fee, pay, free the spot.
- Show availability per floor and type.
- Pricing can vary (hourly, flat, per vehicle type).
- Out of scope: reservations, real payment gateway, UI.
- Concurrent entries at multiple gates.

## Entities

| Entity | Responsibility |
|--------|----------------|
| `Vehicle` | Plate + `VehicleType` |
| `ParkingSpot` | Id, `SpotType`, occupied vehicle; atomic assign/free |
| `Floor` | Holds spots, finds a free spot |
| `Ticket` | Vehicle, spot, entry time, exit time, amount |
| `ParkingLot` | Floors, entry/exit flow (service) |
| `SpotAllocationStrategy` | Which free spot to choose |
| `PricingStrategy` | Fee from ticket |

## Class sketch

```python
import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class VehicleType(Enum):
    BIKE = 1
    CAR = 2
    TRUCK = 3


class SpotType(Enum):
    SMALL = 1
    COMPACT = 2
    LARGE = 3


FITS = {VehicleType.BIKE: SpotType.SMALL,
        VehicleType.CAR: SpotType.COMPACT,
        VehicleType.TRUCK: SpotType.LARGE}


@dataclass(frozen=True)
class Vehicle:
    plate: str
    type: VehicleType


@dataclass
class ParkingSpot:
    id: str
    type: SpotType
    vehicle: Vehicle | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def try_park(self, v: Vehicle) -> bool:
        with self._lock:
            if self.vehicle:
                return False
            self.vehicle = v
            return True

    def free(self) -> None:
        with self._lock:
            self.vehicle = None


@dataclass
class Floor:
    number: int
    spots: list[ParkingSpot]

    def free_spots(self, t: SpotType) -> list[ParkingSpot]:
        return [s for s in self.spots if s.type == t and s.vehicle is None]


@dataclass
class Ticket:
    vehicle: Vehicle
    spot: ParkingSpot
    entry: datetime
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    exit: datetime | None = None
    amount: int = 0


class SpotAllocationStrategy(ABC):
    @abstractmethod
    def candidates(self, floors: list[Floor], t: SpotType) -> list[ParkingSpot]: ...


class NearestFirst(SpotAllocationStrategy):
    def candidates(self, floors, t):
        return [s for f in floors for s in f.free_spots(t)]


class PricingStrategy(ABC):
    @abstractmethod
    def fee(self, ticket: Ticket) -> int: ...


class HourlyPricing(PricingStrategy):
    RATES = {VehicleType.BIKE: 10, VehicleType.CAR: 20, VehicleType.TRUCK: 40}

    def fee(self, t: Ticket) -> int:
        hours = max(1, -(-int((t.exit - t.entry).total_seconds()) // 3600))
        return hours * self.RATES[t.vehicle.type]
```

## Key flows

```python
class ParkingLot:
    def __init__(self, floors, allocator: SpotAllocationStrategy,
                 pricing: PricingStrategy):
        self.floors, self.allocator, self.pricing = floors, allocator, pricing
        self.active: dict[str, Ticket] = {}
        self._tickets_lock = threading.Lock()

    def park(self, v: Vehicle) -> Ticket:
        for spot in self.allocator.candidates(self.floors, FITS[v.type]):
            if spot.try_park(v):             # atomic; loser tries next spot
                t = Ticket(v, spot, datetime.now())
                with self._tickets_lock:
                    self.active[t.id] = t
                return t
        raise RuntimeError("lot full")

    def unpark(self, ticket_id: str) -> Ticket:
        with self._tickets_lock:
            t = self.active.pop(ticket_id)
        t.exit = datetime.now()
        t.amount = self.pricing.fee(t)
        t.spot.free()
        return t

    def availability(self) -> dict:
        return {(f.number, st.name): len(f.free_spots(st))
                for f in self.floors for st in SpotType}
```

- **Park:** find candidates → `try_park` each until one wins → create ticket.
- **Unpark:** pop ticket → set exit → compute fee → free spot.
- `candidates` is a snapshot; `try_park` re-checks under the spot's lock, so a stale snapshot is harmless.

## Patterns used

| Pattern | Where | Why |
|---------|-------|-----|
| Strategy | `SpotAllocationStrategy`, `PricingStrategy` | Nearest/by-floor/EV-first and pricing rules change independently |
| Singleton (optional) | `ParkingLot` | One lot per process; prefer injecting one instance |
| Factory | `VehicleFactory.create(type, plate)` | If vehicles get type-specific behavior |
| Observer | Display boards | Update availability on park/unpark events |

## Concurrency

- Race: two gates pick the same free spot. Fixed by per-spot lock and "try next on failure".
- Ticket map guarded by its own lock; never hold the spot lock and ticket lock together in reverse orders.
- Production: `UPDATE spot SET vehicle=? WHERE id=? AND vehicle IS NULL` and check rows affected.

## Extensions interviewers ask

- **EV spots with chargers:** new `SpotType.EV`, an allocation strategy that prefers EV for EVs, add-on pricing.
- **Larger spot fallback:** `FITS` maps to an ordered list of acceptable spot types.
- **Reservations:** `Reservation` entity holding a spot for a time window; spot gets `RESERVED` state.
- **Payment modes:** `PaymentMethod` strategy (cash, card, UPI); `Payment` entity linked to ticket.
- **Multiple entry/exit gates:** `Gate` entity calling the same `ParkingLot` service; concurrency already handled.
- **O(1) spot lookup:** keep a free-spot set per (floor, type) instead of scanning lists.
- **Lost ticket:** look up by plate; charge max daily rate.

> **Interview tip:** Draw the `FITS` mapping and the two strategies early. They are what the interviewer probes with "what if pricing changes?" and "what if we add EV?".
