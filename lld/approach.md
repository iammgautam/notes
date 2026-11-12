---
title: How to approach an LLD interview
section: lld
order: 1
phase: "Start here"
summary: A minute-by-minute plan for 45–60 minute LLD and machine-coding rounds, what interviewers score, and the mistakes that sink candidates.
---

LLD and machine-coding rounds test whether you can turn a vague prompt into classes with clear jobs, write working code for the core flows, and keep the design open to change. A small core that runs beats a complete design that doesn't.

| Round | What you produce | Weighted toward |
|---|---|---|
| LLD discussion | Class diagram, key methods, some code | Modeling, patterns, trade-offs |
| Machine coding | Runnable code for the core flows | Correctness, code quality, a working demo |

## What interviewers score

| Signal | What they look for | Red flag |
|---|---|---|
| Responsibilities | Each class has one job; behavior sits next to the data it changes | A `System` god class; callers poking fields (`spot.vehicle = v`) |
| Extensibility | A new type or rule is a new class | `if kind == "car"` repeated in three places |
| Correctness | Invariants enforced, failure paths handled, shared state safe | Double booking, negative balance, float money |
| Code quality | Clear names, small methods, enums, type hints | Getter/setter boilerplate, magic strings |
| Communication | Thinks aloud, states trade-offs, uses hints | 20 minutes of silent typing |

## The plan

| Step | 45 min | 60 min | Output |
|---|---|---|---|
| 1. Clarify | 0–4 | 0–5 | In-scope / out-of-scope list |
| 2. Use cases | 4–7 | 5–8 | 4–6 public method signatures |
| 3. Entities | 7–10 | 8–12 | Classes and enums from the nouns |
| 4. Class diagram | 10–15 | 12–18 | Boxes, relationships, key methods |
| 5. Patterns | 15–17 | 18–21 | Each variation point named |
| 6. Code core flows | 17–36 | 21–45 | Happy path plus one failure path |
| 7. Test with a demo | 36–40 | 45–50 | `__main__` block with asserts |
| 8. Production follow-ups | 40–45 | 50–60 | Concurrency, persistence, scale |

> **Rule of thumb:** If you are not writing classes by minute 17, you are over-talking.

### 1. Clarify

- **Actors**: who calls what (user, admin, a scheduler).
- **Scope**: what is out (payments, auth, UI, persistence). Write the list where the interviewer can see it.
- **Concurrency**: one user at a time, or many grabbing the same seat or spot?
- **Variation hints**: "pricing may change" or "more vehicle types later" means a strategy or a factory.

### 2. Use cases

Turn each goal into a signature: `park(vehicle) -> Ticket`, `unpark(ticket_id) -> Receipt`. This is the public API and your contract with the interviewer.

### 3. Entities

- Nouns become candidate classes; fixed sets become `Enum` (`SpotType`, `BookingStatus`).
- A noun that is only a value becomes a field (`price`) or a frozen dataclass (`Money`).
- Separate **entities** (identity and state: `Booking`) from **services** (orchestrate a use case: `BookingService`).

### 4. Class diagram

Boxes, arrows, multiplicity, only the methods from step 2. See [UML for interviews]({{ '/lld/uml-for-interviews/' | relative_url }}) for the notation.

### 5. Patterns

Only at real variation points. One sentence each: "Pricing varies by vehicle type, so `PricingStrategy` is a strategy."

### 6. Code core flows

- Enums and entities first, then the service, then one flow end to end.
- In-memory dicts behind repository classes. Behavior methods, not setters.
- Validate input and raise a specific exception on the failure path.

### 7. Test with a demo

A `__main__` block that runs the main flow and asserts the results, including one failure. Running it in front of the interviewer is the strongest correctness signal you have.

### 8. Production follow-ups

Have one-line answers ready for thread safety (lock the check-then-act), persistence (repository interface, DB unique constraints), idempotency (request IDs), extensibility (new class, no edits), scale (what moves to a DB or queue) and observability (what you log and measure). Every case study in this section ends with these.

## Most problems reduce to Strategy + Factory + Composition

| Piece | Job | Typical spot |
|---|---|---|
| Composition | An aggregate owns its parts | `ParkingLot` → `Floor` → `Spot`; `Order` → `OrderItem` |
| Strategy | A swappable rule | Pricing, spot allocation, split type, rate-limit algorithm |
| Factory | Build the right subtype from input | Vehicle from a type string, payment method, notification channel |

Add **State** when an entity has a lifecycle with different rules per state (vending machine, elevator, order) and **Observer** when other parts react to events (notifications, display boards). More in [Patterns in practice]({{ '/lld/patterns-in-practice/' | relative_url }}).

All three in one skeleton:

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class VehicleType(Enum):
    BIKE = "bike"
    CAR = "car"


@dataclass
class Spot:                         # entity owns its state change
    id: str
    type: VehicleType
    plate: str | None = None

    def occupy(self, plate: str) -> None:
        if self.plate is not None:
            raise ValueError(f"spot {self.id} is taken")
        self.plate = plate


class PricingStrategy(ABC):         # strategy: the variation point
    @abstractmethod
    def price(self, hours: int) -> int: ...


class HourlyRate(PricingStrategy):
    def __init__(self, per_hour: int):
        self.per_hour = per_hour

    def price(self, hours: int) -> int:
        return self.per_hour * max(hours, 1)


PRICING = {
    VehicleType.BIKE: HourlyRate(10),
    VehicleType.CAR: HourlyRate(30),
}


def pricing_for(vt: VehicleType) -> PricingStrategy:  # factory: a lookup
    return PRICING[vt]


@dataclass
class ParkingLot:                   # composition: owns its spots
    spots: list[Spot] = field(default_factory=list)

    def park(self, plate: str, vt: VehicleType) -> Spot:
        for spot in self.spots:
            if spot.type is vt and spot.plate is None:
                spot.occupy(plate)
                return spot
        raise LookupError(f"no free {vt.value} spot")


if __name__ == "__main__":
    lot = ParkingLot([Spot("B1", VehicleType.BIKE),
                      Spot("C1", VehicleType.CAR)])
    assert lot.park("KA01", VehicleType.CAR).id == "C1"
    assert pricing_for(VehicleType.CAR).price(2) == 60
    try:
        lot.park("KA02", VehicleType.CAR)
        raise AssertionError("expected LookupError")
    except LookupError:
        pass
```

## Common mistakes

- Coding before scope is agreed, or drawing for 20 minutes and never coding.
- One god class (`ParkingSystem`) holding every method and every dict.
- Type switches spread across the code instead of polymorphism or a lookup table.
- Public mutable state changed from outside the owning class.
- Deep inheritance for reuse (`ElectricSUV(SUV(Car(Vehicle)))`).
- Patterns for show: a factory with one product, an observer with one listener.
- `float` for money; no thought for two users grabbing the same resource.
- No demo, so nobody knows whether the code works.

## Interview questions

- **"Why not put this in one class?"** Each class should have one reason to change. Pricing changes for business reasons, allocation for layout reasons; splitting them means one change touches one class.
- **"How would you add a new vehicle type?"** Add an enum value and a pricing entry. `ParkingLot` does not change. If you would have to edit an `if/elif`, say so and refactor it into a lookup.
- **"How do you know it works?"** Run the demo. Assert the happy path and at least one rejection (full lot, double booking, insufficient funds).
- **"You have 10 minutes left."** Stop adding features. Make the core flow run, then talk through concurrency and persistence instead of coding them.
