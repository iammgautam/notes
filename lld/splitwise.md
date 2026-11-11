---
title: Design Splitwise
section: lld
order: 11
summary: Expense sharing with equal, exact and percent split strategies, a net-balance ledger, settle-up, and greedy debt simplification.
---

## Requirements

- Users, and groups of users.
- Add an expense: payer, amount, participants, split type (equal, exact, percent).
- Show balances: who owes whom, per user and per group.
- Settle up: record a payment between two users.
- Simplify debts: minimize the number of transactions in a group.
- Out of scope: currency conversion, notifications, auth (mention as extensions).

## Entities

| Entity | Responsibility |
|--------|----------------|
| `User` | Id, name |
| `Group` | Members, expenses |
| `Expense` | Payer, amount, list of `Split`, split type |
| `Split` | User + amount owed (computed) |
| `SplitStrategy` | Turns input into splits; validates |
| `BalanceSheet` | Net balance per pair or per user |
| `ExpenseService` | Add expense, settle, query balances |

> **Rule of thumb:** Store money as integer paise/cents, never floats. Distribute rounding remainders deterministically.

## Class sketch

```python
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
import uuid


@dataclass(frozen=True)
class User:
    id: str
    name: str


@dataclass
class Split:
    user: User
    amount: int           # cents


class SplitType(Enum):
    EQUAL = "equal"
    EXACT = "exact"
    PERCENT = "percent"


class SplitStrategy(ABC):
    @abstractmethod
    def split(self, amount: int, users: list[User],
              values: list[float] | None) -> list[Split]: ...


class EqualSplit(SplitStrategy):
    def split(self, amount, users, values=None):
        base, rem = divmod(amount, len(users))
        # first `rem` users absorb one extra cent each
        return [Split(u, base + (1 if i < rem else 0)) for i, u in enumerate(users)]


class ExactSplit(SplitStrategy):
    def split(self, amount, users, values):
        if sum(values) != amount:
            raise ValueError("exact amounts must sum to total")
        return [Split(u, int(v)) for u, v in zip(users, values)]


class PercentSplit(SplitStrategy):
    def split(self, amount, users, values):
        if abs(sum(values) - 100) > 1e-9:
            raise ValueError("percents must sum to 100")
        splits = [Split(u, int(amount * p / 100)) for u, p in zip(users, values)]
        splits[0].amount += amount - sum(s.amount for s in splits)  # rounding
        return splits


STRATEGIES = {SplitType.EQUAL: EqualSplit(),
              SplitType.EXACT: ExactSplit(),
              SplitType.PERCENT: PercentSplit()}


@dataclass
class Expense:
    payer: User
    amount: int
    splits: list[Split]
    description: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
```

## Balance model

Keep **net balance per user** (positive = is owed, negative = owes) plus optional pairwise ledger for "who owes whom".

```python
class BalanceSheet:
    def __init__(self):
        # pair[(a, b)] > 0 means b owes a
        self.pair: dict[tuple[str, str], int] = defaultdict(int)
        self.net: dict[str, int] = defaultdict(int)

    def record(self, creditor: str, debtor: str, amount: int) -> None:
        if creditor == debtor or amount == 0:
            return
        self.pair[(creditor, debtor)] += amount
        self.pair[(debtor, creditor)] -= amount
        self.net[creditor] += amount
        self.net[debtor] -= amount


class ExpenseService:
    def __init__(self):
        self.sheet = BalanceSheet()
        self.expenses: list[Expense] = []

    def add_expense(self, payer: User, amount: int, users: list[User],
                    split_type: SplitType, values=None, desc="") -> Expense:
        splits = STRATEGIES[split_type].split(amount, users, values)
        exp = Expense(payer, amount, splits, desc)
        for s in splits:
            self.sheet.record(payer.id, s.user.id, s.amount)
        self.expenses.append(exp)
        return exp

    def settle(self, payer: User, payee: User, amount: int) -> None:
        # payer pays payee back: reduces what payer owes
        self.sheet.record(payer.id, payee.id, amount)
```

## Debt simplification

Goal: settle all net balances with few transactions. Greedy: repeatedly match the largest creditor with the largest debtor.

```python
import heapq


def simplify(net: dict[str, int]) -> list[tuple[str, str, int]]:
    creditors = [(-amt, u) for u, amt in net.items() if amt > 0]
    debtors = [(amt, u) for u, amt in net.items() if amt < 0]
    heapq.heapify(creditors)
    heapq.heapify(debtors)
    txns = []
    while creditors and debtors:
        c_amt, c = heapq.heappop(creditors)
        d_amt, d = heapq.heappop(debtors)
        pay = min(-c_amt, -d_amt)
        txns.append((d, c, pay))               # d pays c
        if -c_amt > pay:
            heapq.heappush(creditors, (c_amt + pay, c))
        if -d_amt > pay:
            heapq.heappush(debtors, (d_amt + pay, d))
    return txns
```

- Produces at most `n - 1` transactions, `O(n log n)`.
- True minimum is NP-hard (subset-sum style: find zero-sum subgroups). Greedy is the expected interview answer; say so.

## Key flows

1. **Add expense:** pick strategy → validate and compute splits → update balance sheet → store expense.
2. **Show balances:** read `pair` entries with positive values for a user.
3. **Settle:** record payment as a reverse balance entry.
4. **Simplify:** run greedy on group's `net` map, present suggested transfers.

## Patterns used

| Pattern | Where | Why |
|---------|-------|-----|
| Strategy | `SplitStrategy` | New split types (shares, adjustment) without touching service |
| Factory / registry | `STRATEGIES` map | Pick strategy from enum |
| Observer | Notify participants on new expense | Decouple notifications |
| Repository | Expense storage | Swap in-memory for DB |

## Concurrency

- Two expenses for the same group update the same balances: lock per group, or make the ledger append-only and derive balances.
- In PostgreSQL: insert `expense` + `split` rows in one transaction; compute balances with `SUM` or maintain them with `F()` updates.

## Extensions interviewers ask

- **Share-based split** (2:1:1): new `ShareSplit` strategy.
- **Edit / delete expense:** reverse its balance entries, then apply the new ones (keep history).
- **Multi-currency:** `Money(amount, currency)` value object; convert at expense time, store rate.
- **Recurring expenses:** scheduler creating expenses from a template (prototype).
- **Activity feed:** observer writing events.
- **Audit:** append-only ledger as the source of truth; balances are a cache.
