---
title: SOLID principles
section: lld
order: 3
summary: The five SOLID principles, each with a bad-to-good Python example and the smell that tells you it is being violated.
---

| Letter | Principle | Smell that signals a violation |
|--------|-----------|-------------------------------|
| S | Single responsibility | Class changes for unrelated reasons |
| O | Open/closed | Adding a type means editing an `if/elif` chain |
| L | Liskov substitution | Subclass raises `NotImplementedError` or surprises callers |
| I | Interface segregation | Implementers stub out methods they don't need |
| D | Dependency inversion | High-level class does `SomeConcrete()` inside itself |

## S: single responsibility

A class should have one reason to change.

```python
# Bad: business logic, persistence and email in one class
class InvoiceService:
    def create(self, order):
        total = sum(i.price for i in order.items)
        db.save(total)
        smtp.send(order.email, f"Total {total}")
```

```python
# Good: each collaborator owns one concern
class InvoiceCalculator:
    def total(self, order) -> int:
        return sum(i.price for i in order.items)


class InvoiceService:
    def __init__(self, calc, repo, notifier):
        self.calc, self.repo, self.notifier = calc, repo, notifier

    def create(self, order):
        total = self.calc.total(order)
        self.repo.save(order.id, total)
        self.notifier.notify(order.email, total)
```

## O: open/closed

Open for extension, closed for modification.

```python
# Bad: every new discount edits this function
def discount(kind: str, amount: int) -> int:
    if kind == "festive":
        return amount * 10 // 100
    elif kind == "loyalty":
        return 50
    return 0
```

```python
# Good: new discount = new class
from abc import ABC, abstractmethod


class Discount(ABC):
    @abstractmethod
    def apply(self, amount: int) -> int: ...


class Festive(Discount):
    def apply(self, amount: int) -> int:
        return amount * 10 // 100


class Loyalty(Discount):
    def apply(self, amount: int) -> int:
        return 50
```

## L: Liskov substitution

Subtypes must be usable wherever the base type is expected, without surprises.

```python
# Bad: Square breaks Rectangle's contract (width and height independent)
class Rectangle:
    def set_width(self, w): self.w = w
    def set_height(self, h): self.h = h


class Square(Rectangle):
    def set_width(self, w): self.w = self.h = w
    def set_height(self, h): self.w = self.h = h
```

```python
# Good: model what they share, not a false is-a
class Shape(ABC):
    @abstractmethod
    def area(self) -> float: ...


class Rectangle(Shape):
    def __init__(self, w, h): self.w, self.h = w, h
    def area(self): return self.w * self.h


class Square(Shape):
    def __init__(self, side): self.side = side
    def area(self): return self.side ** 2
```

Other LSP violations: `Penguin(Bird).fly()` raising, a subclass that tightens preconditions or returns a different type.

## I: interface segregation

Many small interfaces beat one fat one.

```python
# Bad: a basic printer is forced to implement fax and scan
class Machine(ABC):
    @abstractmethod
    def print(self, doc): ...
    @abstractmethod
    def scan(self, doc): ...
    @abstractmethod
    def fax(self, doc): ...


class BasicPrinter(Machine):
    def print(self, doc): ...
    def scan(self, doc): raise NotImplementedError
    def fax(self, doc): raise NotImplementedError
```

```python
# Good: compose capabilities
class Printer(ABC):
    @abstractmethod
    def print(self, doc): ...


class Scanner(ABC):
    @abstractmethod
    def scan(self, doc): ...


class BasicPrinter(Printer):
    def print(self, doc): ...


class OfficeMachine(Printer, Scanner):
    def print(self, doc): ...
    def scan(self, doc): ...
```

## D: dependency inversion

High-level modules depend on abstractions; concrete classes are injected.

```python
# Bad: hard-wired, untestable without a real DB
class OrderService:
    def __init__(self):
        self.repo = PostgresOrderRepo()
```

```python
# Good: depend on an abstraction, inject the concrete one
class OrderRepo(ABC):
    @abstractmethod
    def save(self, order): ...


class OrderService:
    def __init__(self, repo: OrderRepo):
        self.repo = repo


service = OrderService(PostgresOrderRepo())   # prod
test_service = OrderService(InMemoryOrderRepo())  # tests
```

> **Gotcha:** Dependency inversion is the principle; dependency injection (passing collaborators in) is the technique. Interviewers sometimes check that you know the difference.

## Pitfalls

- Over-applying SRP until every method is its own class. Group by reason to change, not by line count.
- Creating an ABC for every class "for OCP". Add the abstraction at the point where variation is real or requested.
- Thinking LSP is about inheritance syntax; it is about behavioral contracts.

> **Interview tip:** When you introduce a strategy or interface in a case study, name the principle: "This keeps `ParkingService` closed for modification when we add new pricing (OCP)."
