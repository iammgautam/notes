---
title: Design an elevator system
section: lld
order: 23
phase: "Phase 5 · Intermediate"
summary: Multi-car elevator controller with State classes per car, LOOK scheduling, swappable dispatch strategies, and thread-safe hall and car calls.
---

## Requirements

Clarifying questions to ask first:

- How many floors and cars? (N floors, M cars, one controller per building.)
- Do hall buttons carry a direction? (Yes, up or down. Car buttons carry only a floor.)
- What do we optimize: average wait, travel, energy? (Average wait, with no starvation.)
- Door timing, capacity, fire mode? (Doors stay open one tick; the rest are extensions.)

**Functional**

- External (hall) request: floor plus direction. The controller assigns exactly one car.
- Internal (car) request: destination floor for that car.
- Each car serves stops in LOOK order: keep going while stops remain ahead, then reverse.
- Car states: idle, moving, doors open, maintenance. A car in maintenance takes no hall calls.
- Dispatch policy is swappable.

**Non-functional**

- Buttons are pressed from many threads while a ticker thread moves the cars.
- No request is lost; no car runs past the last floor.
- Any stop is served within one sweep up and one sweep down.

**Out of scope:** hardware drivers, weight sensors, displays, destination dispatch.

## Entities and relationships

```text
+--------------------------------+      +----------------------------+
| Controller                     | uses | DispatchStrategy (ABC)     |
| floors, cars: list[Elevator]   |----->| pick(cars, floor, dir)     |
| hall_call(floor, dir) -> id    |      +----------------------------+
| car_call(car_id, floor)        |        ^ is-a: NearestCar, LookCost
| out_of_service(car_id)         |
| tick()                         |
+--------------------------------+
        | has 1..*
        v
+--------------------------------+ has  +----------------------------+
| Elevator                       |----->| State (ABC)                |
| id, floor, direction           |      | accepts_hall_calls: bool   |
| up: set[int], down: set[int]   |      | tick(car)                  |
| add_stop(floor, dir)           |      +----------------------------+
| should_stop(), arrive()        |        ^ is-a: Idle, Moving,
| next_direction()  # LOOK       |        |       DoorsOpen, Maintenance
+--------------------------------+
        | uses
        v
  Direction (enum: UP, DOWN, IDLE)
```

A car keeps two stop sets. `up` holds stops to serve while going up (hall-up calls and car calls above it), `down` the rest. A car going up skips a hall-down call on its way and serves it on the way back, which is what the person waiting expects.

## Patterns and why

| Pattern | Where | Why |
|---|---|---|
| State | `Idle`, `Moving`, `DoorsOpen`, `Maintenance` | Each state's tick lives in one class; no `if state == ...` chains to edit when fire mode arrives |
| Strategy | `DispatchStrategy` | Nearest car vs LOOK cost vs peak-hour rules swap at runtime; the controller never changes |
| Facade | `Controller` | Buttons and the ticker talk to one object that owns the lock |

### Scheduling choices

| Algorithm | Behavior | Verdict |
|---|---|---|
| FCFS | Serve in arrival order | Zigzags, long waits |
| SSTF / nearest | Closest stop next | Starves far floors |
| SCAN | Sweep to the end floor, reverse | Fair, wastes travel at the ends |
| LOOK | Sweep to the last stop, reverse | Standard answer, used per car here |

Two separate decisions: **which car** gets a hall call (dispatch strategy) and **in what order a car serves its stops** (LOOK, built into the states).

## Code

