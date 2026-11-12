---
title: Design a vending machine
section: lld
order: 21
phase: "Phase 5 · Beginner"
summary: State pattern vending machine with inventory, coins and notes, correct change-making from limited stock, cancel and refund, and a maintenance mode.
---

## Requirements

Clarifying questions to ask first:

- Pay first, then select? Or select, then pay?
- Which coins and notes? Is the change supply limited (it always is)?
- One item per session, or keep the leftover credit for a second item?
- Cash only, or card and UPI too?
- What happens if the machine cannot make change?
- Who restocks, and can they do it while a customer is mid-purchase?

Functional:

- Accept coins and notes of fixed denominations; show credit.
- Select a product by slot code; dispense if in stock and paid enough; return change.
- Change comes only from coins actually in the machine, using the fewest pieces.
- Cancel at any time before dispensing returns exactly the pieces inserted.
- Maintenance mode: restock slots, load change; customer actions are rejected.

Non-functional: money is never lost or created (every rupee is in the cash box, in escrow, or returned); events from the coin acceptor, keypad and admin panel can arrive on different threads.

Out of scope: card payments (follow-up), multi-item cart, the hardware drivers.

## Entities and relationships

```text
+-------------------------------+         +-------------------------+
| VendingMachine (context)      | 1     1 | State                   |
| slots: dict[str, Slot]        |-------->| insert(d), select(code) |
| cash: Counter[Denom]          | current | cancel(), start_service |
| escrow: Counter[Denom]        |         | restock, load_cash      |
| insert/select/cancel/...      |         | end_service             |
|   -> lock, delegate to state  |         +-------------------------+
+---------------+---------------+            ^ is-a   ^ is-a   ^ is-a
                | has 0..*                 Idle   HasMoney  Maintenance
                v
+-------------------------+     +-------------------+
| Slot                    |---->| Product (frozen)  |
| product, qty            | has | code, name, price |
+-------------------------+     +-------------------+

Denom (IntEnum): 1, 2, 5, 10, 20, 50, 100
make_change(amount, stock) -> Counter | None    (used by HasMoney)
```

State transitions:

```text
            insert                    select ok (dispense + change)
   Idle ------------> HasMoney ----------------------------------+
    ^  ^               |  ^ |                                    |
    |  +--- cancel ----+  +-+ insert more / select fails         |
    |      (refund)                                              |
    +------------------------------------------------------------+
    |  start_service          end_service
    +-----------------> Maintenance ---------> Idle
```

## Patterns and why

| Pattern | Where | Why (what it prevents) |
|---------|-------|------------------------|
| State | `Idle`, `HasMoney`, `Maintenance` | No `if state == ...` in every method; an action not overridden by a state is illegal by default |
| Facade | `VendingMachine` | Hardware and UI call one object; the lock and delegation live in one place |
| Strategy (extension) | `PaymentMethod`: cash, card | Add a payment type without touching states (see follow-ups) |
| Observer (extension) | Low-stock and low-change alerts | Monitoring without coupling to the purchase flow |

## Code

