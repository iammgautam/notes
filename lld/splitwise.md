---
title: Design Splitwise
section: lld
order: 31
phase: "Phase 5 · Intermediate"
summary: Expense sharing with equal, exact and percent split strategies, Decimal money, a per-group balance sheet, settle-up and greedy debt simplification.
---

## Requirements

Clarifying questions worth asking:

- Are balances tracked per group, or across all of a user's groups? (Per group; the cross-group total is a sum.)
- Which split types? (Equal, exact amounts, percentages. Shares are a likely follow-up.)
- One currency or many? (One per group for now.)
- Does "simplify debts" change the ledger, or only suggest payments? (Only suggests.)
- Where does an odd cent go when 100.00 is split three ways? (Deterministically to the first participants.)

Functional:

- Create users and groups.
- Add an expense: payer, amount, participants, split type and values.
- Show who owes whom inside a group.
- Settle up: record a payment from one member to another.
- Suggest a short list of transfers that clears every balance in a group.

Non-functional:

- Money is exact. No floats, no lost or invented cents.
- Two members adding expenses at the same time never corrupt balances.
- A retried "add expense" request does not count the expense twice.

Out of scope: currency conversion, recurring expenses, notifications, auth.

## Entities and relationships

```text
+-------------------+  has *  +----------------------------+
| ExpenseService    |-------->| Group                      |
|-------------------|         |----------------------------|
| add_expense()     |         | id, members: set[str]      |
| settle()          |         | lock: Lock                 |
| balances()        |         | expenses: {id: Expense}    |
| suggest_transfers |         | sheet: BalanceSheet        |
+-------------------+         +----------------------------+
         | uses                   | has *            | has 1
         v                        v                  v
+-------------------+   +------------------+  +-------------------+
| <<abstract>>      |   | Expense          |  | BalanceSheet      |
| SplitStrategy     |   |------------------|  |-------------------|
|-------------------|   | id, paid_by      |  | owes[a][b]        |
| split(total,      |   | amount: Decimal  |  | record(d, c, amt) |
|   users, values)  |   | splits: [Split]  |  | net()             |
+-------------------+   +------------------+  +-------------------+
   ^ is-a                         | has *
   |                              v
EqualSplit, ExactSplit,     Split(user, amount)
PercentSplit
```

- `owes[a][b] > 0` means `a` owes `b`. The sheet keeps `owes[b][a] == -owes[a][b]`, so one lookup answers "who owes whom".
- `Group` owns the lock, so two groups never block each other.

## Patterns and why

| Pattern | Where | Why (what it prevents) |
|---|---|---|
| Strategy | `SplitStrategy` subclasses | An `if split_type == ...` chain inside `add_expense` that grows with every new split type |
| Registry | `STRATEGIES` dict keyed by `SplitType` | Callers constructing strategies or knowing class names |
| Value object | `Split`, `User`, `Expense` (frozen) | A split being changed after its balance was recorded |
| Settlement as an expense | `settle()` reuses `add_expense()` | A second write path for payments that drifts from the first |

## Code

