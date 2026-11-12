---
title: Strategy, Factory and Composition in practice
section: lld
order: 9
phase: "Phase 3 · Design patterns"
summary: Most LLD problems are Strategy, Factory and composition plus State or Observer. Map the requirement to the pattern, and know when to stop.
---

Strip the domain away and most machine-coding problems have the same skeleton: **objects that own other objects** (composition), **one or two behaviors that vary by type** (Strategy), **something that turns an input into the right variant** (Factory), and then either **a lifecycle** (State) or **an event with many reactions** (Observer). Spot those five and you have 80% of the design; the other patterns show up occasionally.

## The five building blocks

| Block | Question it answers | Typical code |
|---|---|---|
| Composition | What does this object own or use? | `ParkingLot` has `Floor`s has `Spot`s |
| Strategy | What varies by type, config or time? | `FeePolicy`, `SplitStrategy`, `DispatchStrategy` |
| Factory | Given this input, which variant? | `dict[VehicleType, FeePolicy]` |
| State | Does the same call act differently now? | `Idle`, `HasMoney`, `Dispensing` |
| Observer | Who else needs to know this happened? | `bus.publish("seat_released", seat)` |

Composition is the default glue: prefer "has a `FeePolicy`" over "is an `HourlyParkingLot`". Inheritance is for real is-a relationships with shared behavior, such as `Car` is a `Vehicle`.

## One example, three patterns

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class VehicleType(Enum):
    BIKE = "bike"
    CAR = "car"


class FeePolicy(ABC):                               # Strategy
    @abstractmethod
    def fee(self, hours: int) -> int: ...


class Hourly(FeePolicy):
    def __init__(self, rate: int) -> None:
        self.rate = rate

    def fee(self, hours: int) -> int:
        return max(1, hours) * self.rate


class DailyCap(FeePolicy):
    def __init__(self, rate: int, cap: int) -> None:
        self.rate, self.cap = rate, cap

    def fee(self, hours: int) -> int:
        return min(max(1, hours) * self.rate, self.cap)


FEES: dict[VehicleType, FeePolicy] = {              # Factory, as a registry
    VehicleType.BIKE: Hourly(10),
    VehicleType.CAR: DailyCap(rate=30, cap=200),
}


@dataclass
class Spot:
    spot_id: str
    kind: VehicleType
    taken: bool = False


@dataclass
class ParkingLot:                                   # Composition
    spots: list[Spot]
    fees: dict[VehicleType, FeePolicy] = field(default_factory=lambda: dict(FEES))

    def park(self, kind: VehicleType) -> Spot:
        free = (s for s in self.spots if s.kind is kind and not s.taken)
        spot = next(free, None)
        if spot is None:
            raise LookupError(f"no free {kind.value} spot")
        spot.taken = True
        return spot

    def leave(self, spot: Spot, hours: int) -> int:
        spot.taken = False
        return self.fees[spot.kind].fee(hours)


