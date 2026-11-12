---
title: "Mini designs: printer, bank account, car"
section: lld
order: 4
phase: "Phase 1 · OOP foundations"
summary: Three warm-up designs in runnable Python (a printer queue, a bank account, a car with swappable engines) and what each one tests.
---

Common warm-ups, or the first 15 minutes of a longer round. Each exercises one OOP idea in about 60 lines.

| Problem | Core idea | Pattern inside |
|---|---|---|
| Printer | Abstraction over types, explicit states, a queue | Template method |
| Bank account | Encapsulation, invariants, money | Strategy |
| Car | Composition, dependency injection | Strategy (the engine) |

## Printer

FIFO job queue, printer states, jobs held when paper runs out, laser and inkjet types.

```python
import itertools
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from enum import Enum


class PrinterState(Enum):
    IDLE = "idle"
    PRINTING = "printing"
    OUT_OF_PAPER = "out_of_paper"


@dataclass(frozen=True)
class PrintJob:
    id: int
    document: str
    pages: int


class Printer(ABC):
    _ids = itertools.count(1)           # job ids unique across printers

    def __init__(self, paper: int):
        self.state = PrinterState.IDLE
        self._paper = paper
        self._queue: deque[PrintJob] = deque()

    def submit(self, document: str, pages: int) -> PrintJob:
        if pages <= 0:
            raise ValueError("pages must be positive")
        job = PrintJob(next(Printer._ids), document, pages)
        self._queue.append(job)
        return job

    def print_next(self) -> str | None:
        if not self._queue:
            return None
        job = self._queue[0]            # peek: job waits if paper is short
        if job.pages > self._paper:
            self.state = PrinterState.OUT_OF_PAPER
            return None
        self.state = PrinterState.PRINTING
        self._queue.popleft()
        self._paper -= job.pages
        output = self._render(job)
        self.state = PrinterState.IDLE
        return output

    def load_paper(self, sheets: int) -> None:
        self._paper += sheets
        if self.state is PrinterState.OUT_OF_PAPER:
            self.state = PrinterState.IDLE

    @property
    def pending(self) -> int:
        return len(self._queue)

    @abstractmethod
    def _render(self, job: PrintJob) -> str: ...   # the part that varies


class LaserPrinter(Printer):
    def _render(self, job: PrintJob) -> str:
        return f"laser mono: {job.document} ({job.pages}p)"


class InkjetPrinter(Printer):
    def _render(self, job: PrintJob) -> str:
        return f"inkjet color: {job.document} ({job.pages}p)"


if __name__ == "__main__":
    p = LaserPrinter(paper=5)
    p.submit("report.pdf", 3)
    p.submit("slides.pdf", 4)
    assert p.print_next() == "laser mono: report.pdf (3p)"
    assert p.print_next() is None and p.state is PrinterState.OUT_OF_PAPER
    assert p.pending == 1               # job kept, not dropped
    p.load_paper(10)
    assert p.print_next() == "laser mono: slides.pdf (4p)"
    assert p.state is PrinterState.IDLE and p.pending == 0
```

What the interviewer is checking:

- **Abstraction**: queue and state logic live once in `Printer`; subclasses override only `_render`. No `if printer_type == "laser"`.
- **Explicit state**: an `Enum`, changed only inside the class, with a recovery path (`load_paper`).
- **No lost work**: a job that cannot print stays at the head of the queue. Jobs are frozen once queued.
- **Follow-up**: with a background worker, `PRINTING` becomes observable and the queue must be thread-safe (`queue.Queue`). See [Producer-consumer]({{ '/lld/producer-consumer/' | relative_url }}).

## Bank account

Deposit, withdraw, read-only balance, history, exact money, and account types with different withdrawal rules.

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

CENT = Decimal("0.01")


def to_money(value: Decimal | str | int) -> Decimal:
    if isinstance(value, float):
        raise TypeError("use Decimal or str for money, not float")
    amount = Decimal(value)
    if amount != amount.quantize(CENT):
        raise ValueError("more than two decimal places")
    if amount <= 0:
        raise ValueError("amount must be positive")
    return amount.quantize(CENT)


class TxnType(Enum):
    DEPOSIT = "deposit"
    WITHDRAW = "withdraw"


@dataclass(frozen=True)
class Transaction:
    type: TxnType
    amount: Decimal
    balance_after: Decimal
    at: datetime


class WithdrawalPolicy(ABC):
    @abstractmethod
    def allows(self, balance: Decimal, amount: Decimal) -> bool: ...


class MinimumBalance(WithdrawalPolicy):     # savings
    def __init__(self, minimum: Decimal):
        self.minimum = minimum

    def allows(self, balance: Decimal, amount: Decimal) -> bool:
        return balance - amount >= self.minimum


class OverdraftLimit(WithdrawalPolicy):     # current account
    def __init__(self, limit: Decimal):
        self.limit = limit

    def allows(self, balance: Decimal, amount: Decimal) -> bool:
        return balance - amount >= -self.limit


