---
title: Design a vending machine
section: lld
order: 14
summary: Vending machine modeled with the state pattern, covering inventory, coin handling, change-making, cancel and refund flows.
---

## Requirements

- Products in slots, each with price and quantity.
- Accept coins/notes of fixed denominations.
- User selects product; machine dispenses if paid enough and in stock, returns change.
- User can cancel and get a refund at any time before dispensing.
- Admin: restock products, collect cash.
- Handle: out of stock, insufficient money, cannot make change.

## Why the state pattern

Every action (`insert_coin`, `select`, `dispense`, `cancel`) behaves differently depending on the machine's state. Without states you get a giant `if state == ...` in every method. With states, each class implements only the transitions legal from it.

```text
          insert            select (paid >= price, in stock)
  Idle ----------> HasMoney --------------------------------> Dispensing
   ^  <-- cancel --   |  ^                                         |
   |    (refund)      +--+ insert more                             |
   +---------------------- dispense product + change --------------+
```

## Entities

| Entity | Responsibility |
|--------|----------------|
| `Coin` | Enum of denominations (value in cents) |
| `Product` | Code, name, price |
| `Inventory` | Slot → product + count |
| `CashBox` | Denomination → count; makes change |
| `State` (ABC) | `insert`, `select`, `cancel` behavior |
| `IdleState`, `HasMoneyState`, `DispensingState` | Concrete states |
| `VendingMachine` | Context: holds state, balance, inventory, cash box |

## Class sketch

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class Coin(Enum):
    ONE = 100
    FIVE = 500
    TEN = 1000
    TWENTY = 2000


@dataclass(frozen=True)
class Product:
    code: str
    name: str
    price: int


class Inventory:
    def __init__(self):
        self.items: dict[str, list] = {}       # code -> [product, count]

    def add(self, p: Product, n: int) -> None:
        self.items.setdefault(p.code, [p, 0])[1] += n

    def available(self, code: str) -> bool:
        return code in self.items and self.items[code][1] > 0

    def take(self, code: str) -> Product:
        self.items[code][1] -= 1
        return self.items[code][0]


class CashBox:
    def __init__(self):
        self.coins = {c: 0 for c in Coin}

    def add(self, coins: list[Coin]) -> None:
        for c in coins:
            self.coins[c] += 1

    def make_change(self, amount: int) -> list[Coin] | None:
        """Greedy, largest first. Fine for canonical coin systems."""
        out, left = [], amount
        for c in sorted(Coin, key=lambda c: -c.value):
            n = min(left // c.value, self.coins[c])
            out += [c] * n
            left -= n * c.value
        if left:
            return None
        for c in out:
            self.coins[c] -= 1
        return out
```

## States

```python
class State(ABC):
    def __init__(self, vm: "VendingMachine"):
        self.vm = vm

    def insert(self, coin: Coin):
        raise RuntimeError("cannot insert now")

    def select(self, code: str):
        raise RuntimeError("cannot select now")

    def cancel(self) -> list[Coin]:
        return []


class IdleState(State):
    def insert(self, coin):
        self.vm.inserted.append(coin)
        self.vm.set_state(HasMoneyState(self.vm))


class HasMoneyState(State):
    def insert(self, coin):
        self.vm.inserted.append(coin)

    def select(self, code):
        vm = self.vm
        if not vm.inventory.available(code):
            raise ValueError("out of stock")
        price = vm.inventory.items[code][0].price
        paid = sum(c.value for c in vm.inserted)
        if paid < price:
            raise ValueError(f"insert {price - paid} more")
        vm.cashbox.add(vm.inserted)
        change = vm.cashbox.make_change(paid - price)
        if change is None:                     # roll back, refund
            for c in vm.inserted:
                vm.cashbox.coins[c] -= 1
            raise ValueError("cannot make change, please cancel")
        vm.set_state(DispensingState(vm))
        return vm.dispense(code, change)

    def cancel(self):
        refund, self.vm.inserted = self.vm.inserted, []
        self.vm.set_state(IdleState(self.vm))
        return refund


class DispensingState(State):
    pass        # every action rejected while the motor runs
```

## Context

```python
class VendingMachine:
    def __init__(self):
        self.inventory, self.cashbox = Inventory(), CashBox()
        self.inserted: list[Coin] = []
        self.state: State = IdleState(self)

    def set_state(self, s: State) -> None:
        self.state = s

    # public API delegates to current state
    def insert(self, coin: Coin): return self.state.insert(coin)
    def select(self, code: str): return self.state.select(code)
    def cancel(self): return self.state.cancel()

    def dispense(self, code: str, change: list[Coin]):
        product = self.inventory.take(code)
        self.inserted = []
        self.set_state(IdleState(self))
        return product, change
```

## Key flows

1. **Happy path:** Idle → `insert` → HasMoney → `insert`… → `select` → Dispensing → product + change → Idle.
2. **Insufficient funds:** `select` raises, stays in HasMoney.
3. **Cancel:** HasMoney → refund inserted coins → Idle.
4. **No change possible:** roll back cash box, ask user to cancel or insert exact amount.
5. **Out of stock:** rejected before taking money into the cash box.

## Patterns used

| Pattern | Where | Why |
|---------|-------|-----|
| State | `IdleState`, `HasMoneyState`, `DispensingState` | Removes conditionals; illegal actions fail by default |
| Strategy | Change-making, payment method (coins, card, UPI) | Swap greedy for DP; add card readers |
| Singleton | Machine per device | One controller for the hardware |
| Observer | Low-stock alerts to admin | Decouple monitoring |

## Pitfalls

- Putting state checks in the context (`if self.state == "idle"`); defeats the pattern.
- Taking the money into the cash box before checking stock and change.
- Greedy change fails for non-canonical denominations (e.g. 1, 3, 4 making 6). Use DP coin change with limited counts if asked.
- Floats for money.

## Extensions interviewers ask

- **Card / UPI payments:** `PaymentStrategy`; a `PaymentPendingState`.
- **Admin mode:** `MaintenanceState` allowing restock and cash collection, rejecting user actions.
- **Multiple quantities / cart:** select multiple items before paying.
- **Concurrency:** physical machines are single-user, but a networked one (remote purchase via app) needs a lock around `select`.
- **Timeout:** auto-cancel and refund if idle in HasMoney for 60 s.