```python
from __future__ import annotations

import heapq
import threading
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from enum import Enum

CENT = Decimal("0.01")


@dataclass(frozen=True)
class User:
    id: str
    name: str


@dataclass(frozen=True)
class Split:
    user: User
    amount: Decimal


class SplitStrategy(ABC):
    @abstractmethod
    def split(self, total: Decimal, users: list[User],
              values: list[Decimal]) -> list[Split]: ...


def spread(total: Decimal, users: list[User], raw: list[Decimal]) -> list[Split]:
    """Round each share down to a cent, then hand leftover cents out in order."""
    shares = [r.quantize(CENT, rounding=ROUND_DOWN) for r in raw]
    for i in range(int((total - sum(shares)) / CENT)):
        shares[i] += CENT
    return [Split(u, s) for u, s in zip(users, shares, strict=True)]


class EqualSplit(SplitStrategy):
    def split(self, total, users, values):
        return spread(total, users, [total / len(users)] * len(users))


class ExactSplit(SplitStrategy):
    def split(self, total, users, values):
        if sum(values) != total or any(v < 0 for v in values):
            raise ValueError("exact amounts must be >= 0 and add up to the total")
        return [Split(u, v) for u, v in zip(users, values, strict=True)]


class PercentSplit(SplitStrategy):
    def split(self, total, users, values):
        if sum(values) != 100:
            raise ValueError("percentages must add up to 100")
        return spread(total, users, [total * p / 100 for p in values])


class SplitType(Enum):
    EQUAL = "equal"
    EXACT = "exact"
    PERCENT = "percent"


STRATEGIES: dict[SplitType, SplitStrategy] = {
    SplitType.EQUAL: EqualSplit(),
    SplitType.EXACT: ExactSplit(),
    SplitType.PERCENT: PercentSplit(),
}


@dataclass(frozen=True)
class Expense:
    id: str                      # client-generated: doubles as idempotency key
    paid_by: User
    amount: Decimal
    splits: tuple[Split, ...]
    description: str


class BalanceSheet:
    def __init__(self) -> None:
        self.owes: dict[str, dict[str, Decimal]] = defaultdict(
            lambda: defaultdict(Decimal))

    def record(self, debtor: str, creditor: str, amount: Decimal) -> None:
        if debtor != creditor:
            self.owes[debtor][creditor] += amount
            self.owes[creditor][debtor] -= amount

    def net(self) -> dict[str, Decimal]:
        """Positive: the user is owed money overall. Negative: the user owes."""
        return {u: -sum(row.values(), Decimal(0)) for u, row in self.owes.items()}


@dataclass
class Group:
    id: str
    members: set[str]
    expenses: dict[str, Expense] = field(default_factory=dict)
    sheet: BalanceSheet = field(default_factory=BalanceSheet)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class ExpenseService:
    def __init__(self) -> None:
        self.groups: dict[str, Group] = {}

    def create_group(self, group_id: str, users: list[User]) -> Group:
        self.groups[group_id] = Group(group_id, {u.id for u in users})
        return self.groups[group_id]

    def add_expense(self, group_id: str, expense_id: str, paid_by: User,
                    amount: Decimal, users: list[User], split_type: SplitType,
                    values: list[Decimal] | None = None,
                    description: str = "") -> Expense:
        group = self.groups[group_id]
        if amount <= 0 or amount != amount.quantize(CENT):
            raise ValueError("amount must be positive and in whole cents")
        if not {paid_by.id, *(u.id for u in users)} <= group.members:
            raise ValueError("payer and participants must be group members")
        splits = tuple(STRATEGIES[split_type].split(amount, users, values or []))
        with group.lock:                        # check-then-write is one step
            if expense_id in group.expenses:    # client retry: no double count
                return group.expenses[expense_id]
            expense = Expense(expense_id, paid_by, amount, splits, description)
            for s in splits:
                group.sheet.record(s.user.id, paid_by.id, s.amount)
            group.expenses[expense_id] = expense
            return expense

    def settle(self, group_id: str, payment_id: str, payer: User,
               payee: User, amount: Decimal) -> Expense:
        """A settlement is an expense the payer covers entirely for the payee."""
        return self.add_expense(group_id, payment_id, payer, amount, [payee],
                                SplitType.EXACT, [amount], "settle up")

    def balances(self, group_id: str, user_id: str) -> dict[str, Decimal]:
        group = self.groups[group_id]
        with group.lock:
            return {o: a for o, a in group.sheet.owes[user_id].items() if a}

    def suggest_transfers(self, group_id: str) -> list[tuple[str, str, Decimal]]:
        group = self.groups[group_id]
        with group.lock:
            return simplify(group.sheet.net())


def simplify(net: dict[str, Decimal]) -> list[tuple[str, str, Decimal]]:
    """Greedy: largest debtor pays largest creditor. At most n - 1 transfers."""
    creditors = [(-amt, u) for u, amt in net.items() if amt > 0]
    debtors = [(amt, u) for u, amt in net.items() if amt < 0]
    heapq.heapify(creditors)
    heapq.heapify(debtors)
    transfers = []
    while creditors and debtors:
        c_amt, c = heapq.heappop(creditors)
        d_amt, d = heapq.heappop(debtors)
        pay = min(-c_amt, -d_amt)
        transfers.append((d, c, pay))             # d pays c
        if -c_amt > pay:
            heapq.heappush(creditors, (c_amt + pay, c))
        if -d_amt > pay:
            heapq.heappush(debtors, (d_amt + pay, d))
    return transfers


if __name__ == "__main__":
    from concurrent.futures import ThreadPoolExecutor

    D = Decimal
    a, b, c, d = (User(x, x.upper()) for x in "abcd")
    svc = ExpenseService()
    svc.create_group("trip", [a, b, c, d])

    e1 = svc.add_expense("trip", "e1", a, D("100.00"), [a, b, c], SplitType.EQUAL)
    assert [s.amount for s in e1.splits] == [D("33.34"), D("33.33"), D("33.33")]
    assert svc.add_expense("trip", "e1", a, D("100.00"), [a, b, c],
                           SplitType.EQUAL) is e1           # retry is a no-op
    svc.add_expense("trip", "e2", b, D("60.00"), [c, d], SplitType.EXACT,
                    [D("20.00"), D("40.00")])
    e3 = svc.add_expense("trip", "e3", c, D("10.01"), [a, b, c],
                         SplitType.PERCENT, [D(50), D(30), D(20)])
    assert [s.amount for s in e3.splits] == [D("5.01"), D("3.00"), D("2.00")]
    assert svc.balances("trip", "b") == {"a": D("33.33"), "c": D("-17.00"),
                                         "d": D("-40.00")}

    transfers = svc.suggest_transfers("trip")
    assert len(transfers) <= 3                   # n - 1 for four people
    for i, (payer, payee, amt) in enumerate(transfers):
        svc.settle("trip", f"pay{i}", User(payer, ""), User(payee, ""), amt)
    assert all(v == 0 for v in svc.groups["trip"].sheet.net().values())

    # Stress: 8 threads, 400 expenses, every request sent twice.
    svc.create_group("flat", [a, b, c])
    ids = [f"x{i}" for i in range(400)] * 2
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda k: svc.add_expense(
            "flat", k, a, D("10.00"), [a, b, c], SplitType.EQUAL), ids))
    net = svc.groups["flat"].sheet.net()
    assert len(svc.groups["flat"].expenses) == 400
    assert net["a"] == D("6.66") * 400 and sum(net.values()) == 0
    print("splitwise ok")
```

