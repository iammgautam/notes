---
title: How to approach an LLD interview
section: lld
order: 1
summary: An 8-step script for machine-coding and LLD rounds, with a 45-minute time budget and what interviewers actually score.
---

LLD rounds test whether you can turn a fuzzy problem into clean, extensible classes and working core code. The design matters more than finishing every feature.

## What gets scored

- **Requirement handling**: you scope before you code.
- **Modeling**: right entities, right responsibilities, sensible relationships.
- **Code quality**: readable, small classes, SOLID, no god objects.
- **Extensibility**: new requirement = new class, not edits everywhere.
- **Correctness under concurrency** when the problem has shared state (booking, parking, inventory).

## The 8 steps

| # | Step | Output |
|---|------|--------|
| 1 | Clarify | Scope list: in / out, scale, single vs multi-threaded |
| 2 | Use cases | 4–6 verbs: `park`, `unpark`, `pay`, `search` |
| 3 | Entities | Nouns from use cases become candidate classes |
| 4 | Relationships | has-a / is-a, cardinality (1:N), ownership |
| 5 | Class design | Attributes + methods per class, enums, interfaces |
| 6 | Patterns | Only where a variation point exists |
| 7 | Code core flows | 1–2 end-to-end flows, runnable |
| 8 | Extensibility | Walk through "what if…" changes |

### 1. Clarify

Ask, then write the answers down where the interviewer can see them.

- Who are the actors? (user, admin, system)
- What is explicitly out of scope? (payments, auth, UI, persistence)
- In-memory or DB-backed? Usually in-memory with repository classes.
- Concurrency: multiple users at once? This changes the design.
- Any variation the interviewer hints at? ("pricing may change" = strategy)

### 2. Use cases

Turn each actor's goal into a method signature early: `book(user, show, seats) -> Booking`. These become your public API.

### 3. Entities

- Nouns → classes; adjectives with fixed values → enums (`VehicleType`, `SeatStatus`).
- Drop nouns that are just attributes (a "price" is a field, not a class).
- Separate **entities** (have identity: `Booking`) from **services** (do work: `BookingService`).

### 4. Relationships

- Composition (owns lifecycle): `ParkingLot` → `Floor` → `Spot`.
- Association (references): `Booking` → `User`.
- Inheritance only for true is-a with shared behavior; prefer composition.

### 5. Class design

- Data classes for entities, ABCs for variation points, a service/manager per use-case group.
- Keep state changes inside the owning class (`spot.occupy(vehicle)`, not `spot.vehicle = v` from outside).

### 6. Patterns

Name the pattern and the reason in one sentence: "Pricing varies by vehicle type and time, so `PricingStrategy` is a strategy." Never force one.

### 7. Code core flows

Write the happy path end to end, then add validation and one failure path. Skip getters/setters boilerplate.

### 8. Extensibility

Proactively say: "To add EV charging spots, I add a `SpotType.EV` and a new pricing strategy; nothing else changes."

## 45-minute budget

| Minutes | Activity |
|---------|----------|
| 0–5 | Clarify and list use cases |
| 5–12 | Entities, relationships, enums |
| 12–20 | Class diagram / skeleton with method signatures |
| 20–38 | Code core flows |
| 38–45 | Concurrency, extensions, Q&A |

> **Rule of thumb:** If you have not started writing classes by minute 15, you are over-talking.

## Template skeleton

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class Status(Enum):
    AVAILABLE = "available"
    BOOKED = "booked"


@dataclass
class Entity:
    id: str
    status: Status = Status.AVAILABLE


class Strategy(ABC):            # the variation point
    @abstractmethod
    def apply(self, entity: Entity) -> int: ...


class Repository:               # in-memory storage, swappable for DB
    def __init__(self):
        self._items: dict[str, Entity] = {}

    def add(self, e: Entity) -> None:
        self._items[e.id] = e

    def get(self, id_: str) -> Entity:
        return self._items[id_]


class Service:                  # orchestrates a use case
    def __init__(self, repo: Repository, strategy: Strategy):
        self.repo, self.strategy = repo, strategy
```

## Pitfalls

- Jumping into code before agreeing scope.
- One giant `System` class doing everything.
- Inheritance trees five levels deep (`Car(FourWheeler(Vehicle))`).
- Patterns for show: a factory with one product, an observer with one listener.
- Ignoring concurrency when two users can grab the same resource.
- Leaving no time for the extensibility discussion.

> **Interview tip:** Narrate trade-offs as you go: "I'm keeping this in memory; in production this repository would be a Django model with a unique constraint."
