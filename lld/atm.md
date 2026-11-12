---
title: Design an ATM
section: lld
order: 34
phase: "Phase 5 · Intermediate"
summary: ATM as a state machine with a bank interface, a chain-of-responsibility cash dispenser, PIN lockout, Decimal money and idempotent debits.
---

## Requirements

Clarifying questions to ask first:

- Which operations? (Withdraw and balance. Deposits and transfers are extensions.)
- Who checks the PIN and owns the balance? (The bank, over a network. The ATM never holds balances.)
- Which notes, and what if the exact amount can't be made? (500, 200, 100. Refuse before debiting.)
- Wrong PIN policy? (Three wrong attempts block the card and the machine keeps it.)

**Functional**

- Insert card, enter PIN, withdraw or check balance, eject.
- Out-of-order actions are refused (withdraw before PIN, PIN with no card).
- Withdraw: confirm the machine can make the amount, debit the account, dispense.
- Three wrong PINs: the bank blocks the card and the ATM retains it.
- If dispensing fails after the debit, reverse the debit.

**Non-functional**

- Money is `Decimal`, never `float`.
- A debit retried after a lost response charges once.
- Many ATMs can debit one account at the same time without overdrawing it.

**Out of scope:** deposits, receipts, daily limits, real PIN encryption, cash refills.

## Entities and relationships

```text
+------------------------------+ has  +----------------------------+
| ATM                          |----->| State                      |
| state, card, retained        |      | insert_card, enter_pin,    |
| insert_card(card)            |      | withdraw, balance, eject   |
| enter_pin(pin) -> PinResult  |      | (default: InvalidAction)   |
| withdraw(amount) -> notes    |      +----------------------------+
| balance(), eject()           |        ^ is-a: Idle, CardInserted,
+------------------------------+        |  Authenticated, InTransaction
     | uses              | has 1
     v                   v
+--------------------------+  +----------------------------+
| BankService (Protocol)   |  | CashDispenser              |
| verify_pin(card, pin)    |  | head: NoteSlot             |
| balance(account)         |  | plan(amount) -> notes      |
| debit(acct, amt, txn_id) |  | dispense(notes)            |
| reverse(txn_id)          |  +----------------------------+
+--------------------------+         | has 1..* (chain)
  ^ is-a: InMemoryBank               v
                              +----------------------------+
                              | NoteSlot                   |
                              | value, count, next         |
                              | plan(amount, out) -> rest  |
                              +----------------------------+

Idle --insert_card--> CardInserted --PIN OK--> Authenticated
CardInserted --eject--> Idle
CardInserted --3rd wrong PIN--> Idle (card retained)
Authenticated --withdraw--> InTransaction --done/failed--> Authenticated
Authenticated --eject--> Idle
```

## Patterns and why

| Pattern | Where | Why |
|---|---|---|
| State | `Idle`, `CardInserted`, `Authenticated`, `InTransaction` | Each action is legal only in some states; the base class refuses by default, so there is no `has_card and is_authed` flag soup |
| Chain of responsibility | `NoteSlot` links, largest note first | Each slot takes what it can and passes the rest on; a new note is one more link |
| Dependency inversion | `BankService` protocol | The ATM is tested with a fake bank; the network client plugs in later |
| Idempotency key | `txn_id` on `debit` and `reverse` | A retry after a lost response is a no-op, not a second charge |

## Code