```python
import sys
import threading
from collections import Counter
from dataclasses import dataclass
from enum import IntEnum


class Denom(IntEnum):              # whole rupees; money is never a float
    ONE = 1
    TWO = 2
    FIVE = 5
    TEN = 10
    TWENTY = 20
    FIFTY = 50
    HUNDRED = 100


@dataclass(frozen=True)
class Product:
    code: str
    name: str
    price: int


@dataclass
class Slot:
    product: Product
    qty: int


class VendingError(Exception):
    pass


def make_change(amount: int, stock: Counter) -> Counter | None:
    """Fewest pieces for `amount` using only pieces in `stock`.
    Greedy is wrong here: 6 from (one 5, three 2s) takes the 5, then is stuck."""
    best: dict[int, Counter] = {0: Counter()}        # reachable amount -> pieces
    for d in sorted(stock, reverse=True):
        for _ in range(min(stock[d], amount // d)):  # each physical piece once
            for a in sorted(best, reverse=True):     # high to low: no reuse per pass
                t = a + d
                if t <= amount and (t not in best
                                    or best[a].total() + 1 < best[t].total()):
                    best[t] = best[a] + Counter({d: 1})
    return best.get(amount)


class State:
    """Default: every action is illegal. Each state overrides what it allows."""

    def __init__(self, vm: "VendingMachine"):
        self.vm = vm

    def _illegal(self, action: str):
        raise VendingError(f"cannot {action} while {type(self).__name__}")

    def insert(self, d: Denom): self._illegal("insert money")
    def select(self, code: str): self._illegal("select")
    def cancel(self): self._illegal("cancel")
    def start_service(self): self._illegal("start service")
    def restock(self, product: Product, qty: int): self._illegal("restock")
    def load_cash(self, pieces: Counter): self._illegal("load cash")
    def end_service(self): self._illegal("end service")


class Idle(State):
    def insert(self, d):
        self.vm.escrow[d] += 1
        self.vm.state = HasMoney(self.vm)

    def cancel(self):
        return Counter()                         # nothing to refund

    def start_service(self):
        self.vm.state = Maintenance(self.vm)


class HasMoney(State):
    def insert(self, d):
        self.vm.escrow[d] += 1

    def select(self, code):
        vm = self.vm
        slot = vm.slots.get(code)
        if slot is None or slot.qty == 0:
            raise VendingError(f"{code} is sold out")
        paid, price = vm.credit(), slot.product.price
        if paid < price:
            raise VendingError(f"insert {price - paid} more")
        change = make_change(paid - price, vm.cash + vm.escrow)
        if change is None:
            raise VendingError("cannot make change: use exact amount or cancel")
        vm.cash += vm.escrow                     # commit only after every check
        vm.cash -= change
        vm.escrow = Counter()
        slot.qty -= 1
        vm.state = Idle(vm)
        return slot.product, change

    def cancel(self):
        refund, self.vm.escrow = self.vm.escrow, Counter()
        self.vm.state = Idle(self.vm)
        return refund                            # the exact pieces inserted


class Maintenance(State):
    def restock(self, product, qty):
        self.vm.slots.setdefault(product.code, Slot(product, 0)).qty += qty

    def load_cash(self, pieces):
        self.vm.cash += pieces

    def end_service(self):
        self.vm.state = Idle(self.vm)


class VendingMachine:
    def __init__(self):
        self.slots: dict[str, Slot] = {}
        self.cash: Counter = Counter()           # Denom -> pieces in the cash box
        self.escrow: Counter = Counter()         # customer's money, not yet ours
        self.state: State = Idle(self)
        self._lock = threading.Lock()            # coin, keypad, admin events race

    def credit(self) -> int:
        return sum(d * n for d, n in self.escrow.items())

    def _do(self, action: str, *args):
        with self._lock:                         # each transition is atomic
            return getattr(self.state, action)(*args)

    def insert(self, d: Denom): return self._do("insert", d)
    def select(self, code: str): return self._do("select", code)
    def cancel(self): return self._do("cancel")
    def start_service(self): return self._do("start_service")
    def restock(self, p: Product, qty: int): return self._do("restock", p, qty)
    def load_cash(self, pieces: Counter): return self._do("load_cash", pieces)
    def end_service(self): return self._do("end_service")


if __name__ == "__main__":
    def fails(fn, *args) -> str:
        try:
            fn(*args)
        except VendingError as e:
            return str(e)
        raise AssertionError("expected VendingError")

    def value(c: Counter) -> int:
        return sum(d * n for d, n in c.items())

    vm = VendingMachine()
    vm.start_service()
    vm.restock(Product("A1", "Chips", 14), 2)
    vm.load_cash(Counter({Denom.FIVE: 1, Denom.TWO: 3}))   # no 1-rupee coins
    vm.end_service()

    assert "cannot select" in fails(vm.select, "A1")       # Idle: pay first
    vm.insert(Denom.TEN)
    assert "insert 4 more" in fails(vm.select, "A1")
    vm.insert(Denom.TEN)                                   # credit 20, change 6
    product, change = vm.select("A1")
    assert product.name == "Chips" and change == Counter({Denom.TWO: 3})
    assert vm.cash == Counter({Denom.FIVE: 1, Denom.TEN: 2})

    vm.insert(Denom.FIFTY)                                 # change 36: impossible
    assert "cannot make change" in fails(vm.select, "A1")
    assert vm.cancel() == Counter({Denom.FIFTY: 1})        # exact pieces back
    assert "cannot restock" in fails(vm.restock, product, 1)

    sys.setswitchinterval(1e-6)                  # switch threads often so races show up
    # stress: 8 shoppers on 8 threads, 100 sodas, 400 purchase attempts
    vm.start_service()
    vm.restock(Product("B1", "Soda", 20), 100)
    vm.end_service()
    cash_before, sold, returned = value(vm.cash), [], []

    def shopper() -> None:
        for _ in range(50):
            vm.insert(Denom.TWENTY)
            try:
                p, ch = vm.select("B1")
                sold.append(p)
                returned.append(value(ch))
            except VendingError:
                pass                     # sold out, or another thread spent it

    threads = [threading.Thread(target=shopper) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    refund = value(vm.cancel())
    gained = value(vm.cash) - cash_before
    assert len(sold) + vm.slots["B1"].qty == 100           # no item lost
    assert gained == 20 * len(sold)                        # paid for each one
    assert gained + sum(returned) + refund == 8 * 50 * 20  # no money lost
    print("ok")
```

