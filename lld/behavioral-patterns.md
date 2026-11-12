---
title: Behavioral patterns
section: lld
order: 8
phase: "Phase 3 · Design patterns"
summary: Strategy, observer, command, state and chain of responsibility (the most asked), plus template method and iterator, in runnable Python.
---

Behavioral patterns decide **which object does what, and how they talk**. The first five in the table carry most LLD rounds; know them cold.

| Pattern | Signal in the problem | LLD example | Most asked |
|---|---|---|---|
| Strategy | "Multiple ways to X", "varies by type" | Fees, split types, rate-limit algorithms | Yes |
| Observer | "Notify when X happens", many listeners | Price alerts, seat released, order events | Yes |
| Command | "Undo / redo / queue / replay" | Text editor, job queue, remote control | Yes |
| State | "Behaves differently depending on status" | Vending machine, order lifecycle, elevator | Yes |
| Chain of responsibility | "Pass along until handled", pipeline of checks | Approvals, middleware, log levels | Yes |
| Template method | "Same steps, one step differs" | Importers, report generation | |
| Iterator | "Traverse without exposing internals" | Paginated API, custom collections | |

## Strategy

Problem: one operation with several interchangeable algorithms. `if/elif` on a type inside the class grows with every new variant.

```python
from abc import ABC, abstractmethod


class FeeStrategy(ABC):
    @abstractmethod
    def fee(self, hours: int) -> int: ...


class Hourly(FeeStrategy):
    def __init__(self, rate: int) -> None:
        self.rate = rate

    def fee(self, hours: int) -> int:
        return hours * self.rate


class DailyCap(FeeStrategy):
    def __init__(self, rate: int, cap: int) -> None:
        self.rate, self.cap = rate, cap

    def fee(self, hours: int) -> int:
        return min(hours * self.rate, self.cap)


class ParkingTicket:
    def __init__(self, strategy: FeeStrategy) -> None:
        self.strategy = strategy              # injected, swappable at runtime

    def checkout(self, hours: int) -> int:
        return self.strategy.fee(hours)


assert ParkingTicket(Hourly(50)).checkout(3) == 150
assert ParkingTicket(DailyCap(rate=50, cap=300)).checkout(10) == 300
```

> **Rule of thumb:** With no config or state, a plain function is a strategy (`Callable[[int], int]`). Use a class when the strategy carries parameters like `rate` and `cap`.

**Avoid when** there is one algorithm and no hint of a second. Pair it with a factory (a `dict[Enum, FeeStrategy]`) to pick from input.

## Observer

Problem: one event, many independent reactions (email, analytics, waitlist). The publisher should not import or know any of them.

```python
import inspect
import logging
import weakref
from collections import defaultdict
from typing import Any, Callable

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[weakref.ref]] = defaultdict(list)

    def subscribe(self, event: str, handler: Callable[[Any], None]) -> None:
        # Bound methods need WeakMethod: a plain ref to one dies immediately.
        ref = (weakref.WeakMethod(handler) if inspect.ismethod(handler)
               else weakref.ref(handler))
        self._subs[event].append(ref)

    def publish(self, event: str, payload: Any) -> int:
        alive = []
        for ref in self._subs[event]:
            handler = ref()
            if handler is None:               # subscriber was garbage-collected
                continue
            alive.append(ref)
            try:
                handler(payload)
            except Exception:                 # isolate a failing subscriber
                log.exception("handler failed for %s", event)
        self._subs[event] = alive             # prune dead references
        return len(alive)


class Waitlist:
    def __init__(self) -> None:
        self.notified: list[str] = []

    def on_seat_released(self, seat: str) -> None:
        self.notified.append(seat)


bus = EventBus()
waitlist = Waitlist()
bus.subscribe("seat_released", waitlist.on_seat_released)
assert bus.publish("seat_released", "A1") == 1 and waitlist.notified == ["A1"]
del waitlist                                  # no unsubscribe call needed
assert bus.publish("seat_released", "A2") == 0
```

- **Why weak references:** a strong reference in the bus keeps every subscriber alive forever (the classic observer memory leak). Weak references let a subscriber disappear when nothing else uses it.
- **The flip side:** a lambda passed only to the bus is collected immediately and never fires. Django signals have the same rule (`connect(weak=True)` by default); pass `weak=False` for closures.
- Handlers run synchronously on the publisher's thread. A slow one blocks the publisher; push events onto a queue instead ([Producer-consumer]({{ '/lld/producer-consumer/' | relative_url }}), [Pub-sub broker]({{ '/lld/pub-sub-broker/' | relative_url }})).
- The bus is not thread-safe; guard `_subs` with a `threading.Lock` if threads subscribe concurrently.

