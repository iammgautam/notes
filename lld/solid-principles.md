---
title: SOLID principles
section: lld
order: 5
phase: "Phase 2 · SOLID"
summary: The five SOLID principles, the problem each one prevents, a bad-to-good Python example for each, and how to spot a violation in an interview.
---

Five rules for keeping classes easy to change. Each prevents a ripple: one change forcing edits in many places, or a subclass breaking code that trusted its parent.

| Letter | Principle | Problem it prevents | Smell in an interview |
|---|---|---|---|
| S | Single responsibility | Unrelated changes collide in one class | Class name has "And" or "Manager"; it imports the DB, email and pricing |
| O | Open/closed | Every new type edits old code | `if/elif` on a type string |
| L | Liskov substitution | Subclass breaks callers of the parent | Override raises `NotImplementedError` or ignores an argument |
| I | Interface segregation | Implementers forced to stub methods | Empty or raising methods to satisfy a base class |
| D | Dependency inversion | Business logic welded to infrastructure | `self.repo = PostgresRepo()` inside `__init__` |

## S: single responsibility

A class should have one reason to change. "Reason" means one stakeholder or concern: pricing rules, storage format, notification channel.

```python
# Bad: three reasons to change in one method
class InvoiceService:
    def create(self, order: dict) -> None:
        total = sum(item["price"] for item in order["items"])   # pricing
        with open("invoices.csv", "a") as f:                     # storage
            f.write(f"{order['id']},{total}\n")
        print(f"email {order['email']}: total {total}")          # channel
```

```python
# Good: one collaborator per concern; the service only orchestrates
class InvoiceCalculator:
    def total(self, order: dict) -> int:
        return sum(item["price"] for item in order["items"])


class InMemoryInvoiceRepo:
    def __init__(self):
        self.saved: dict[str, int] = {}

    def save(self, order_id: str, total: int) -> None:
        self.saved[order_id] = total


class ConsoleNotifier:
    def notify(self, email: str, total: int) -> None:
        print(f"email {email}: total {total}")


class InvoiceService:
    def __init__(self, calc, repo, notifier):
        self.calc, self.repo, self.notifier = calc, repo, notifier

    def create(self, order: dict) -> int:
        total = self.calc.total(order)
        self.repo.save(order["id"], total)
        self.notifier.notify(order["email"], total)
        return total


repo = InMemoryInvoiceRepo()
service = InvoiceService(InvoiceCalculator(), repo, ConsoleNotifier())
items = [{"price": 40}, {"price": 60}]
order = {"id": "o1", "email": "a@x.com", "items": items}
assert service.create(order) == 100 and repo.saved == {"o1": 100}
```

**Spot it**: describe the class in one sentence. If you need "and", split it.

## O: open/closed

Open for extension, closed for modification: add behavior by adding code, not by editing tested code.

```python
# Bad: every new discount edits this function
def discount(kind: str, amount: int) -> int:
    if kind == "festive":
        return amount * 10 // 100
    elif kind == "loyalty":
        return min(50, amount)
    return 0
```

```python
# Good: a new discount is a new class; checkout never changes
from abc import ABC, abstractmethod


class Discount(ABC):
    @abstractmethod
    def apply(self, amount: int) -> int: ...


class Festive(Discount):
    def apply(self, amount: int) -> int:
        return amount * 10 // 100


class Loyalty(Discount):
    def apply(self, amount: int) -> int:
        return min(50, amount)


def checkout(amount: int, discount: Discount) -> int:
    return amount - discount.apply(amount)


assert checkout(1000, Festive()) == 900
assert checkout(1000, Loyalty()) == 950
```

A dict registry (`DISCOUNTS = {"festive": Festive()}`) is the Pythonic factory on top: a new entry, no new branch.

**Spot it**: ask "what happens when we add a new X?" If the answer is "edit this `if/elif` in three places", that is the violation.

## L: Liskov substitution

A subclass must work anywhere its parent works: same preconditions or looser, same guarantees or stronger, no new exceptions.

```python
# Bad: Square changes what set_width promises
class Rectangle:
    def __init__(self, w: int, h: int):
        self.w, self.h = w, h

    def set_width(self, w: int) -> None:
        self.w = w

    def set_height(self, h: int) -> None:
        self.h = h

    def area(self) -> int:
        return self.w * self.h


class Square(Rectangle):
    def __init__(self, side: int):
        super().__init__(side, side)

    def set_width(self, w: int) -> None:
        self.w = self.h = w

    def set_height(self, h: int) -> None:
        self.w = self.h = h


def resize(r: Rectangle) -> int:      # written against Rectangle's contract
    r.set_width(5)
    r.set_height(2)
    return r.area()


assert resize(Rectangle(1, 1)) == 10
assert resize(Square(1)) == 4         # the caller expected 10
```

