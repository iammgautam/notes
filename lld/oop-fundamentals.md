---
title: OOP fundamentals
section: lld
order: 2
phase: "Phase 1 · OOP foundations"
summary: Encapsulation, abstraction, composition over inheritance, polymorphism with ABCs and Protocols, Python access rules, and object relationships.
---

OOP in an LLD round comes down to one question: which class owns which data, and which behavior goes with it. Everything below is a tool for answering that well.

| Concept | One line | Python tool |
|---|---|---|
| Encapsulation | State changes only through methods that guard invariants | `_attr`, `@property` |
| Abstraction | Callers see what, not how | `ABC`, `Protocol` |
| Inheritance | Is-a, with a substitutable subclass | `class Savings(Account)` |
| Composition | Has-a; delegate to a collaborator | `self.engine = engine` |
| Polymorphism | Same call, behavior picked at runtime | Overriding, duck typing |

## Encapsulation and access conventions

```python
class Account:
    def __init__(self, owner: str):
        self.owner = owner              # public
        self._balance = 0               # internal by convention
        self.__audit: list[str] = []    # name-mangled to _Account__audit

    @property
    def balance(self) -> int:           # read-only view, no setter
        return self._balance

    def deposit(self, amount: int) -> None:
        if amount <= 0:
            raise ValueError("amount must be positive")
        self._balance += amount
        self.__audit.append(f"deposit {amount}")


acct = Account("asha")
acct.deposit(100)
assert acct.balance == 100
try:
    acct.balance = 5                    # no setter, so AttributeError
    raise AssertionError("expected AttributeError")
except AttributeError:
    pass
assert acct._Account__audit == ["deposit 100"]   # renamed, not hidden
```

| Form | Meaning | Enforced? |
|---|---|---|
| `name` | Public API | n/a |
| `_name` | Internal; subclasses may touch it | No, convention only |
| `__name` | Renamed to `_Class__name` so subclasses can't clash with it | Only the rename |
| `@property` | Read-only or computed attribute; add validation later without changing callers | Assigning without a setter raises |

- Expose behavior (`deposit`, `withdraw`), not setters (`set_balance`).
- Return a tuple or copy of internal collections, never the list itself.

## Abstraction: ABC vs Protocol

```python
from abc import ABC, abstractmethod
from typing import Protocol


class PaymentGateway(ABC):      # nominal: implementers must subclass
    @abstractmethod
    def charge(self, user_id: str, amount: int) -> str: ...


class StripeGateway(PaymentGateway):
    def charge(self, user_id: str, amount: int) -> str:
        return f"txn-{user_id}-{amount}"


class Notifier(Protocol):       # structural: any matching shape fits
    def send(self, to: str, msg: str) -> None: ...


class ConsoleNotifier:                  # no base class needed
    def send(self, to: str, msg: str) -> None:
        print(f"to {to}: {msg}")


def checkout(gw: PaymentGateway, notifier: Notifier, user: str) -> str:
    txn = gw.charge(user, 500)
    notifier.send(user, f"paid {txn}")
    return txn


assert checkout(StripeGateway(), ConsoleNotifier(), "u1") == "txn-u1-500"
try:
    PaymentGateway()            # abstract methods block instantiation
    raise AssertionError("expected TypeError")
except TypeError:
    pass
```

| | `ABC` | `Protocol` |
|---|---|---|
| Typing | Nominal (must subclass) | Structural (duck typing, checked by mypy) |
| Runtime check | Missing method raises `TypeError` on instantiation | None, unless `@runtime_checkable` (and then only names) |
| Use when | You own the hierarchy and want enforcement or shared code | Third-party or unrelated classes should fit without subclassing |

## Runtime polymorphism

- Python picks the method from the object's type at call time. Every method is "virtual"; there is no keyword.
- Duck typing: anything with the right methods works. ABCs and Protocols add a checked contract on top.
- No overloading by signature. Use default arguments or `functools.singledispatch`.
- A chain of `if kind == ...` is the smell polymorphism removes: move each branch into a method on its type.

```python
import math


class Circle:
    def __init__(self, r: float):
        self.r = r

    def area(self) -> float:
        return math.pi * self.r ** 2


class Square:
    def __init__(self, side: float):
        self.side = side

    def area(self) -> float:
        return self.side ** 2


assert round(sum(s.area() for s in (Circle(1), Square(2))), 2) == 7.14
```

## Inheritance vs composition