## Command

Problem: you need to undo, redo, queue, retry or audit actions. Turn each action into an object that knows how to run and reverse itself.

```python
from abc import ABC, abstractmethod


class Command(ABC):
    @abstractmethod
    def execute(self) -> None: ...

    @abstractmethod
    def undo(self) -> None: ...


class Append(Command):
    def __init__(self, buf: list[str], text: str) -> None:
        self.buf, self.text = buf, text

    def execute(self) -> None:
        self.buf.append(self.text)

    def undo(self) -> None:
        self.buf.pop()


class Editor:
    def __init__(self) -> None:
        self.buf: list[str] = []
        self._undo: list[Command] = []
        self._redo: list[Command] = []

    def run(self, cmd: Command) -> None:
        cmd.execute()
        self._undo.append(cmd)
        self._redo.clear()                    # new action: redo history is gone

    def undo(self) -> None:
        if self._undo:
            cmd = self._undo.pop()
            cmd.undo()
            self._redo.append(cmd)

    def redo(self) -> None:
        if self._redo:
            cmd = self._redo.pop()
            cmd.execute()
            self._undo.append(cmd)


ed = Editor()
ed.run(Append(ed.buf, "a"))
ed.run(Append(ed.buf, "b"))
ed.undo()
assert ed.buf == ["a"]
ed.redo()
assert ed.buf == ["a", "b"]
```

- **Queueing:** because a command is data, you can put it on a `queue.Queue` for worker threads, persist it for retries, or log it for audit. A Celery task message is a serialized command. See [Task scheduler]({{ '/lld/task-scheduler/' | relative_url }}).
- A command must store whatever it needs to undo (a `Delete` keeps the deleted text).
- Side effects like sending an email are not undoable; model them as a compensating action instead.

## State

Problem: the same call means different things depending on the current status, and `if self.status == ...` spreads through every method.

```python
class InvalidTransition(Exception): ...


class OrderState:
    def pay(self, order: "Order") -> None:
        self._reject("pay")

    def ship(self, order: "Order") -> None:
        self._reject("ship")

    def cancel(self, order: "Order") -> None:
        self._reject("cancel")

    def _reject(self, action: str) -> None:
        raise InvalidTransition(f"cannot {action} when {type(self).__name__}")


class Created(OrderState):
    def pay(self, order: "Order") -> None:
        order.state = Paid()

    def cancel(self, order: "Order") -> None:
        order.state = Cancelled()


class Paid(OrderState):
    def ship(self, order: "Order") -> None:
        order.state = Shipped()

    def cancel(self, order: "Order") -> None:
        order.refund_due = True
        order.state = Cancelled()


class Shipped(OrderState): ...
class Cancelled(OrderState): ...


class Order:
    def __init__(self) -> None:
        self.state: OrderState = Created()
        self.refund_due = False

    def pay(self) -> None:
        self.state.pay(self)                  # delegate to the current state

    def ship(self) -> None:
        self.state.ship(self)

    def cancel(self) -> None:
        self.state.cancel(self)


order = Order()
order.pay()
order.cancel()
assert isinstance(order.state, Cancelled) and order.refund_due
try:
    order.ship()
except InvalidTransition as e:
    assert str(e) == "cannot ship when Cancelled"
else:
    raise AssertionError("shipped a cancelled order")
```