lot = ParkingLot([Spot("B1", VehicleType.BIKE), Spot("C1", VehicleType.CAR)])
car = lot.park(VehicleType("car"))          # input string -> enum -> variant
assert lot.leave(car, hours=10) == 200
assert lot.leave(lot.park(VehicleType.BIKE), hours=0) == 10
```

Adding trucks is one enum member, one `Spot` kind and one registry entry. `ParkingLot` does not change: that is the open/closed principle, and it is the sentence interviewers want to hear. Spot allocation (the `free` scan) is the next seam: if "nearest to the gate" or "fill floor by floor" comes up, it becomes a second strategy. Full case study: [Parking lot]({{ '/lld/parking-lot/' | relative_url }}).

## Reading the requirement

Patterns are answers to specific phrases. Listen for these while clarifying.

| The requirement says | Reach for | Example |
|---|---|---|
| "varies by type", "multiple ways to", "may change later" | Strategy | Fee per vehicle type |
| "create the right kind from input or config" | Factory | `"card"` to `CardProcessor` |
| "behavior depends on current mode", "can't X while Y" | State | Vending machine, order |
| "notify", "many listeners", "when X, also do Y and Z" | Observer | Seat released: waitlist, email |
| "tree", "nested", "folder inside folder" | Composite | File system, menus |
| "wrap without changing", "add-ons", "stackable extras" | Decorator | Toppings, retry, logging |
| "undo", "redo", "queue", "replay", "audit" | Command | Editor, job queue |
| "pass along until handled", "series of checks" | Chain of responsibility | Approvals, middleware |
| "third-party API with a different shape" | Adapter | Payment provider SDKs |
| "one simple call over several subsystems" | Facade | `BookingService.book()` |
| "lazy", "cache in front", "check permission first" | Proxy | Report cache |
| "same steps, one step differs" | Template method | CSV vs JSON importer |
| "many optional parameters" | Builder or dataclass defaults | Query, HTTP config |
| "exactly one shared instance" | Module-level instance, injected | Config, logger |

## Classic problems mapped to patterns

| Problem | Patterns | Why |
|---|---|---|
| [Parking lot]({{ '/lld/parking-lot/' | relative_url }}) | Strategy, Factory, Composition | Fee and allocation vary by vehicle type; lot has floors has spots |
| [Elevator system]({{ '/lld/elevator-system/' | relative_url }}) | State, Strategy, Command | Car is idle, moving or doors-open; dispatch (nearest, SCAN) is pluggable; requests are queued objects |
| [Vending machine]({{ '/lld/vending-machine/' | relative_url }}) | State, Strategy | The same button acts differently in Idle, HasMoney, Dispensing; change-making varies |
| [Rate limiter]({{ '/lld/rate-limiter/' | relative_url }}) | Strategy, Factory, Proxy | Token bucket vs sliding window chosen from config; limiter sits in front of the handler |
| [Notification system]({{ '/lld/notification-system/' | relative_url }}) | Strategy (or Bridge), Factory, Observer, Decorator | Channel varies; events fan out; retry wraps sending |
| [Splitwise]({{ '/lld/splitwise/' | relative_url }}) | Strategy, Factory | Equal, exact and percent splits validate and compute differently |
| [Logging framework]({{ '/lld/logging-framework/' | relative_url }}) | Chain of responsibility, Strategy, Observer, Singleton | Level filters pass records along; formatter varies; many sinks; one root logger |
| [Chess]({{ '/lld/chess/' | relative_url }}) | Strategy, Factory, Command, State | Each piece has its own move rule; setup creates pieces; moves undo; check and checkmate status |
| [Payment gateway]({{ '/lld/payment-gateway/' | relative_url }}) | Adapter, Strategy, State, Chain of responsibility | Provider SDKs adapted; route by method; payment lifecycle; fraud checks in a chain |
| [LRU cache]({{ '/lld/lru-cache/' | relative_url }}) | Composition, Strategy, Proxy | Dict plus linked list; eviction policy pluggable; cache fronts a slow store |
| [Movie ticket booking]({{ '/lld/movie-ticket-booking/' | relative_url }}) | State, Strategy, Observer, Facade | Seat available, locked, booked; pricing varies; waitlist hears releases |
| [In-memory file system]({{ '/lld/in-memory-file-system/' | relative_url }}) | Composite, Iterator | Directories hold files and directories; walk without exposing the tree |
| [Task scheduler]({{ '/lld/task-scheduler/' | relative_url }}) | Command, Strategy, Observer | Tasks are queued objects; ordering policy varies; completion callbacks |
| [ATM]({{ '/lld/atm/' | relative_url }}) | State, Chain of responsibility, Facade | Idle, card inserted, authenticated; notes dispensed 500, then 200, then 100 |
| [Shopping cart]({{ '/lld/shopping-cart/' | relative_url }}) | Strategy, Chain of responsibility | Discount rules vary and apply in order |
| [Pub-sub broker]({{ '/lld/pub-sub-broker/' | relative_url }}) | Observer, Strategy | Subscribers per topic; delivery semantics vary |
| [Ride booking]({{ '/lld/ride-booking/' | relative_url }}) | Strategy, State, Observer | Matching and surge pricing vary; trip lifecycle; location updates |
| [Auction system]({{ '/lld/auction-system/' | relative_url }}) | Observer, State, Strategy | Outbid alerts; auction open or closed; English vs sealed-bid rules |
| [Hotel booking]({{ '/lld/hotel-booking/' | relative_url }}) | State, Strategy, Facade | Reservation lifecycle; seasonal pricing; one booking entry point |
| [Library management]({{ '/lld/library-management/' | relative_url }}) | State, Strategy, Observer | Copy available, borrowed, reserved; fine policy; notify on return |
| [Tic-tac-toe]({{ '/lld/tic-tac-toe/' | relative_url }}) | Strategy, Composition | Player is human or bot; no factory needed |
| [Snake and ladder]({{ '/lld/snake-and-ladder/' | relative_url }}) | Composition, Strategy | Board holds jumps; dice swappable; keep it small |

Notice how rarely Abstract factory, Prototype, Bridge or Template method appear. Know them; don't lead with them.

## The Python-native versions

Interviewers accept these, and they are less code than the textbook shape.

| Textbook shape | Python-native | Use the class when |
|---|---|---|
| Strategy interface + classes | A function, `Callable[[int], int]` | It has config or state |
| Factory class | A `dict` from enum to class or instance | Creation needs logic or validation |
| Singleton class | A module-level instance | Creation must be lazy |
| Builder | `@dataclass(kw_only=True)` with defaults | The object is built in steps |
| Iterator class | A generator | Iteration has its own state to expose |

## Over-engineering warnings

- **One variant, no pattern.** With one fee rule, write the method. Say "if pricing starts to vary, this becomes a Strategy": naming the seam earns the credit without the code.
- **An ABC with one implementation**, a factory with one product, an abstract factory with one family: delete them.
- **State classes for two states and one transition.** An `Enum` and a guard clause are enough.
- **Observer with one listener.** Call the method directly.
- **Singleton by reflex.** Create the object once in `main` and pass it in.
- **Pattern names in class names.** `FeePolicy`, not `ParkingFeeStrategyFactoryImpl`. Name the domain idea.
- **Time.** In a 45-minute round, a working happy path beats a beautiful hierarchy that never runs. Get the core flow running, then add patterns where the interviewer pushes.

> **Rule of thumb:** For every pattern in your design, you should be able to say which requirement it serves and which change it makes cheap. If you can't, remove it.

See [How to approach an LLD interview]({{ '/lld/approach/' | relative_url }}) for the step where patterns enter, and the [behavioral]({{ '/lld/behavioral-patterns/' | relative_url }}), [creational]({{ '/lld/creational-patterns/' | relative_url }}) and [structural]({{ '/lld/structural-patterns/' | relative_url }}) notes for each pattern's code.

## Interview questions

- **Why Strategy here?** "Fees vary by vehicle type and you said pricing may change. A new rule is one class and one registry entry; `ParkingLot` is untouched."
- **How do you add a new vehicle type?** Add an enum member, a spot kind and a registry entry. No `if/elif` to edit, because the variation lives in the registry.
- **Composition or inheritance?** Inheritance for a true is-a with shared behavior. Composition for behavior that varies or gets swapped, which avoids `HourlyCarLot`, `FlatBikeLot`, and so on.
- **Strategy or State?** Strategy is chosen from outside and stays put. State changes itself as events arrive; if the requirement has a status field with transitions, it is State.
- **Isn't this over-engineered?** Be ready to collapse it: "With one rule I would inline it. I added the seam because you said pricing changes per city."