```python
# Good: share only what is truly shared; immutable shapes have no setters
from abc import ABC, abstractmethod
from dataclasses import dataclass


class Shape(ABC):
    @abstractmethod
    def area(self) -> int: ...


@dataclass(frozen=True)
class Rectangle(Shape):
    w: int
    h: int

    def area(self) -> int:
        return self.w * self.h


@dataclass(frozen=True)
class Square(Shape):
    side: int

    def area(self) -> int:
        return self.side ** 2


assert [s.area() for s in (Rectangle(5, 2), Square(3))] == [10, 9]
```

**Spot it**: an override that raises `NotImplementedError` (`Penguin.fly()`), returns a different type, rejects inputs the parent accepts, or callers that need `isinstance` checks before calling.

## I: interface segregation

Clients should not depend on methods they don't use. Many small interfaces beat one fat one.

```python
# Bad: a basic printer is forced to fake scanning
from abc import ABC, abstractmethod


class Machine(ABC):
    @abstractmethod
    def print_doc(self, doc: str) -> str: ...

    @abstractmethod
    def scan(self, doc: str) -> str: ...


class BasicPrinter(Machine):
    def print_doc(self, doc: str) -> str:
        return f"printed {doc}"

    def scan(self, doc: str) -> str:
        raise NotImplementedError   # forced stub
```

```python
# Good: small capabilities; each class implements what it has
from typing import Protocol


class Printer(Protocol):
    def print_doc(self, doc: str) -> str: ...


class Scanner(Protocol):
    def scan(self, doc: str) -> str: ...


class BasicPrinter:
    def print_doc(self, doc: str) -> str:
        return f"printed {doc}"


class OfficeMachine:
    def print_doc(self, doc: str) -> str:
        return f"printed {doc}"

    def scan(self, doc: str) -> str:
        return f"scanned {doc}"


def print_all(printer: Printer, docs: list[str]) -> list[str]:
    return [printer.print_doc(d) for d in docs]


assert print_all(BasicPrinter(), ["a"]) == print_all(OfficeMachine(), ["a"])
```

**Spot it**: stubbed or raising methods in an implementer, or a function that takes a big interface but calls one method on it. ISP violations usually cause LSP violations.

## D: dependency inversion

High-level policy (business rules) should depend on an abstraction, not on low-level details (database, HTTP client). The concrete class is passed in.

```python
# Bad: OrderService cannot run without a real database
class PostgresOrderRepo:
    def save(self, order_id: str) -> None:
        raise ConnectionError("needs a real database")


class OrderService:
    def __init__(self):
        self.repo = PostgresOrderRepo()     # hard-wired concrete class

    def place(self, order_id: str) -> None:
        self.repo.save(order_id)
```

```python
# Good: the service owns the abstraction; any repo that fits is injected
from typing import Protocol


class OrderRepo(Protocol):
    def save(self, order_id: str) -> None: ...


class InMemoryOrderRepo:
    def __init__(self):
        self.ids: list[str] = []

    def save(self, order_id: str) -> None:
        self.ids.append(order_id)


class OrderService:
    def __init__(self, repo: OrderRepo):
        self.repo = repo

    def place(self, order_id: str) -> None:
        self.repo.save(order_id)


repo = InMemoryOrderRepo()
OrderService(repo).place("o1")
assert repo.ids == ["o1"]
```

**Spot it**: a concrete class constructed inside business logic, or a unit test that needs a database, network or clock to run.

> **Gotcha:** Dependency inversion is the principle (depend on abstractions). Dependency injection is the technique (pass collaborators in). Interviewers sometimes check that you know the difference.

## When to apply, when not

| Apply | Skip |
|---|---|
| At a variation point the problem names ("pricing may change") | "Just in case" interfaces with one implementation |
| When a class is hard to test in isolation | Two stable cases that will never grow |

## Pitfalls

- Over-applying SRP until every method is its own class. Group by reason to change, not line count.
- An ABC for every class "for OCP". Add the abstraction where variation is real or requested.
- Treating LSP as a syntax rule. It is about behavior callers rely on.

## Interview questions

- **Which principle does Strategy support?** OCP (new behavior is a new class) and DIP (the context depends on the strategy interface).
- **Is Python's duck typing a problem for LSP?** No. LSP is about behavior, not declared types. A duck-typed object that honors the contract is substitutable; one that raises on a required method is not.
- **How do you apply DIP in Django?** Services take a repository or gateway in their constructor; views or a small factory wire the real ORM-backed or HTTP-backed one, and tests pass an in-memory fake.
