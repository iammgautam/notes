---
title: Design an elevator system
section: lld
order: 12
summary: Multi-car elevator controller with elevator state, the SCAN/LOOK algorithm, pluggable dispatch strategies, and a thread-safe request flow.
---

## Requirements

- Building with `N` floors and `M` elevators.
- **External requests:** hall button on a floor with direction (up/down).
- **Internal requests:** destination button inside a car.
- Dispatcher assigns each hall call to one elevator.
- Each elevator serves its stops efficiently (don't zigzag).
- Capacity / weight limit, door open/close, maintenance mode.
- Out of scope: real hardware, display UI.

## Entities

| Entity | Responsibility |
|--------|----------------|
| `Direction` | `UP`, `DOWN`, `IDLE` |
| `ElevatorState` | `MOVING`, `STOPPED`, `DOORS_OPEN`, `MAINTENANCE` |
| `Request` | Floor + direction (hall) or floor only (car) |
| `Elevator` | Current floor, direction, state, up/down stop sets, `step()` |
| `DispatchStrategy` | Choose an elevator for a hall call |
| `ElevatorController` | Receives requests, dispatches, ticks elevators |

## Class sketch

```python
import heapq
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class Direction(Enum):
    UP = 1
    DOWN = -1
    IDLE = 0


class State(Enum):
    MOVING = "moving"
    STOPPED = "stopped"
    MAINTENANCE = "maintenance"


@dataclass(frozen=True)
class Request:
    floor: int
    direction: Direction = Direction.IDLE   # IDLE for in-car requests


class Elevator:
    def __init__(self, id_: int, capacity: int = 8):
        self.id, self.capacity = id_, capacity
        self.floor, self.direction, self.state = 0, Direction.IDLE, State.STOPPED
        self.up: list[int] = []      # min-heap of floors above
        self.down: list[int] = []    # max-heap (negated) of floors below
        self.lock = threading.Lock()

    def add_stop(self, floor: int) -> None:
        with self.lock:
            if floor > self.floor and floor not in self.up:
                heapq.heappush(self.up, floor)
            elif floor < self.floor and -floor not in self.down:
                heapq.heappush(self.down, -floor)
            if self.direction is Direction.IDLE and floor != self.floor:
                self.direction = Direction.UP if floor > self.floor else Direction.DOWN

    def step(self) -> None:
        """Advance one floor using LOOK: keep going while stops remain ahead."""
        with self.lock:
            if self.state is State.MAINTENANCE:
                return
            if self.direction is Direction.UP and not self.up:
                self.direction = Direction.DOWN if self.down else Direction.IDLE
            elif self.direction is Direction.DOWN and not self.down:
                self.direction = Direction.UP if self.up else Direction.IDLE
            if self.direction is Direction.IDLE:
                self.state = State.STOPPED
                return
            self.floor += self.direction.value
            self.state = State.MOVING
            if self.up and self.up[0] == self.floor:
                heapq.heappop(self.up)
                self.state = State.STOPPED          # open doors
            elif self.down and -self.down[0] == self.floor:
                heapq.heappop(self.down)
                self.state = State.STOPPED

    def pending(self) -> int:
        return len(self.up) + len(self.down)
```

## Scheduling strategies

| Algorithm | Behavior | Notes |
|-----------|----------|-------|
| FCFS | Serve requests in arrival order | Simple, lots of zigzag |
| SSTF | Nearest stop next | Starvation of far floors |
| SCAN | Go end to end, serve along the way | Fair, wasteful at ends |
| LOOK | Like SCAN but reverse at last request | Standard answer |

Within one car, use **LOOK** (above). Across cars, the **dispatcher** decides.

```python
class DispatchStrategy(ABC):
    @abstractmethod
    def pick(self, elevators: list[Elevator], req: Request) -> Elevator: ...


class NearestSuitable(DispatchStrategy):
    """Prefer cars moving toward the floor in the same direction, else idle, else least busy."""

    def cost(self, e: Elevator, r: Request) -> int:
        dist = abs(e.floor - r.floor)
        if e.direction is Direction.IDLE:
            return dist
        heading_to = (r.floor - e.floor) * e.direction.value >= 0
        if heading_to and e.direction is r.direction:
            return dist
        return 1_000 + dist + e.pending()       # penalize wrong direction

    def pick(self, elevators, req):
        ok = [e for e in elevators if e.state is not State.MAINTENANCE]
        return min(ok, key=lambda e: self.cost(e, req))


class LeastLoaded(DispatchStrategy):
    def pick(self, elevators, req):
        return min(elevators, key=Elevator.pending)
```

## Controller and flows

```python
class ElevatorController:
    def __init__(self, n_elevators: int, strategy: DispatchStrategy):
        self.elevators = [Elevator(i) for i in range(n_elevators)]
        self.strategy = strategy

    def hall_call(self, floor: int, direction: Direction) -> Elevator:
        e = self.strategy.pick(self.elevators, Request(floor, direction))
        e.add_stop(floor)
        return e

    def car_call(self, elevator_id: int, floor: int) -> None:
        self.elevators[elevator_id].add_stop(floor)

    def tick(self) -> None:               # called by a scheduler thread
        for e in self.elevators:
            e.step()
```

- **Hall call:** controller → strategy picks car → `add_stop`.
- **Car call:** goes straight to that car.
- **Tick:** each car advances one floor, stops if it reached a stop.

## Patterns used

| Pattern | Where | Why |
|---------|-------|-----|
| Strategy | `DispatchStrategy` | Peak-hour vs normal dispatch, swap at runtime |
| State | `State` enum (or state classes) | Behavior differs when moving, stopped, in maintenance |
| Observer | Floor displays, hall lanterns | Subscribe to elevator position changes |
| Command | `Request` objects | Queue, log, replay requests |
| Singleton | Controller | One per building; inject it |

For a richer state model, turn `State` into classes (`Idle`, `MovingUp`, `MovingDown`, `DoorsOpen`, `Maintenance`) each implementing `on_request()` and `on_tick()`. See [Behavioral patterns]({{ '/lld/behavioral-patterns/' | relative_url }}).

## Concurrency

- Requests arrive from many threads (buttons) while a ticker thread moves cars.
- Each `Elevator` guards its stop heaps with its own lock; `step()` and `add_stop()` both take it.
- Dispatcher reads car state without locks: a slightly stale view is fine, the decision is a heuristic.
- Alternative: one `queue.Queue` of requests consumed by the controller thread (single writer, no locks on cars).

## Extensions interviewers ask

- **Capacity:** track load; skip hall calls when full, the dispatcher reassigns.
- **Emergency / fire mode:** all cars go to ground floor, ignore hall calls (a new state).
- **VIP / service elevator:** filter in dispatch strategy.
- **Destination dispatch:** riders enter target floor in the lobby; group riders by destination.
- **Peak-hour strategy:** morning, park idle cars at lobby; evening, spread across floors.
- **Door safety:** door sensor blocks transition from `DOORS_OPEN` to `MOVING`.