```python
import hashlib
import hmac
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Protocol


class InvalidAction(Exception): pass
class InsufficientFunds(Exception): pass
class CannotDispense(Exception): pass


class PinResult(Enum):
    OK = "ok"
    WRONG = "wrong"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Card:
    number: str
    account: str


class BankService(Protocol):
    def verify_pin(self, card: Card, pin: str) -> PinResult: ...
    def balance(self, account: str) -> Decimal: ...
    def debit(self, account: str, amount: Decimal, txn_id: str) -> None: ...
    def reverse(self, txn_id: str) -> None: ...


def _hash(pin: str) -> str:  # stand-in: real banks verify PIN blocks in an HSM
    return hashlib.sha256(pin.encode()).hexdigest()


class InMemoryBank:
    MAX_PIN_ATTEMPTS = 3

    def __init__(self):
        self.balances: dict[str, Decimal] = {}
        self.pins: dict[str, str] = {}
        self.failures: dict[str, int] = {}
        self.debits: dict[str, tuple[str, Decimal]] = {}  # txn_id -> (account, amount)
        self.reversed: set[str] = set()
        # Simplification: one lock for every account; per-account row locks in a real ledger
        self._lock = threading.Lock()

    def open(self, card: Card, pin: str, balance: Decimal) -> None:
        self.balances[card.account], self.pins[card.number] = balance, _hash(pin)

    def verify_pin(self, card, pin):
        with self._lock:
            if self.failures.get(card.number, 0) >= self.MAX_PIN_ATTEMPTS:
                return PinResult.BLOCKED
            if hmac.compare_digest(self.pins[card.number], _hash(pin)):
                self.failures[card.number] = 0
                return PinResult.OK
            self.failures[card.number] = self.failures.get(card.number, 0) + 1
            blocked = self.failures[card.number] >= self.MAX_PIN_ATTEMPTS
            return PinResult.BLOCKED if blocked else PinResult.WRONG

    def balance(self, account):
        with self._lock:
            return self.balances[account]

    def debit(self, account, amount, txn_id):
        with self._lock:
            if txn_id in self.debits:  # retry of an applied debit
                return
            if self.balances[account] < amount:
                raise InsufficientFunds(account)
            self.balances[account] -= amount
            self.debits[txn_id] = (account, amount)

    def reverse(self, txn_id):
        with self._lock:
            if txn_id in self.debits and txn_id not in self.reversed:
                account, amount = self.debits[txn_id]
                self.balances[account] += amount
                self.reversed.add(txn_id)


class NoteSlot:
    """One link: hand out as many of my notes as fit, pass the rest on."""

    def __init__(self, value: int, count: int, next_: "NoteSlot | None"):
        self.value, self.count, self.next = value, count, next_

    def plan(self, amount: int, out: dict[int, int]) -> int:
        n = min(amount // self.value, self.count)
        if n:
            out[self.value] = n
        rest = amount - n * self.value
        return self.next.plan(rest, out) if self.next and rest else rest


class CashDispenser:
    def __init__(self, notes: dict[int, int]):
        self.head = None
        for value in sorted(notes):  # prepend ascending -> chain is largest first
            self.head = NoteSlot(value, notes[value], self.head)

    def plan(self, amount: int) -> dict[int, int]:
        """Pick notes without touching the stock."""
        out: dict[int, int] = {}
        if self.head is None or self.head.plan(amount, out):
            raise CannotDispense(amount)
        return out

    def dispense(self, notes: dict[int, int]) -> None:  # the hardware call
        slot = self.head
        while slot:
            slot.count -= notes.get(slot.value, 0)
            slot = slot.next


class State:
    def _deny(self, action: str):
        raise InvalidAction(f"{action} not allowed while {type(self).__name__}")

    def insert_card(self, atm, card): self._deny("insert_card")
    def enter_pin(self, atm, pin): self._deny("enter_pin")
    def withdraw(self, atm, amount): self._deny("withdraw")
    def balance(self, atm): self._deny("balance")
    def eject(self, atm): self._deny("eject")


class Idle(State):
    def insert_card(self, atm, card):
        atm.card, atm.state = card, CardInserted()


class CardInserted(State):
    def enter_pin(self, atm, pin):
        result = atm.bank.verify_pin(atm.card, pin)
        if result is PinResult.OK:
            atm.state = Authenticated()
        elif result is PinResult.BLOCKED:
            atm.retained.append(atm.card)
            atm.card, atm.state = None, Idle()
        return result

    def eject(self, atm):
        return atm.release_card()


class Authenticated(State):
    def withdraw(self, atm, amount):
        atm.state = InTransaction()
        try:
            return atm.do_withdraw(amount)
        finally:
            atm.state = Authenticated()

    def balance(self, atm):
        return atm.bank.balance(atm.card.account)

    def eject(self, atm):
        return atm.release_card()


class InTransaction(State):
    """Busy dispensing: every action is refused until it finishes."""


class ATM:
    def __init__(self, bank: BankService, dispenser: CashDispenser):
        self.bank, self.dispenser = bank, dispenser
        self.state: State = Idle()
        self.card: Card | None = None
        self.retained: list[Card] = []

    def insert_card(self, card): return self.state.insert_card(self, card)
    def enter_pin(self, pin): return self.state.enter_pin(self, pin)
    def withdraw(self, amount): return self.state.withdraw(self, amount)
    def balance(self): return self.state.balance(self)
    def eject(self): return self.state.eject(self)

    def release_card(self) -> Card:
        card, self.card, self.state = self.card, None, Idle()
        return card

    def do_withdraw(self, amount: Decimal, attempts: int = 3) -> dict[int, int]:
        if amount <= 0 or amount % 1:
            raise InvalidAction(f"bad amount {amount}")
        notes = self.dispenser.plan(int(amount))  # can we pay before we charge?
        txn_id = uuid.uuid4().hex                 # one id for every retry
        for i in range(attempts):
            try:
                self.bank.debit(self.card.account, amount, txn_id)
                break
            except TimeoutError:
                if i == attempts - 1:
                    self.bank.reverse(txn_id)  # outcome unknown: undo it
                    raise
        try:
            self.dispenser.dispense(notes)
        except OSError:
            self.bank.reverse(txn_id)
            raise
        return notes


if __name__ == "__main__":
    def raises(exc, fn, *args):
        try:
            fn(*args)
        except exc:
            return True
        raise AssertionError(f"{fn.__name__}{args} did not raise {exc.__name__}")

    bank = InMemoryBank()
    card = Card("4111-0001", "ACC-1")
    bank.open(card, "1234", Decimal("1000"))
    atm = ATM(bank, CashDispenser({500: 2, 200: 5, 100: 5}))
    assert raises(InvalidAction, atm.withdraw, Decimal("100"))  # no card yet
    atm.insert_card(card)
    assert atm.enter_pin("0000") is PinResult.WRONG
    assert atm.enter_pin("1234") is PinResult.OK
    assert atm.withdraw(Decimal("700")) == {500: 1, 200: 1}
    assert atm.balance() == Decimal("300")
    for amount, exc in [("400", InsufficientFunds), ("150", CannotDispense),
                        ("-100", InvalidAction), ("100.50", InvalidAction)]:
        assert raises(exc, atm.withdraw, Decimal(amount))
    assert atm.balance() == Decimal("300") and atm.dispenser.plan(600) == {500: 1, 100: 1}
    assert atm.eject() == card and isinstance(atm.state, Idle)

    # Lockout lives in the bank, so another ATM can't reset it.
    thief = Card("4111-0002", "ACC-2")
    bank.open(thief, "9999", Decimal("50"))
    atm.insert_card(thief)
    assert [atm.enter_pin("0000") for _ in range(3)] == [PinResult.WRONG] * 2 + [PinResult.BLOCKED]
    assert thief in atm.retained and isinstance(atm.state, Idle)
    other = ATM(bank, CashDispenser({100: 1}))
    other.insert_card(thief)
    assert other.enter_pin("9999") is PinResult.BLOCKED

    class FlakyBank(InMemoryBank):  # applies the first debit, loses the reply
        dropped = False

        def debit(self, account, amount, txn_id):
            super().debit(account, amount, txn_id)
            if not self.dropped:
                self.dropped = True
                raise TimeoutError

    flaky = FlakyBank()
    flaky.open(card, "1234", Decimal("1000"))
    atm2 = ATM(flaky, CashDispenser({100: 10}))
    atm2.insert_card(card)
    atm2.enter_pin("1234")
    assert atm2.withdraw(Decimal("300")) == {100: 3}
    assert flaky.balance("ACC-1") == Decimal("700")  # charged once

    class JammedDispenser(CashDispenser):
        def dispense(self, notes):
            raise OSError("cassette jam")

    atm3 = ATM(bank, JammedDispenser({100: 10}))
    atm3.insert_card(card)
    atm3.enter_pin("1234")
    assert raises(OSError, atm3.withdraw, Decimal("100"))
    assert bank.balance("ACC-1") == Decimal("300") and isinstance(atm3.state, Authenticated)

    # Stress: 30 debits (15 txn ids, each sent twice) race on a 1000 balance.
    for _ in range(20):
        bank = InMemoryBank()
        bank.open(Card("j", "JOINT"), "1", Decimal("1000"))

        def hit(i):
            try:
                bank.debit("JOINT", Decimal("100"), f"t{i % 15}")
            except InsufficientFunds:
                pass

        with ThreadPoolExecutor(16) as pool:
            list(pool.map(hit, range(30)))
        assert bank.balance("JOINT") == Decimal("0") and len(bank.debits) == 10
    print("atm ok")
```