class BankAccount:
    def __init__(self, policy: WithdrawalPolicy):
        self.policy = policy
        self._balance = Decimal("0.00")
        self._history: list[Transaction] = []

    @property
    def balance(self) -> Decimal:
        return self._balance

    @property
    def history(self) -> tuple[Transaction, ...]:
        return tuple(self._history)         # callers cannot mutate it

    def deposit(self, value: Decimal | str | int) -> None:
        amount = to_money(value)
        self._apply(TxnType.DEPOSIT, amount, self._balance + amount)

    def withdraw(self, value: Decimal | str | int) -> None:
        amount = to_money(value)
        if not self.policy.allows(self._balance, amount):
            raise ValueError(f"withdrawal of {amount} not allowed")
        self._apply(TxnType.WITHDRAW, amount, self._balance - amount)

    def _apply(self, kind: TxnType, amount: Decimal, new: Decimal) -> None:
        self._balance = new                 # the one place balance changes
        txn = Transaction(kind, amount, new, datetime.now(UTC))
        self._history.append(txn)


def raises(exc: type[Exception], fn, *args) -> bool:
    try:
        fn(*args)
    except exc:
        return True
    return False


if __name__ == "__main__":
    savings = BankAccount(MinimumBalance(Decimal("100")))
    savings.deposit("500.10")
    savings.withdraw("400.10")
    assert savings.balance == Decimal("100.00")
    assert raises(ValueError, savings.withdraw, "0.01")  # below minimum
    for bad in ("0", "-5", "1.001"):
        assert raises(ValueError, savings.deposit, bad)
    assert raises(TypeError, savings.deposit, 0.1)
    kinds = [t.type for t in savings.history]
    assert kinds == [TxnType.DEPOSIT, TxnType.WITHDRAW]

    current = BankAccount(OverdraftLimit(Decimal("1000")))
    current.withdraw("250")
    assert current.balance == Decimal("-250.00")
    assert raises(ValueError, current.withdraw, "750.01")
```

Account types: inheritance or strategy?

| | Inheritance: `SavingsAccount(BankAccount)` | Strategy: `BankAccount(policy=...)` |
|---|---|---|
| New type | Subclass overriding a hook | New policy class |
| Change rules at runtime | Build a new object | Swap `account.policy` |
| Pick when | Types differ in many behaviors (interest, fees) | One rule varies |

These two policies differ only by sign, so one `floor` field would do. The strategy earns its place once rules differ in shape: daily limits, withdrawal counts, holds.

What the interviewer is checking:

- **Encapsulation**: no setter on `balance`; every change goes through `_apply`, which also writes history, so the two cannot disagree.
- **Invariants at the boundary**: positive, at most two decimals, policy checked before any state changes.
- **Money**: `Decimal` from strings. `float` is rejected: `0.1 + 0.2 != 0.3`, and `Decimal(0.1)` copies the binary error.
- **Follow-up**: two concurrent withdrawals both pass the check and overdraw. Lock check-and-apply per account; for transfers, lock both accounts in a fixed order. See [Python concurrency primitives]({{ '/lld/python-concurrency-primitives/' | relative_url }}).

## Car with an engine

A car with a petrol or electric engine, swappable while the car is off.

```python
from typing import Protocol


class Engine(Protocol):
    def start(self) -> str: ...
    def range_km(self) -> int: ...


class PetrolEngine:
    def __init__(self, fuel_litres: int):
        self.fuel_litres = fuel_litres

    def start(self) -> str:
        if self.fuel_litres <= 0:
            raise RuntimeError("no fuel")
        return "petrol engine running"

    def range_km(self) -> int:
        return self.fuel_litres * 15


class ElectricEngine:
    def __init__(self, charge_pct: int):
        self.charge_pct = charge_pct

    def start(self) -> str:
        if self.charge_pct < 5:
            raise RuntimeError("battery too low")
        return "electric motor running"

    def range_km(self) -> int:
        return self.charge_pct * 4


class Car:
    def __init__(self, engine: Engine):     # injected, not built here
        self._engine = engine
        self._running = False

    def start(self) -> str:
        if self._running:
            return "already running"
        message = self._engine.start()      # no type checks, just delegate
        self._running = True
        return message

    def stop(self) -> None:
        self._running = False

    def range_km(self) -> int:
        return self._engine.range_km()

    def swap_engine(self, engine: Engine) -> None:
        if self._running:
            raise RuntimeError("stop the car before swapping the engine")
        self._engine = engine


if __name__ == "__main__":
    car = Car(PetrolEngine(fuel_litres=20))
    assert car.start() == "petrol engine running" and car.range_km() == 300
    try:
        car.swap_engine(ElectricEngine(80))
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass
    car.stop()
    car.swap_engine(ElectricEngine(charge_pct=80))
    assert car.start() == "electric motor running" and car.range_km() == 320
```

What the interviewer is checking:

- **Composition over inheritance**: one `Car` plus engines, not `PetrolCar`, `ElectricCar`, `HybridCar`. A hybrid is a new engine class.
- **Dependency injection**: nothing inside `Car` names a concrete engine, so a test can pass a two-method fake.
- **State guard**: no swap while running.
- **UML nuance**: the engine is passed in and can outlive the car, so strictly this is aggregation. "Composition over inheritance" means has-a in general.

## Pitfalls

- Public state changed from outside (`account.balance -= x`).
- Silently rounding bad money input instead of rejecting it.
- `self.engine = PetrolEngine()` inside `__init__`: no swapping, no fakes.

## Interview questions

- **Why not `ElectricCar(Car)`?** Engine is one axis of variation; body style and transmission are others. Subclassing multiplies classes per combination; composition adds one class per option.
- **Where does the queue go in a multi-printer office?** A `PrintSpooler` owns the queue and assigns jobs to idle printers by a strategy (least busy, nearest). Printers just print.
