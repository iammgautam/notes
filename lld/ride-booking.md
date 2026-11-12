---
title: Design a ride booking system
section: lld
order: 40
phase: "Phase 5 · Intermediate"
summary: Riders, drivers, nearest-driver matching, a ride state machine, surge-aware fare strategies, and locking so one driver can never accept two rides.
---

A ride booking system (Uber, Lyft, Ola) matches a rider's request to a nearby driver, tracks the ride through its lifecycle and prices it. The interesting parts are the matching strategy, the state machine and the accept race.

## Requirements

Clarifying questions:
- One ride type, or several (auto, sedan, pool)? Start with one.
- Do we offer the ride to several drivers at once, or one at a time? Several; first to accept wins.
- How is the fare computed? Base + per km + per minute, times a surge multiplier.
- Is surge locked in when the rider requests? Yes.
- Payments, ratings, pooling? Out of scope.

Functional:
- Drivers go online/offline and push location updates.
- A rider requests a ride from pickup to drop.
- Find available drivers within a radius, nearest first, and offer them the ride.
- A driver accepts; then the ride is started and completed (or cancelled).
- Compute the fare on completion using the pricing in force at request time.

Non-functional:
- A driver is never on two rides; a ride never gets two drivers.
- Location updates are frequent and must be cheap.
- Matching should return in well under a second.

Out of scope: payments, ratings, ride pooling, ETA from road graphs.

## Entities and relationships

```text
+-------------------+ uses +-----------------------+
| RideService       |----->| MatchingStrategy      |
| drivers {id: D}   |      | candidates(pickup, ds)|
| rides {id: R}     |      +-----------^-----------+
| pricing           |                  | is-a
| update_location() |      +-----------+-----------+
| request_ride()    |      | NearestWithinRadius   |
| accept(), start() |      +-----------------------+
| complete()        | uses +-----------------------+
| cancel()          |----->| FareStrategy          |
+---------+---------+      | fare(km, minutes)     |
          | 1..*           +-----------^-----------+
          |                            | is-a
          |                +-----------+-----------+
          |                | StandardFare          |
          |                | SurgeFare(inner)      |
          |                +-----------------------+
+---------v--------+ 0..1 +--------------+
| Ride             |----->| Driver       |
| id, rider_id     |      | id, location |
| pickup, drop     |      | status, lock |
| status, driver   |      +--------------+
| pricing, fare    |
| lock, move_to()  |
+------------------+
Location(lat, lng).km_to(other) is an immutable value object.
```

## Patterns and why

| Pattern | Where | Why |
|---|---|---|
| Strategy | `MatchingStrategy` | Swap nearest-first for ETA-based or rating-weighted matching without touching the service |
| Strategy + decorator | `FareStrategy`, `SurgeFare` | Surge wraps any base fare; new pricing = new class |
| State machine | `Ride.move_to` + `ALLOWED` | Illegal jumps (complete before start) fail loudly |
| Value object | `Location` | Immutable, safe to share across threads |

## Code