```python
import random
import threading
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from enum import Enum


class Direction(Enum):
    UP = 1
    DOWN = -1
    IDLE = 0


class State(ABC):
    accepts_hall_calls = True

    @abstractmethod
    def tick(self, car: "Elevator") -> None: ...


class Idle(State):
    def tick(self, car):
        stops = car.up | car.down
        if car.floor in stops:
            car.arrive()
        elif stops:
            target = min(stops, key=lambda f: abs(f - car.floor))
            car.direction = Direction.UP if target > car.floor else Direction.DOWN
            car.state = Moving()


class Moving(State):
    def tick(self, car):
        car.floor += car.direction.value
        if car.should_stop():
            car.arrive()


class DoorsOpen(State):
    def tick(self, car):  # doors close after one tick
        car.direction = car.next_direction()
        car.state = Idle() if car.direction is Direction.IDLE else Moving()


class Maintenance(State):
    accepts_hall_calls = False

    def tick(self, car):
        pass


class Elevator:
    def __init__(self, id_: int, floor: int = 0):
        self.id, self.floor = id_, floor
        self.direction = Direction.IDLE
        self.state: State = Idle()
        self.up: set[int] = set()    # serve while moving up
        self.down: set[int] = set()  # serve while moving down
        self.served: list[int] = []

    def add_stop(self, floor: int, direction: Direction) -> None:
        if direction is Direction.IDLE:  # car call: side depends on position
            direction = Direction.UP if floor >= self.floor else Direction.DOWN
        (self.up if direction is Direction.UP else self.down).add(floor)

    def _ahead(self, d: Direction) -> bool:
        return any((f - self.floor) * d.value > 0 for f in self.up | self.down)

    def _sets(self) -> tuple[set[int], set[int]]:
        d = self.direction
        return (self.up, self.down) if d is Direction.UP else (self.down, self.up)

    def should_stop(self) -> bool:
        here, other = self._sets()
        # Stop for calls in our direction, or for anything at the turnaround floor.
        return self.floor in here or (self.floor in other and not self._ahead(self.direction))

    def arrive(self) -> None:
        here, other = self._sets()
        here.discard(self.floor)
        if self.direction is Direction.IDLE or not self._ahead(self.direction):
            other.discard(self.floor)
        self.served.append(self.floor)
        self.state = DoorsOpen()

    def next_direction(self) -> Direction:  # LOOK: keep going, else reverse
        d = self.direction
        order = [Direction.UP, Direction.DOWN] if d is Direction.IDLE else [d, Direction(-d.value)]
        return next((x for x in order if self._ahead(x)), Direction.IDLE)


class DispatchStrategy(ABC):
    @abstractmethod
    def pick(self, cars: list[Elevator], floor: int, direction: Direction) -> Elevator: ...


class NearestCar(DispatchStrategy):
    def pick(self, cars, floor, direction):
        return min(cars, key=lambda c: abs(c.floor - floor))


class LookCost(DispatchStrategy):
    """Floors a car travels under LOOK before it reaches the call."""

    def cost(self, c: Elevator, floor: int, direction: Direction) -> int:
        d = c.direction
        if d is Direction.IDLE:
            return abs(c.floor - floor)
        if direction is d and (floor - c.floor) * d.value >= 0:
            return abs(floor - c.floor)  # on its way
        turn = max(c.up | c.down | {c.floor}, key=lambda f: (f - c.floor) * d.value)
        return abs(turn - c.floor) + abs(turn - floor)

    def pick(self, cars, floor, direction):
        return min(cars, key=lambda c: self.cost(c, floor, direction))


class Controller:
    def __init__(self, floors: int, cars: list[Elevator], strategy: DispatchStrategy):
        self.floors, self.cars, self.strategy = floors, cars, strategy
        # Simplification: one lock for buttons and ticks; a request queue if dispatch gets slow
        self._lock = threading.Lock()

    def _check(self, floor: int) -> None:
        if not 0 <= floor < self.floors:
            raise ValueError(f"no floor {floor}")

    def hall_call(self, floor: int, direction: Direction) -> int:
        """External request. Returns the id of the car that will come."""
        self._check(floor)
        if direction is Direction.IDLE or floor + direction.value not in range(self.floors):
            raise ValueError("bad direction")
        with self._lock:
            ok = [c for c in self.cars if c.state.accepts_hall_calls]
            for c in ok:  # pressed twice: same car
                if floor in (c.up if direction is Direction.UP else c.down):
                    return c.id
            if not ok:
                raise RuntimeError("no car in service")
            car = self.strategy.pick(ok, floor, direction)
            car.add_stop(floor, direction)
            return car.id

    def car_call(self, car_id: int, floor: int) -> None:
        """Internal request from a button inside the car."""
        self._check(floor)
        with self._lock:
            self.cars[car_id].add_stop(floor, Direction.IDLE)

    def out_of_service(self, car_id: int) -> None:
        with self._lock:
            self.cars[car_id].state = Maintenance()

    def tick(self) -> None:
        with self._lock:
            for c in self.cars:
                c.state.tick(c)


if __name__ == "__main__":
    def run(ctl, ticks):
        for _ in range(ticks):
            ctl.tick()
            assert all(0 <= c.floor < ctl.floors for c in ctl.cars)

    # LOOK: a DOWN call at 3 is skipped on the way up and served on the way down.
    car = Elevator(0)
    ctl = Controller(10, [car], LookCost())
    ctl.car_call(0, 7)
    run(ctl, 2)  # Idle -> Moving, then floor 1
    ctl.hall_call(3, Direction.DOWN)
    ctl.car_call(0, 5)
    run(ctl, 30)
    assert car.served == [5, 7, 3], car.served
    assert isinstance(car.state, Idle) and not (car.up or car.down)

    # Nearest car picks A (1 floor away, heading down); LOOK cost picks idle B.
    def setup(strategy):
        ctl = Controller(10, [Elevator(0, floor=2), Elevator(1, floor=6)], strategy)
        ctl.car_call(0, 0)
        ctl.tick()
        return ctl

    assert setup(NearestCar()).hall_call(3, Direction.UP) == 0
    assert setup(LookCost()).hall_call(3, Direction.UP) == 1

    ctl = Controller(10, [Elevator(0), Elevator(1, floor=9)], NearestCar())
    ctl.out_of_service(1)
    assert ctl.hall_call(8, Direction.DOWN) == 0
    for bad in [(10, Direction.UP), (9, Direction.UP), (0, Direction.DOWN)]:
        try:
            ctl.hall_call(*bad)
            raise AssertionError(bad)
        except ValueError:
            pass

    # Stress: 8 threads press random buttons while the main thread ticks.
    ctl = Controller(20, [Elevator(i) for i in range(3)], LookCost())

    def rider(seed):
        rng, pressed = random.Random(seed), set()
        for _ in range(300):
            f = rng.randrange(20)
            if rng.random() < 0.5:
                ctl.hall_call(f, Direction.UP if f < 19 else Direction.DOWN)
            else:
                ctl.car_call(rng.randrange(3), f)
            pressed.add(f)
        return pressed

    with ThreadPoolExecutor(8) as pool:
        futures = [pool.submit(rider, s) for s in range(8)]
        while not all(f.done() for f in futures):
            run(ctl, 1)
    run(ctl, 200)  # drain
    pressed = set().union(*(f.result() for f in futures))
    assert pressed <= set().union(*(c.served for c in ctl.cars))
    assert all(not c.up and not c.down for c in ctl.cars)
    print("elevator ok")
```