## Key flows

```text
User   ATM (Authenticated)       CashDispenser         BankService
 | withdraw(700) |                     |                     |
 |-------------->| state = InTransaction                     |
 |               | plan(700) --------->| 500 -> 200 -> 100   |
 |               |<-- {500: 1, 200: 1} |                     |
 |               | txn_id = uuid4()                          |
 |               | debit(acct, 700, txn_id) ---------------->| applied once
 |               |   timeout: retry, same txn_id ----------->| no-op
 |               | dispense(notes) --->|                     |
 |               |   jam: reverse(txn_id) ------------------>| refund
 |               | state = Authenticated                     |
 |<--- notes ----|                     |                     |

User   ATM (CardInserted)                      BankService
 | enter_pin("0000") x3 |                           |
 |--------------------->| verify_pin -------------->| failures += 1
 |                      |<------ WRONG, WRONG, BLOCKED
 |                      | retain card, state = Idle |
```

## Edge cases

- Zero, negative, fractional, or not a multiple of the smallest note: refused before the bank is called.
- Greedy chain misses a valid combination: 600 from one 500 and three 200s fails, because the 500 is taken first. Fall back to a small search over note counts when the chain fails.
- Insufficient funds: the bank refuses; `plan` never touched the cassette counts.
- Debit reply lost: retried with the same `txn_id` and applied once (the `FlakyBank` case).
- Cassette jams after the debit: `reverse(txn_id)`, state returns to `Authenticated`.
- Customer walks away mid-session: a session timer calls `eject`, and retains the card if it isn't taken.