```python
from __future__ import annotations

import itertools
import math
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

CENT = Decimal("0.01")


@dataclass(frozen=True)
class Location:
    lat: float
    lng: float

    def km_to(self, other: Location) -> float:  # haversine
        p1, p2 = math.radians(self.lat), math.radians(other.lat)
        dp, dl = p2 - p1, math.radians(other.lng - self.lng)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * 6371 * math.asin(math.sqrt(a))


class DriverStatus(Enum):
    OFFLINE = "offline"
    AVAILABLE = "available"
    ON_TRIP = "on_trip"


class RideStatus(Enum):
    REQUESTED = "requested"
    ACCEPTED = "accepted"
    STARTED = "started"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


ALLOWED = {
    RideStatus.REQUESTED: {RideStatus.ACCEPTED, RideStatus.CANCELLED},
    RideStatus.ACCEPTED: {RideStatus.STARTED, RideStatus.CANCELLED},
    RideStatus.STARTED: {RideStatus.COMPLETED},
}


class InvalidTransition(Exception):
    pass


class FareStrategy(ABC):
    @abstractmethod
    def fare(self, km: float, minutes: float) -> Decimal: ...


class StandardFare(FareStrategy):
    def __init__(self, base="50", per_km="12", per_min="2"):
        self.base, self.per_km, self.per_min = map(Decimal, (base, per_km, per_min))

    def fare(self, km, minutes):
        raw = self.base + self.per_km * Decimal(f"{km:.2f}") + self.per_min * Decimal(f"{minutes:.2f}")
        return raw.quantize(CENT, ROUND_HALF_UP)


class SurgeFare(FareStrategy):
    def __init__(self, inner: FareStrategy, multiplier: str):
        self.inner, self.multiplier = inner, Decimal(multiplier)

    def fare(self, km, minutes):
        return (self.inner.fare(km, minutes) * self.multiplier).quantize(CENT, ROUND_HALF_UP)


@dataclass(eq=False)
class Driver:
    id: str
    location: Location
    status: DriverStatus = DriverStatus.AVAILABLE
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass(eq=False)
class Ride:
    id: int
    rider_id: str
    pickup: Location
    drop: Location
    pricing: FareStrategy  # snapshot at request time: surge can't change mid-ride
    status: RideStatus = RideStatus.REQUESTED
    driver: Driver | None = None
    fare: Decimal | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def move_to(self, new: RideStatus) -> None:
        if new not in ALLOWED.get(self.status, set()):
            raise InvalidTransition(f"{self.status.value} -> {new.value}")
        self.status = new


class MatchingStrategy(ABC):
    @abstractmethod
    def candidates(self, pickup: Location, drivers: list[Driver]) -> list[Driver]: ...


class NearestWithinRadius(MatchingStrategy):
    def __init__(self, radius_km: float = 3.0, limit: int = 5):
        self.radius_km, self.limit = radius_km, limit

    def candidates(self, pickup, drivers):
        # Simplification: linear scan; use a geohash/H3 cell index past ~10k drivers
        near = [(d.location.km_to(pickup), d) for d in drivers
                if d.status is DriverStatus.AVAILABLE]
        near = sorted((x for x in near if x[0] <= self.radius_km), key=lambda x: x[0])
        return [d for _, d in near[: self.limit]]


class RideService:
    def __init__(self, matcher: MatchingStrategy, pricing: FareStrategy):
        self.matcher, self.pricing = matcher, pricing  # swap pricing to start surge
        self.drivers: dict[str, Driver] = {}
        self.rides: dict[int, Ride] = {}
        self._ids = itertools.count(1)

    def add_driver(self, driver: Driver) -> None:
        self.drivers[driver.id] = driver

    def update_location(self, driver_id: str, loc: Location) -> None:
        self.drivers[driver_id].location = loc  # one attribute store, last write wins

    def request_ride(self, rider_id, pickup, drop) -> tuple[Ride, list[Driver]]:
        ride = Ride(next(self._ids), rider_id, pickup, drop, self.pricing)
        self.rides[ride.id] = ride
        return ride, self.matcher.candidates(pickup, list(self.drivers.values()))

    def accept(self, ride_id: int, driver_id: str) -> bool:
        ride, driver = self.rides[ride_id], self.drivers[driver_id]
        with ride.lock, driver.lock:  # always ride, then driver: no deadlock
            if (ride.status is not RideStatus.REQUESTED
                    or driver.status is not DriverStatus.AVAILABLE):
                return False
            ride.move_to(RideStatus.ACCEPTED)
            ride.driver, driver.status = driver, DriverStatus.ON_TRIP
            return True

    def start(self, ride_id: int) -> None:
        ride = self.rides[ride_id]
        with ride.lock:
            ride.move_to(RideStatus.STARTED)

    def complete(self, ride_id: int, minutes: float) -> Decimal:
        ride = self.rides[ride_id]
        with ride.lock:
            ride.move_to(RideStatus.COMPLETED)
            ride.fare = ride.pricing.fare(ride.pickup.km_to(ride.drop), minutes)
            with ride.driver.lock:
                ride.driver.status, ride.driver.location = DriverStatus.AVAILABLE, ride.drop
            return ride.fare

    def cancel(self, ride_id: int) -> None:
        ride = self.rides[ride_id]
        with ride.lock:
            ride.move_to(RideStatus.CANCELLED)
            if ride.driver:
                with ride.driver.lock:
                    ride.driver.status = DriverStatus.AVAILABLE


def race(fn, arg_list) -> list:
    barrier, results = threading.Barrier(len(arg_list)), []

    def run(args):
        barrier.wait()  # release all threads at once
        results.append(fn(*args))

    threads = [threading.Thread(target=run, args=(a,)) for a in arg_list]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


if __name__ == "__main__":
    svc = RideService(NearestWithinRadius(radius_km=3), StandardFare())
    home, office = Location(12.9716, 77.5946), Location(12.9352, 77.6245)
    svc.add_driver(Driver("near", Location(12.975, 77.595)))
    svc.add_driver(Driver("far", Location(12.990, 77.600)))
    svc.add_driver(Driver("out", Location(13.200, 77.700)))  # ~28 km away
    ride, offers = svc.request_ride("r1", home, office)
    assert [d.id for d in offers] == ["near", "far"]

    assert StandardFare().fare(10, 20) == Decimal("210.00")
    assert SurgeFare(StandardFare(), "1.5").fare(10, 20) == Decimal("315.00")

    # 20 drivers race for one ride: exactly one wins
    for i in range(20):
        svc.add_driver(Driver(f"d{i}", home))
    contested, _ = svc.request_ride("r2", home, office)
    wins = race(svc.accept, [(contested.id, f"d{i}") for i in range(20)])
    assert wins.count(True) == 1 and contested.driver.status is DriverStatus.ON_TRIP

    # one driver, 20 rides at once: driver ends up on exactly one
    rides = [svc.request_ride(f"x{i}", home, office)[0] for i in range(20)]
    wins = race(svc.accept, [(r.id, "near") for r in rides])
    assert wins.count(True) == 1

    won = next(r for r in rides if r.driver)
    svc.start(won.id)
    fare = svc.complete(won.id, minutes=18)
    assert fare > Decimal("50") and svc.drivers["near"].status is DriverStatus.AVAILABLE

    svc.cancel(contested.id)  # cancel after accept frees the driver
    assert contested.driver.status is DriverStatus.AVAILABLE
    try:
        svc.start(contested.id)
        raise AssertionError("cancelled ride must not start")
    except InvalidTransition:
        pass
    print("ok")
```

