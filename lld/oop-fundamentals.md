---
title: OOP fundamentals
section: lld
order: 2
summary: Encapsulation, abstraction, inheritance vs composition, polymorphism, ABCs and Protocols in Python, and association vs aggregation vs composition.
---

## The four pillars

| Pillar | One line | Python tool |
|--------|----------|-------------|
| Encapsulation | Bundle state with the methods that guard it | `_private` convention, `@property` |
| Abstraction | Expose what, hide how | `ABC`, `Protocol` |
| Inheritance | Reuse and specialize an is-a type | `class Car(Vehicle)` |
| Polymorphism | Same call, different behavior | Duck typing, method overriding |

## Encapsulation

Keep invariants inside the class. Callers never set state that could break them.

```python
class Account:
    def __init__(self, balance: int = 0):
        self._balance = balance          # internal by convention

    @property
    def balance(self) -> int:            # read-only from outside
        return self._balance

    def withdraw(self, amount: int) -> None:
        if amount <= 0 or amount > self._balance:
            raise ValueError("invalid amount")
        self._balance -= amount
```

- `_x` = internal by convention. `__x` = name-mangled; use only to avoid subclass clashes.
- Prefer behavior methods (`withdraw`) over setters (`set_balance`).

## Abstraction

Define a contract; clients depend on it, not on concrete classes.

```python
from abc import ABC, abstractmethod


class PaymentGateway(ABC):
    @abstractmethod
    def charge(self, user_id: str, amount: int) -> str: ...


class StripeGateway(PaymentGateway):
    def charge(self, user_id: str, amount: int) -> str:
        return "txn_123"
```

### ABC vs Protocol

| | `ABC` | `typing.Protocol` |
|---|------|-------------------|
| Typing | Nominal (must subclass) | Structural (duck typing) |
| Runtime check | Instantiating an incomplete subclass raises `TypeError` | Only with `@runtime_checkable`, and shallow |
| Use when | You own the hierarchy, want enforcement | Third-party classes should fit without subclassing |

```python
from typing import Protocol


class Notifier(Protocol):
    def send(self, to: str, msg: str) -> None: ...
```

## Inheritance vs composition

- **Inheritance**: is-a, shares behavior, tightly coupled to parent internals.
- **Composition**: has-a, delegates to a collaborator, swappable at runtime.

```python
# Inheritance explosion: FlyingDuck, SwimmingDuck, FlyingSwimmingDuck...
# Composition: plug behaviors in.
class Duck:
    def __init__(self, fly_behavior, quack_behavior):
        self.fly_behavior = fly_behavior
        self.quack_behavior = quack_behavior

    def fly(self):
        return self.fly_behavior.fly()
```

> **Rule of thumb:** Favor composition. Use inheritance when the subclass is genuinely substitutable for the parent (see LSP) and the hierarchy is shallow.

## Polymorphism

- **Subtype / overriding**: `shape.area()` works for `Circle` and `Square`.
- **Duck typing**: anything with `.read()` works as a file.
- **Overloading**: Python has none natively; use default args or `functools.singledispatch`.

```python
from functools import singledispatch


@singledispatch
def render(obj):
    raise NotImplementedError


@render.register
def _(obj: int):
    return f"int {obj}"
```

Polymorphism replaces `if type == ...` chains. When you see a type switch, ask whether it should be a method on the types.

## Object relationships

| Relationship | Meaning | Lifecycle | Example |
|--------------|---------|-----------|---------|
| Association | Uses / knows about | Independent | `Booking` → `User` |
| Aggregation | Has-a, weak ownership | Part outlives whole | `Team` → `Player` |
| Composition | Has-a, strong ownership | Part dies with whole | `House` → `Room` |
| Dependency | Uses temporarily (param) | None stored | `Printer.print(doc)` |
| Inheritance | Is-a | n/a | `Car` → `Vehicle` |

```python
class Room: ...


class House:
    def __init__(self, n: int):
        self.rooms = [Room() for _ in range(n)]   # composition: created inside


class Team:
    def __init__(self, players: list["Player"]):
        self.players = players                    # aggregation: passed in
```

> **Interview tip:** In UML, composition is a filled diamond, aggregation is a hollow diamond. Say "composition" when the parent creates and owns the children.

## Other terms that come up

- **Coupling**: how much one class knows about another. Aim low.
- **Cohesion**: how focused one class is. Aim high.
- **Dataclass**: `@dataclass` for entities with mostly data; add `frozen=True` for value objects (`Money`, `Coordinate`).
- **Class vs instance attributes**: mutable class attributes (`items = []`) are shared across instances; a classic bug.

## Pitfalls

- Mutable default arguments (`def f(x=[])`); use `field(default_factory=list)` in dataclasses.
- Deep hierarchies for code reuse rather than is-a semantics.
- Exposing internals via public lists that callers mutate.
- Abstract base classes with a single implementation and no planned variation.