- **Inheritance**: is-a. The subclass must work anywhere the parent does (see [SOLID principles]({{ '/lld/solid-principles/' | relative_url }}), LSP). It couples you to the parent's internals.
- **Composition**: has-a. Delegate to a collaborator you can swap at runtime and fake in tests.

```python
# Inheritance explodes: FlyingDuck, RubberDuck, FlyingQuietDuck, ...
class FlyWithWings:
    def fly(self) -> str:
        return "flapping"


class NoFly:
    def fly(self) -> str:
        return "stays put"


class Duck:
    def __init__(self, name: str, flyer):
        self.name = name
        self._flyer = flyer             # composed behavior, swappable

    def fly(self) -> str:
        return f"{self.name}: {self._flyer.fly()}"


assert Duck("mallard", FlyWithWings()).fly() == "mallard: flapping"
assert Duck("rubber", NoFly()).fly() == "rubber: stays put"
```

> **Rule of thumb:** Inherit only for a true is-a with shared behavior, at most two levels deep. If you inherit just to reuse code, or a subclass has to disable a parent method, compose instead.

## Minimal constructors

- `__init__` assigns and validates fields. No I/O, network calls, threads or DB queries.
- Take collaborators as parameters (dependency injection); don't build them inside.
- Put alternate construction in a `@classmethod` (`from_row`, `of`).
- Dataclasses: `field(default_factory=list)` for mutable defaults, validation in `__post_init__`, `frozen=True` for value objects.

```python
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str = "INR"

    def __post_init__(self):
        if self.amount < 0:
            raise ValueError("money cannot be negative")

    @classmethod
    def of(cls, text: str) -> "Money":
        return cls(Decimal(text))


assert Money.of("10.50") == Money(Decimal("10.50"))
```

## Object relationships

| Relationship | Meaning | Lifecycle | Example |
|---|---|---|---|
| Association | Holds a reference | Independent | `Booking` → `User` |
| Aggregation | Has-a, weak ownership | Part outlives the whole | `Team` → `Player` |
| Composition | Has-a, strong ownership | Part dies with the whole | `Order` → `OrderItem` |
| Dependency | Uses briefly, not stored | None | `Coach.train(team)` |
| Inheritance | Is-a | n/a | `Car` → `Vehicle` |

```python
class Player:
    def __init__(self, name: str):
        self.name = name


class Team:
    def __init__(self, players: list[Player]):
        self.players = list(players)    # aggregation: passed in


class OrderItem:
    def __init__(self, sku: str, qty: int):
        self.sku, self.qty = sku, qty


class Order:
    def __init__(self, lines: list[tuple[str, int]]):
        # composition: Order creates and owns its items
        self.items = [OrderItem(sku, qty) for sku, qty in lines]


class Coach:
    def train(self, team: Team) -> int:  # dependency: never stored
        return len(team.players)


assert Coach().train(Team([Player("a"), Player("b")])) == 2
assert Order([("pen", 2)]).items[0].qty == 2
```

## What interviewers check: responsibilities

- **Information expert**: the method goes on the class that has the data. `spot.occupy()`, `cart.total()`.
- **Tell, don't ask**: `account.withdraw(x)`, not `if account.balance >= x: account.balance -= x` in the caller.
- Entities guard their own invariants; services coordinate several entities.
- High cohesion, low coupling: you can describe each class in one sentence without "and".

## Pitfalls

- Mutable default arguments (`def f(x=[])`) and mutable class attributes shared by every instance.
- Returning internal lists that callers then mutate.
- Deep hierarchies built for code reuse rather than is-a.
- An ABC with one implementation and no planned variation.
- `__private` everywhere. It adds no security and makes subclassing and tests awkward.
- Constructors that open connections or start threads.

## Interview questions

- **Composition vs aggregation?** Composition: the whole creates and owns the part, which dies with it (`Order` → `OrderItem`). Aggregation: the part is passed in and outlives the whole (`Team` → `Player`).
- **Why prefer composition?** Behaviors vary independently, swap at runtime, and test with fakes. Inheritance fixes the combination at class-definition time and leaks parent internals.
- **Does Python have private members?** No. `_x` is a convention; `__x` is renamed to avoid subclass clashes. Encapsulation comes from API design, not enforcement.
- **ABC or Protocol?** ABC when you own the hierarchy and want instantiation-time checks or shared code. Protocol when unrelated or third-party classes should fit.
- **Method overloading?** Not by signature. Use defaults, keyword arguments or `functools.singledispatch`.
