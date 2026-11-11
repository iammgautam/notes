---
title: Behavioral patterns
section: lld
order: 6
summary: Strategy, observer, state, command, chain of responsibility, template method and iterator, with signals for when each fits an LLD problem.
---

Behavioral patterns distribute **responsibility and communication** between objects.

| Pattern | Signal in the problem | LLD example |
|---------|----------------------|-------------|
| Strategy | "Algorithm may change / multiple ways to X" | Pricing, split types, rate limit algorithms |
| Observer | "Notify when X happens" | Price alerts, seat availability, order events |
| State | "Behaves differently depending on status" | Vending machine, elevator, order lifecycle |
| Command | "Undo / queue / log actions" | Text editor, remote control, job queue |
| Chain of responsibility | "Pass through handlers until one handles it" | Log levels, approval chain, middleware |
| Template method | "Same steps, some vary" | Report generation, data import pipeline |
| Iterator | "Traverse without exposing internals" | Custom collections, paginated APIs |

## Strategy

Encapsulate interchangeable algorithms behind one interface; pick at runtime.

```python
from abc import ABC, abstractmethod


class PricingStrategy(ABC):
    @abstractmethod
    def price(self, hours: int) -> int: ...


class Hourly(PricingStrategy):
    def price(self, hours): return hours * 20


class FlatDaily(PricingStrategy):
    def price(self, hours): return 200


class Checkout:
    def __init__(self, strategy: PricingStrategy):
        self.strategy = strategy

    def total(self, hours: int) -> int:
        return self.strategy.price(hours)
```

> **Rule of thumb:** In Python a plain function works as a strategy (`Callable[[int], int]`). Use a class when the strategy has config or state.

## Observer

Subject keeps a list of subscribers and notifies them on change.

```python
class EventBus:
    def __init__(self):
        self._subs: dict[str, list] = {}

    def subscribe(self, event: str, fn) -> None:
        self._subs.setdefault(event, []).append(fn)

    def publish(self, event: str, payload) -> None:
        for fn in self._subs.get(event, []):
            fn(payload)


bus = EventBus()
bus.subscribe("seat_released", lambda p: print("notify waitlist", p))
bus.publish("seat_released", {"show": 1, "seat": "A1"})
```

- Django signals are an observer implementation.
- Pitfalls: a slow or failing subscriber blocks the publisher; memory leaks if subscribers never unsubscribe (use `weakref`).

## State

Each state is a class; the context delegates to its current state, and states trigger transitions.

```python
class OrderState(ABC):
    @abstractmethod
    def next(self, order): ...
    def cancel(self, order):
        raise ValueError("cannot cancel now")


class Placed(OrderState):
    def next(self, order): order.state = Shipped()
    def cancel(self, order): order.state = Cancelled()


class Shipped(OrderState):
    def next(self, order): order.state = Delivered()


class Delivered(OrderState):
    def next(self, order): raise ValueError("final")


class Cancelled(Delivered): ...


class Order:
    def __init__(self):
        self.state: OrderState = Placed()

    def next(self): self.state.next(self)
    def cancel(self): self.state.cancel(self)
```

Strategy vs state: same shape; with strategy the **client** picks the algorithm, with state the **object itself** switches as it transitions.

## Command

Wrap a request as an object, so you can queue, log, retry or undo it.

```python
class Command(ABC):
    @abstractmethod
    def execute(self): ...
    @abstractmethod
    def undo(self): ...


class Insert(Command):
    def __init__(self, doc: list, text: str):
        self.doc, self.text = doc, text

    def execute(self): self.doc.append(self.text)
    def undo(self): self.doc.pop()


class Editor:
    def __init__(self):
        self.doc, self.history = [], []

    def run(self, cmd: Command):
        cmd.execute()
        self.history.append(cmd)

    def undo(self):
        if self.history:
            self.history.pop().undo()
```

A Celery task message is a serialized command.

## Chain of responsibility

Handlers linked in a chain; each handles or passes on.

```python
class Handler(ABC):
    def __init__(self, nxt: "Handler | None" = None):
        self.nxt = nxt

    def handle(self, amount: int) -> str:
        if self.can_handle(amount):
            return self.process(amount)
        if self.nxt:
            return self.nxt.handle(amount)
        raise ValueError("no approver")

    @abstractmethod
    def can_handle(self, amount): ...
    @abstractmethod
    def process(self, amount): ...


class Manager(Handler):
    def can_handle(self, a): return a <= 1_000
    def process(self, a): return "manager"


class Director(Handler):
    def can_handle(self, a): return a <= 10_000
    def process(self, a): return "director"


chain = Manager(Director())
```

Django middleware and DRF permission classes are chains.

## Template method

Base class fixes the algorithm skeleton; subclasses fill in steps.

```python
class Importer(ABC):
    def run(self, path):                # the template, don't override
        rows = self.parse(open(path).read())
        valid = [r for r in rows if self.validate(r)]
        self.save(valid)

    @abstractmethod
    def parse(self, raw): ...
    def validate(self, row): return True   # hook with default
    @abstractmethod
    def save(self, rows): ...


class CSVImporter(Importer):
    def parse(self, raw): return [l.split(",") for l in raw.splitlines()]
    def save(self, rows): ...
```

Template method uses inheritance; strategy uses composition. Prefer strategy when steps vary independently.

## Iterator

Traverse a collection without exposing its structure. In Python, implement `__iter__`, usually as a generator.

```python
class Playlist:
    def __init__(self):
        self._songs: list[str] = []

    def add(self, s): self._songs.append(s)

    def __iter__(self):
        yield from self._songs

    def shuffled(self):
        import random
        yield from random.sample(self._songs, len(self._songs))
```

## Pitfalls

- Observer chains that trigger each other in loops.
- State classes that reach into context internals; keep transitions explicit.
- Command undo that is not truly reversible (side effects like emails).
- Using template method where steps need to mix and match at runtime.

> **Interview tip:** Strategy, state and observer cover most case studies. Name them out loud when you introduce the class.