## Key flows

```text
Button    Controller              DispatchStrategy        Elevator B
  | hall_call(3, DOWN)                    |                     |
  |-------->| lock                        |                     |
  |         | already assigned? no        |                     |
  |         | pick(in-service cars) ----->|                     |
  |         |<------------ car B ---------|                     |
  |         | add_stop(3, DOWN) -------------------------------->| down={3}
  |<-- B ---| unlock                      |                     |

Ticker    Controller              Elevator (state = Moving)
  | tick()  |                           |
  |-------->| lock; for each car ------>| floor += direction
  |         |                           | should_stop()? -> arrive()
  |         |                           |   remove stop, state = DoorsOpen
  |         | unlock                    |
  |         |   next tick: DoorsOpen -> next_direction() -> Moving/Idle
```

## Edge cases

- Up call from the top floor or down call from the ground floor: rejected.
- Same hall button pressed twice: the second press returns the car already assigned.
- Car going up passes a down call: not served now, served on the way down or as the turnaround stop.
- Car call for the floor the car is idle at: the doors open on the next tick.
- No car in service: `RuntimeError` here; production queues the call and alerts.
- Car sent to maintenance with pending stops: this sketch freezes them. Production adds a `Draining` state that finishes car calls and hands hall calls back to the controller.
- Floor out of range: `ValueError` before any lock is taken.

## Production follow-ups

**How is it thread-safe?** One lock covers dispatch and tick, so the strategy always sees consistent positions and the critical sections are microseconds. The alternative is a single controller thread consuming a `queue.Queue` of button events (Java's `BlockingQueue`), so car state has exactly one writer and needs no lock. See [Producer-consumer]({{ '/lld/producer-consumer/' | relative_url }}).

**What happens on restart?** Car positions come from floor sensors, never from memory, so the controller rebuilds `floor` from hardware at boot. Hall lamps stay lit, and the panels re-send lit buttons; car calls can be persisted in a small local store or riders press again.

**How are retries handled?** Presses are idempotent by construction: stops are sets, and the duplicate check returns the assigned car. A panel retrying over a flaky bus resends `(floor, direction)`, which is its own natural key.

**How do you add fire mode or a peak-hour policy?** Fire service is a new `State` that sends the car to the lobby and refuses calls. Morning-rush lobby parking is a new `DispatchStrategy`. Neither touches existing classes.

**What breaks at 10× or across machines?** A building is small: under 100 floors and a few dozen cars, and `pick` is `O(cars)` per call. Large towers split into zones (low-rise and high-rise banks), each with its own controller. A fleet of buildings is independent controllers reporting to a central monitoring service; nothing needs cross-building locks.

**What do you measure?** Wait time from press to doors opening (p50 and p95 per floor), ride time, trips per car for wear leveling, door-open duration, calls with no car in service. Log state transitions with car id and floor. A car in `Moving` whose floor stops changing is a sensor or motor fault alarm.

**Can anyone starve?** LOOK serves any stop within one sweep each way. Nearest-car dispatch with heavy traffic near the lobby can keep sending cars away from the top floors; add the age of the call to the cost so old calls win.

## Interview follow-ups

- **SCAN vs LOOK?** SCAN always travels to the end floor; LOOK reverses at the last stop. Both bound wait time, LOOK travels less.
- **Why State classes instead of an enum?** With an enum every method branches on it, and fire mode edits each branch. With classes it is one new class.
- **Capacity?** Track load; when full, `Moving` skips hall stops (not car calls), and the controller reassigns those calls to another car.
- **Destination dispatch?** Riders enter the target floor in the lobby, so the hall call carries a destination and the cost function groups riders going to nearby floors.
- **How do you test without real time?** `tick()` is the clock. Tests drive ticks deterministically, as the demo does.