## Key flows

```text
Client         ExpenseService     SplitStrategy    Group.lock   BalanceSheet
  | add_expense(id,...) |               |               |             |
  |-------------------->| split()       |               |             |
  |                     |-------------->|               |             |
  |                     |<-- splits ----|               |             |
  |                     | acquire --------------------->|             |
  |                     | id seen? return existing      |             |
  |                     | record(debtor, payer, amt) per split ------>|
  |                     | release --------------------->|             |
  |<----- Expense ------|               |               |             |
```

```text
Client         ExpenseService       BalanceSheet        simplify()
  | suggest_transfers   |                  |                  |
  |-------------------->| net() under lock |                  |
  |                     |----------------->|                  |
  |                     |<-- {user: net} --|                  |
  |                     | max-heap creditors, debtors ------->|
  |                     |<--- [(debtor, creditor, amt)] ------|
  |<-- transfers -------|  (user pays; each is a settle())    |
```

## Edge cases

- 100.00 split three ways: shares are 33.34, 33.33, 33.33. The total always matches exactly.
- Payer is also a participant: their own share is skipped by `record` (`debtor == creditor`).
- Exact values that do not add up, negative values, or a length mismatch with users: reject.
- Percentages like 33.33 + 33.33 + 33.33 sum to 99.99: reject, or let the client send 33.34.
- Amount with sub-cent precision (`10.005`): reject at the boundary.
- A member leaving with a non-zero balance: block until settled.
- Settling more than owed flips the direction of the debt; decide whether to allow it.

## Production follow-ups

**How is it thread-safe?** Each group has its own lock, and the idempotency check, the balance updates and the expense insert all happen inside it. The GIL does not help: `owes[a][b] += x` is a read, an add and a write, and two threads can interleave between them. The strategy runs outside the lock to keep the critical section short. See [concurrency in LLD]({{ '/lld/concurrency-in-lld/' | relative_url }}).

**What survives a restart?** Store `expense` and `split` rows append-only; that is the source of truth. The balance sheet is a derived cache: rebuild it on startup by replaying expenses, or keep a `balance(group_id, debtor, creditor)` table updated in the same database transaction as the insert.

**What if the client retries "add expense"?** The client generates the expense id (a UUID) before the first attempt, and the server treats it as an idempotency key. In the database that is a unique constraint on `(group_id, expense_id)`; a duplicate insert returns the existing row instead of adding money twice. Settlements through a payment provider use the provider's transaction id the same way.

**How do you add a "shares" split (2:1:1)?** Write a `ShareSplit` that turns shares into fractions and calls `spread`, then add one registry entry. `ExpenseService` does not change, which is the open/closed principle doing its job.

**What breaks at 10× or across machines?** The in-process lock stops working once two app servers handle the same group. Replace it with a row lock (`SELECT ... FOR UPDATE` on the group row) or an optimistic `version` column on the group. Groups are independent, so shard by `group_id`; a user's total across groups becomes a fan-out read or a per-user total updated asynchronously.

**What do you log and measure?** Log expense id, group, amount, split type and payer on every write. Measure write latency, idempotent replays and lock wait time. Run a periodic job that asserts each group's net balances sum to zero and alert if they ever do not.

**How do you keep money exact?** Use `Decimal` (or integer minor units) end to end and reject sub-cent input. Rounding happens once, inside `spread`, and the leftover cents go to participants in a fixed order, so the same input always gives the same split. Store the currency code with the group so two currencies are never summed.

**How do you edit or delete an expense?** Never mutate the old one. Record a reversing entry (the same splits, negated), then record the new expense. History stays auditable and balances stay correct.

## Interview follow-ups

- **Is the greedy simplification optimal?** No. The true minimum number of transfers is NP-hard (it needs zero-sum subsets). Greedy gives at most `n - 1` transfers in `O(n log n)`, which is good enough in practice.
- **Should simplify change the ledger?** No. It only suggests transfers; balances change when users actually pay and a settlement is recorded.
- **Multi-currency?** Add a `Money(amount, currency)` value object, convert at expense time and store the rate used, so history does not move with exchange rates.
- **Notifications and an activity feed?** Publish an `ExpenseAdded` event after the lock is released and let observers send emails or write the feed. See the [notification system]({{ '/lld/notification-system/' | relative_url }}).
- **Why not store only a net balance per user?** Net is enough for simplification, but users ask "how much do I owe Bob?", which needs the pairwise view.