## Key flows

Happy path with change from limited stock:

```text
customer     VendingMachine      HasMoney         make_change      Slot
   | insert(10)    |                 |                  |            |
   |-------------->| lock, delegate  |                  |            |
   |               |---------------->| escrow += 10     |            |
   | select(A1)    |                 |                  |            |
   |-------------->|---------------->| in stock? -------------------->|
   |               |                 | credit >= price? |            |
   |               |                 |--(6, cash+escrow)>|            |
   |               |                 |<-- {2: 3} -------|            |
   |               |                 | cash += escrow - change       |
   |               |                 | qty -= 1 ------------------->|
   |               |                 | state = Idle     |            |
   |<-- (Chips, three 2s) -----------|                  |            |
```

Cancel and refund:

```text
customer     VendingMachine      HasMoney
   | cancel()      |                 |
   |-------------->| lock, delegate  |
   |               |---------------->| refund = escrow; escrow = {}
   |               |                 | state = Idle
   |<-- the exact coins inserted ----|
```

## Edge cases

- Sold out: rejected before any money moves; credit stays in escrow.
- Not enough credit: stay in `HasMoney`, say how much more.
- Cannot make change: stay in `HasMoney`; customer adds exact money or cancels.
- Escrow is separate from the cash box, so a refund returns the same pieces even if the box has none of that denomination.
- Change can use the customer's own coins: paying 20 with two 10s for a 10 item returns a 10.
- Admin restock while a customer has credit: `HasMoney` rejects `start_service`; refund first.
- Greedy change-making fails with limited stock even for "nice" denominations; the bounded DP above is `O(pieces × amount)`, tiny at vending sizes.

## Production follow-ups

**Is it thread-safe?** Coin, keypad and admin events come from different driver threads, so every public call takes one lock and runs a whole transition. Without it, two `select` calls could both see `qty == 1`. The GIL does not make the check-then-decrement atomic. One lock is right here: a machine handles one customer at a time, so there is no throughput to gain from finer locks.

**What about power loss and restart?** Persist state, escrow and slot counts to local flash (SQLite works) before moving the motor, and write "dispense started" and "dispense confirmed" records. On boot, a started-but-unconfirmed dispense is resolved with the drop sensor: confirmed means keep the money, otherwise refund and mark the slot jammed.

**Idempotency and retries?** Debounced buttons can fire twice; the second `select` lands in `Idle` and is rejected by the state machine. For remote purchases from an app, require an idempotency key per order so a retried request does not dispense twice. If the motor fails, refund instead of retrying blindly; the item may be stuck halfway.

**How do you add card or UPI payment?** Introduce a `PaymentMethod` strategy with `authorize(amount)`, `capture()` and `void()`. `CashPayment` wraps the escrow logic; `CardPayment` authorizes before dispensing, captures after the drop sensor confirms, and voids on failure. States call the strategy, so adding a payment type touches no state class.

**What changes for a fleet of 10,000 machines?** Each machine stays a single-threaded state machine. Add telemetry over MQTT, a central inventory and pricing service, and pushed price updates applied only in `Idle` or `Maintenance`.

**What would you log and measure?** Every state transition with a session id, sales by slot, refunds, "cannot make change" rate (the float is too low), cash box levels per denomination, jam rate per slot. Low stock and low change trigger alerts through an observer.

**How do you avoid money precision bugs?** Integers in the smallest unit (paise if prices have them), never floats. `0.1 + 0.2 != 0.3` turns into lost change.

**How does a timeout refund work?** A timer auto-cancels after 60 s idle in `HasMoney`. The timer thread must take the same lock and check the session is still the one it was armed for, or it may refund the next customer's money.

## Interview follow-ups

- **Why not an enum plus `if` statements?** Every method would branch on every state, and adding `Maintenance` means editing all of them. With State, illegal actions fail by default and a new state is a new class.
- **Who owns transitions: states or the context?** Here states set `vm.state`. Centralizing transitions in a table is fine too; say which and keep it consistent.
- **Would you keep leftover credit after a purchase?** It is a product decision. Supporting it means `select` returns to `HasMoney` with the remainder instead of making change.
- **When is greedy change-making correct?** For canonical coin systems with unlimited supply. Real machines have limited supply, so use the bounded DP or fall back to "exact change only".