## Key flows

Request and accept race:

```text
Rider      RideService      Matcher         Driver A        Driver B
  |- request ->|                |               |               |
  |            |-- candidates ->|               |               |
  |            |<--- [A, B] ----|               |               |
  |<-- ride ---|------------ offer ------------>|---- offer --->|
  |            |<--------- accept(A) -----------|               |
  |            | lock ride, lock A              |               |
  |            | REQUESTED, A free:             |               |
  |            | -> ACCEPTED    |               |               |
  |            |<----------------- accept(B) -------------------|
  |            | lock ride: ACCEPTED            |               |
  |            | return False   |               |               |
  |<--- A -----|                |               |               |
```

Complete: lock ride → `STARTED → COMPLETED` → fare from the ride's pricing snapshot → lock driver → driver `AVAILABLE` at the drop location.

## Edge cases

- No driver within radius: return an empty list; the client retries with a wider radius or shows "no cars".
- Driver goes offline between the offer and accept: `accept` sees status not AVAILABLE and returns `False`.
- Rider cancels after a driver accepted: driver is freed; a cancellation fee could apply after a grace period.
- Cancel after start: rejected by the state machine (end the trip instead).
- Stale location updates arriving out of order: carry a device timestamp and drop older ones.
- The same rider requesting twice: keep one active ride per rider (check before creating).

## Production follow-ups

**Thread safety / concurrency?** The race is "check driver free, then assign" from many drivers and many rides at once. Each ride and each driver has its own lock, always acquired ride first, then driver, so no two threads can hold them in opposite order and deadlock. Across machines the same rule becomes a conditional update: `UPDATE drivers SET status='on_trip' WHERE id=%s AND status='available'` and check the row count. See [Deadlock, livelock and starvation]({{ '/lld/deadlock-livelock-starvation/' | relative_url }}).

**Persistence and restart?** Rides and their status live in PostgreSQL; every transition is a guarded update (`WHERE status='requested'`). Driver locations are ephemeral and belong in Redis with a TTL, so a restart loses nothing important: drivers re-send location within seconds.

**Idempotency and retries?** Mobile clients retry on flaky networks. `request_ride` takes a client-generated request id so a retry returns the same ride. `accept` is already safe to repeat: a second call by the winning driver returns `False` without changing anything; return "already yours" if you want a nicer message.

**Extensibility?** New matching (ETA-based, rating-weighted, batch matching) is a new `MatchingStrategy`. New pricing (flat airport fare, pool discount) is a new `FareStrategy`, and surge composes with any of them. Vehicle types become a filter passed into the matcher.

**Scale (10×, across machines)?** The linear scan and the in-process dicts break first. Index drivers by geohash or H3 cell in Redis (`GEOADD`/`GEOSEARCH`) and shard by city, since rides never cross cities. Location updates (one every few seconds per driver) go through a stream, not the ride database.

**Observability?** Time to match (request to accept), offer acceptance rate, unfulfilled requests by area, accept conflicts per minute, and fare distribution with surge multiplier. Log every state transition with ride id, driver id and timestamp so disputes can be replayed.

**Fairness and surge?** Nearest-first starves drivers on the edge of a cluster; add idle time to the score. Compute surge per cell from the demand/supply ratio every minute, cap it, and show the multiplier before the rider confirms.

**Money precision?** Fares are `Decimal`, quantized to cents with explicit half-up rounding, and surge is applied before the final rounding so you never round twice.

## Interview follow-ups

- **Why offer to several drivers instead of one?** One-at-a-time with a 15-second timeout is fair but slow; broadcasting to a few and letting the lock pick the first acceptor cuts wait time.
- **How do you prevent a driver getting two rides?** Atomic check-and-set on the driver's status under its lock (or a conditional DB update), not a read followed by a write.
- **Where does surge come from?** A separate service computing demand/supply per cell; `RideService` just receives a `SurgeFare` wrapping the base fare.
- **How would you add pooling?** A pool ride holds several riders; matching checks detour cost against riders already on the trip. That is a new matcher and a capacity count on the driver, not a change to `Ride`'s states.