- The base class rejects everything; each state overrides only its legal moves. Illegal transitions fail loudly instead of corrupting data.
- **Lighter option:** if states differ only in which transitions are legal, a `dict[tuple[Status, Event], Status]` table with an `Enum` is enough. Use state classes when each state *behaves* differently (a vending machine's `insert_coin` in Idle vs Dispensing). Full example: [Vending machine]({{ '/lld/vending-machine/' | relative_url }}).
- **Strategy vs state:** same shape. The client picks a strategy; a state object replaces itself as events happen.

## Chain of responsibility

Problem: a request passes through a sequence of handlers, and you want to add, remove or reorder them without touching the sender. Two flavors.

**First capable handler wins** (approvals, log levels, ATM note dispensing):

```python
from abc import ABC, abstractmethod


class Approver(ABC):
    def __init__(self, nxt: "Approver | None" = None) -> None:
        self._next = nxt

    def approve(self, amount: int) -> str:
        if self.can_approve(amount):
            return type(self).__name__
        if self._next is None:
            raise PermissionError(f"nobody can approve {amount}")
        return self._next.approve(amount)

    @abstractmethod
    def can_approve(self, amount: int) -> bool: ...


class Manager(Approver):
    def can_approve(self, amount: int) -> bool:
        return amount <= 1_000


class Director(Approver):
    def can_approve(self, amount: int) -> bool:
        return amount <= 10_000


chain = Manager(Director())
assert chain.approve(500) == "Manager"
assert chain.approve(5_000) == "Director"
```

**Every handler runs and may stop the chain** (middleware, validation pipelines). Django middleware works this way: each layer receives `get_response` (the next handler).

```python
from typing import Callable

Handler = Callable[[dict], dict]


def auth(next_handler: Handler) -> Handler:
    def handle(request: dict) -> dict:
        if "user" not in request:
            return {"status": 401}            # short-circuit: stop here
        return next_handler(request)
    return handle


def add_request_id(next_handler: Handler) -> Handler:
    def handle(request: dict) -> dict:
        response = next_handler(request)
        response["request_id"] = "r-1"        # post-processing on the way out
        return response
    return handle


def view(request: dict) -> dict:
    return {"status": 200}


app = add_request_id(auth(view))
assert app({"user": "asha"})["status"] == 200
assert app({}) == {"status": 401, "request_id": "r-1"}
```

Watch for a request that falls off the end unhandled: raise, as `Approver` does, or end the chain with a default handler.

## Template method

The base class fixes the order of steps; subclasses fill in the ones that vary.

```python
from abc import ABC, abstractmethod


class Importer(ABC):
    def run(self, raw: str) -> int:           # the template: don't override
        rows = self.parse(raw)
        valid = [r for r in rows if self.validate(r)]
        self.save(valid)
        return len(valid)

    @abstractmethod
    def parse(self, raw: str) -> list[list[str]]: ...

    def validate(self, row: list[str]) -> bool:   # hook with a default
        return all(row)

    @abstractmethod
    def save(self, rows: list[list[str]]) -> None: ...


class CsvImporter(Importer):
    def __init__(self) -> None:
        self.saved: list[list[str]] = []

    def parse(self, raw: str) -> list[list[str]]:
        return [line.split(",") for line in raw.splitlines()]

    def save(self, rows: list[list[str]]) -> None:
        self.saved.extend(rows)


assert CsvImporter().run("a,1\nb,\nc,3") == 2
```

Template method uses inheritance; strategy uses composition. Prefer strategy when the varying steps need to mix and match at runtime.

## Iterator

Traverse without exposing the structure. Python has the protocol built in: implement `__iter__` (usually as a generator) or write a generator function.

```python
from typing import Callable, Iterator


Fetch = Callable[[int, int], list[int]]      # (page, size) -> rows


def paginate(fetch: Fetch, size: int = 100) -> Iterator[int]:
    page = 0
    while rows := fetch(page, size):          # stop on the first empty page
        yield from rows
        page += 1


data = list(range(250))
assert list(paginate(lambda p, n: data[p * n:(p + 1) * n])) == data
```

Callers write `for row in paginate(api.list_orders)` and never see page numbers. Rows are fetched lazily, one page at a time.

## Pitfalls

- Observers that publish events which trigger each other in a loop.
- State classes that reach into the context's internals; keep transitions explicit.
- Template method where steps must vary independently: you end up with a subclass per combination.

## Interview questions

- **Strategy vs state?** Same structure. Strategy is chosen from outside and rarely changes; state changes itself as events arrive.
- **How do you avoid observer memory leaks?** Hold subscribers by weak reference (`WeakMethod` for bound methods) or require explicit unsubscribe, and prune dead references on publish.
- **How does undo/redo work?** Two stacks of commands. Undo pops from undo and pushes to redo; any new command clears redo.
- **Observer vs pub/sub?** Observer is in-process and the subject knows its subscribers. Pub/sub puts a broker between them, so publishers and subscribers never reference each other.
- **Chain of responsibility vs decorator?** A decorator always delegates; a chain handler may stop the request.