## Production follow-ups

**Where is the concurrency risk?** One ATM serves one customer, so its state needs no lock. The shared resource is the account. The bank does an atomic check-and-debit, `UPDATE account SET balance = balance - :amt WHERE id = :id AND balance >= :amt`, in the same transaction as `INSERT INTO debit (txn_id PRIMARY KEY, ...)`. The demo races 30 debits and checks the balance never goes negative.

**What happens if the ATM reboots mid-withdrawal?** It writes a journal entry before the debit, after the debit and after dispensing. On boot, any transaction with a debit but no dispense record gets reversed, and cassette counts are reconciled with the hardware sensors.

**How are retries made safe?** One `txn_id` per withdrawal, reused on every retry. The bank's primary key on `txn_id` turns a duplicate into a no-op. Reversals are keyed the same way and sent store-and-forward: queued on disk and retried until the bank acknowledges, because a lost reversal means a customer charged for cash they never got.

**How do you add deposits or a mini-statement?** Add the action to the `State` base (denied by default) and implement it in `Authenticated`. A new note is one more `NoteSlot`; a different bank network is another `BankService`. Existing states don't change.

**What breaks at 10× or across machines?** The ATM is an edge device; the scaling problem is the bank's authorization service. Keep it stateless, partition accounts by id, and rate-limit per card. A hot corporate account means row-lock contention, which is acceptable at ATM transaction rates.

**What do you log and measure?** Cash left per denomination (refill before it runs out), jams, reversal count and the age of unacknowledged reversals, PIN-block rate (a fraud signal), bank latency and timeouts. Never log PINs, and mask card numbers to the last four digits.

**How is the PIN protected?** The keypad encrypts it into a PIN block, and the bank verifies it inside an HSM; the SHA-256 here is a stand-in. The attempt counter lives in the bank, so an attacker can't reset it by walking to the next machine.

**How do you keep money exact?** `Decimal` end to end; the machine pays whole notes of one currency and refuses fractions.

## Interview follow-ups

- **Why plan the notes before debiting?** Reversals are slow and show up on statements. Checking first avoids charging for cash the machine can't give.
- **Why State classes over boolean flags?** Flags allow impossible combinations ("authenticated with no card"). Each state class lists exactly what it allows.
- **Is the greedy chain optimal?** Only for canonical, unlimited note sets. With limited counts, fall back to a bounded search; four denominations and amounts in the low thousands make it cheap.
- **Compare with the vending machine?** Same State pattern, different trust boundary. See [Design a vending machine]({{ '/lld/vending-machine/' | relative_url }}).
