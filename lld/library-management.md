---
title: Design a library management system
section: lld
order: 38
phase: "Phase 5 · Intermediate"
summary: Library with books vs physical copies, borrow, return and FIFO reservations, pluggable fine policies, search, and Observer notifications.
---

## Requirements

Clarifying questions to ask first:

- Do we track titles and physical copies separately? (Yes: one title, many barcoded copies.)
- Loan limit and period? (3 books, 14 days.)
- Reservations per title or per copy? Order? How long is a hold kept? (Per title, FIFO, 3 days.)
- How are fines computed, and do unpaid fines block borrowing? (Per late day with a cap; yes.)
- How is a member told a reserved book is back? (A notification; the channel is pluggable.)

**Functional**

- Catalog of books and copies; search by title, author or ISBN.
- Members borrow a copy of a title and return it by barcode.
- Reserve a title when no copy is on the shelf; reservations are first come, first served.
- On return, if someone is waiting, hold the copy for them and notify them. An unclaimed hold passes to the next person after 3 days.
- Late returns are fined through a pluggable policy; unpaid fines block new loans.

**Non-functional**

- Two desks or kiosks can never lend the same copy twice.
- A slow or failing notification channel never blocks or fails a return.
- Money is `Decimal`.

**Out of scope:** payments, login, renewals, inter-branch transfers, e-books.

## Entities and relationships

```text
+----------------------------------+ uses +-------------------------+
| Library                          |----->| FinePolicy (ABC)        |
| books: {isbn: Book}              |      | fine(loan, at) -> Dec   |
| copies: {isbn: [BookCopy]}       |      +-------------------------+
| loans: {barcode: Loan}           |        ^ is-a: PerDayFine, NoFine
| waitlist: {isbn: deque[Member]}  |
| search(text) -> [Book]           | notifies +----------------------+
| borrow(member_id, isbn) -> Loan  |--------->| Notifier (Protocol)  |
| reserve(member_id, isbn) -> pos  |   0..*   | notify(member, msg)  |
| return_copy(barcode) -> Loan     |          +----------------------+
| expire_holds()                   |            ^ is-a: Inbox, email,
+----------------------------------+            |       SMS, push
   | has 0..*        | has 0..*       | has 0..*
   v                 v                v
+-------------+  +----------------+  +----------------------+
| Book        |  | BookCopy       |  | Loan                 |
| isbn, title |<-| barcode, book  |<-| copy, member         |
| author      |  | status         |  | borrowed_at, due_at  |
+-------------+  | held_for       |  | returned_at, fine    |
                 | hold_until     |  +----------------------+
                 +----------------+         | for 1
                                            v
                                     +----------------------+
                                     | Member               |
                                     | id, max_loans,       |
                                     | active_loans, fines  |
                                     +----------------------+

BookCopy.status: AVAILABLE --borrow--> ON_LOAN --return--> AVAILABLE
                 ON_LOAN --return, someone waiting--> ON_HOLD
                 ON_HOLD --borrow by holder--> ON_LOAN
                 ON_HOLD --expired--> ON_HOLD (next) or AVAILABLE
```

## Patterns and why

| Pattern | Where | Why |
|---|---|---|
| Strategy | `FinePolicy` | Per-day with a cap, flat fee, no fine for staff; return logic never changes |
| Observer | `Notifier` subscribers | The library doesn't know about email or SMS; a new channel just subscribes |
| Title vs item split | `Book` vs `BookCopy` | Search and reservations are per title; status and loans are per physical copy |
| FIFO queue | `waitlist` deque per ISBN | Fair order, `O(1)` to take the next member |

## Code

```python
import logging
import math
import threading
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Callable, Protocol


class CopyStatus(Enum):
    AVAILABLE = "available"
    ON_LOAN = "on loan"
    ON_HOLD = "on hold"  # kept for the member at the head of the waitlist


class LibraryError(Exception):
    pass


@dataclass(frozen=True)
class Book:  # the title
    isbn: str
    title: str
    author: str


@dataclass(eq=False)
class Member:
    id: str
    max_loans: int = 3
    active_loans: int = 0
    fines: Decimal = Decimal("0")


@dataclass(eq=False)
class BookCopy:  # the physical item
    barcode: str
    book: Book
    status: CopyStatus = CopyStatus.AVAILABLE
    held_for: Member | None = None
    hold_until: datetime | None = None


@dataclass
class Loan:
    copy: BookCopy
    member: Member
    borrowed_at: datetime
    due_at: datetime
    returned_at: datetime | None = None
    fine: Decimal = Decimal("0")


class FinePolicy(ABC):
    @abstractmethod
    def fine(self, loan: Loan, returned_at: datetime) -> Decimal: ...


class PerDayFine(FinePolicy):
    def __init__(self, per_day: Decimal, cap: Decimal):
        self.per_day, self.cap = per_day, cap

    def fine(self, loan, returned_at):
        late_days = math.ceil((returned_at - loan.due_at).total_seconds() / 86400)
        return min(self.per_day * max(0, late_days), self.cap)


class NoFine(FinePolicy):
    def fine(self, loan, returned_at):
        return Decimal("0")


class Notifier(Protocol):
    def notify(self, member: Member, message: str) -> None: ...


class Inbox:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def notify(self, member, message):
        self.sent.append((member.id, message))


class Library:
    LOAN_DAYS, HOLD_DAYS = 14, 3

    def __init__(self, fines: FinePolicy, clock: Callable[[], datetime] = datetime.now):
        self.fines, self.clock = fines, clock
        self.books: dict[str, Book] = {}
        self.copies: dict[str, list[BookCopy]] = defaultdict(list)
        self.members: dict[str, Member] = {}
        self.loans: dict[str, Loan] = {}  # barcode -> open loan
        self.waitlist: dict[str, deque[Member]] = defaultdict(deque)
        self.subscribers: list[Notifier] = []
        # Simplification: one lock for the library; row locks per copy in a database
        self._lock = threading.Lock()

    def add_book(self, book: Book, barcodes: list[str]) -> None:
        self.books[book.isbn] = book
        self.copies[book.isbn] += [BookCopy(b, book) for b in barcodes]

    def add_member(self, m: Member) -> None:
        self.members[m.id] = m

    def subscribe(self, n: Notifier) -> None:
        self.subscribers.append(n)

    def search(self, text: str) -> list[Book]:
        # Simplification: linear scan; PostgreSQL full-text index past a few thousand titles
        q = text.lower()
        return [b for b in self.books.values()
                if q in b.title.lower() or q in b.author.lower() or q == b.isbn]

    def borrow(self, member_id: str, isbn: str) -> Loan:
        with self._lock:
            m, copies = self.members[member_id], self.copies[isbn]
            if m.fines > 0:
                raise LibraryError("pay outstanding fines first")
            if m.active_loans >= m.max_loans:
                raise LibraryError("loan limit reached")
            copy = (next((c for c in copies if c.held_for is m), None)
                    or next((c for c in copies if c.status is CopyStatus.AVAILABLE), None))
            if copy is None:
                raise LibraryError("no copy on the shelf; reserve it")
            now = self.clock()
            copy.status, copy.held_for, copy.hold_until = CopyStatus.ON_LOAN, None, None
            m.active_loans += 1
            loan = self.loans[copy.barcode] = Loan(copy, m, now, now + timedelta(days=self.LOAN_DAYS))
            return loan

    def reserve(self, member_id: str, isbn: str) -> int:
        with self._lock:
            m, copies, queue = self.members[member_id], self.copies[isbn], self.waitlist[isbn]
            if any(c.status is CopyStatus.AVAILABLE for c in copies):
                raise LibraryError("a copy is on the shelf; borrow it")
            if m in queue or any(c.held_for is m for c in copies):
                raise LibraryError("already reserved")
            queue.append(m)
            return len(queue)

    def return_copy(self, barcode: str) -> Loan:
        with self._lock:
            loan = self.loans.pop(barcode, None)
            if loan is None:
                raise LibraryError(f"{barcode} is not on loan")
            loan.returned_at = self.clock()
            loan.fine = self.fines.fine(loan, loan.returned_at)
            loan.member.fines += loan.fine
            loan.member.active_loans -= 1
            events = self._next_holder(loan.copy)
        self._publish(events)  # outside the lock: channels can be slow
        return loan

    def pay(self, member_id: str, amount: Decimal) -> None:
        with self._lock:
            self.members[member_id].fines -= amount

    def expire_holds(self) -> None:
        """Run by a scheduler: pass unclaimed holds to the next member."""
        events = []
        with self._lock:
            now = self.clock()
            for copies in self.copies.values():
                for c in copies:
                    if c.status is CopyStatus.ON_HOLD and c.hold_until < now:
                        events += self._next_holder(c)
        self._publish(events)

    def _next_holder(self, copy: BookCopy) -> list[tuple[Member, str]]:
        queue = self.waitlist[copy.book.isbn]
        if not queue:
            copy.status, copy.held_for, copy.hold_until = CopyStatus.AVAILABLE, None, None
            return []
        m = queue.popleft()
        copy.status, copy.held_for = CopyStatus.ON_HOLD, m
        copy.hold_until = self.clock() + timedelta(days=self.HOLD_DAYS)
        return [(m, f"'{copy.book.title}' is held for you until {copy.hold_until:%d %b}")]

    def _publish(self, events: list[tuple[Member, str]]) -> None:
        for member, message in events:
            for s in self.subscribers:
                try:
                    s.notify(member, message)
                except Exception:  # one broken channel must not stop the others
                    logging.exception("notify failed for %s", member.id)


if __name__ == "__main__":
    def raises(fn, *args):
        try:
            fn(*args)
        except LibraryError:
            return True
        raise AssertionError(f"{fn.__name__}{args} should fail")

    now = [datetime(2026, 3, 1, 10, 0)]
    lib = Library(PerDayFine(Decimal("0.50"), cap=Decimal("10.00")), clock=lambda: now[0])
    inbox = Inbox()
    lib.subscribe(inbox)
    ddia = Book("978-1449373320", "Designing Data-Intensive Applications", "Martin Kleppmann")
    lib.add_book(ddia, ["D1"])
    lib.add_book(Book("978-0134685991", "Effective Java", "Joshua Bloch"), ["J1"])
    for mid in ["ann", "bob", "cat"]:
        lib.add_member(Member(mid))

    assert lib.search("kleppmann") == [ddia] and lib.search("978-0134685991")
    loan = lib.borrow("ann", ddia.isbn)
    assert loan.due_at == now[0] + timedelta(days=14)
    assert raises(lib.borrow, "bob", ddia.isbn)  # no copy on the shelf
    assert lib.reserve("bob", ddia.isbn) == 1 and lib.reserve("cat", ddia.isbn) == 2
    assert raises(lib.reserve, "bob", ddia.isbn)  # duplicate

    now[0] += timedelta(days=17)  # 3 days late
    assert lib.return_copy("D1").fine == Decimal("1.50")
    assert raises(lib.return_copy, "D1")  # double scan
    assert inbox.sent[-1][0] == "bob"
    assert raises(lib.borrow, "cat", ddia.isbn)  # held for bob
    assert raises(lib.borrow, "ann", "978-0134685991")  # unpaid fine
    lib.pay("ann", Decimal("1.50"))
    lib.borrow("ann", "978-0134685991")

    now[0] += timedelta(days=4)  # bob never comes; the hold passes to cat
    lib.expire_holds()
    assert inbox.sent[-1][0] == "cat"
    assert lib.borrow("cat", ddia.isbn).copy.barcode == "D1"
    late = Loan(BookCopy("X", ddia), Member("z"), now[0], now[0])
    assert lib.fines.fine(late, now[0] + timedelta(days=100)) == Decimal("10.00")

    # Stress: 20 members race for 3 copies, 20 rounds.
    lib.add_book(Book("111", "Stress Test", "Anon"), ["S1", "S2", "S3"])
    for i in range(20):
        lib.add_member(Member(f"m{i}"))

    def grab(i):
        try:
            return lib.borrow(f"m{i}", "111").copy.barcode
        except LibraryError:
            return None

    for _ in range(20):
        with ThreadPoolExecutor(8) as pool:
            got = [b for b in pool.map(grab, range(20)) if b]
        assert sorted(got) == ["S1", "S2", "S3"]
        for barcode in got:
            lib.return_copy(barcode)
    assert all(m.active_loans == 0 for m in lib.members.values() if m.id.startswith("m"))
    print("library ok")
```

## Key flows

```text
Desk     Library                FinePolicy     waitlist      Notifier
 | return_copy("D1")               |               |             |
 |------->| lock                   |               |             |
 |        | pop open loan          |               |             |
 |        | fine(loan, now) ------>|               |             |
 |        |<------ 1.50 -----------|               |             |
 |        | member.fines += 1.50   |               |             |
 |        | next waiting member? ----------------->|             |
 |        |<-------------------- bob --------------|             |
 |        | copy ON_HOLD for bob until now + 3 days              |
 |        | unlock                                               |
 |        | notify(bob, "held for you") ------------------------>|
 |<- loan-|                                                      |

Member   Library
 | borrow("cat", isbn)
 |------->| lock; fines? loan limit?
 |        | copy held for cat, else any AVAILABLE copy, else error
 |        | status = ON_LOAN, due = now + 14 days, unlock
 |<- loan-|
```

## Edge cases

- Reserve while a copy is on the shelf: refused; borrow it instead.
- Duplicate reservation, or reserving a title already held for you: refused.
- Hold not collected in 3 days: `expire_holds` passes the copy to the next member and notifies them, or puts it back on the shelf.
- Returned a few hours late: counts as one day (ceiling). Very late: capped.
- Same barcode scanned twice at the desk: the second return fails instead of fining twice.
- A notifier raises: logged, and the other channels and the return still go through.
- Member at the loan limit or with unpaid fines: borrow refused before any copy is touched.

## Production follow-ups

**How do you stop two kiosks lending the same copy?** In memory, one lock. In PostgreSQL, claim a copy atomically: `UPDATE book_copy SET status = 'ON_LOAN' WHERE barcode = (SELECT barcode FROM book_copy WHERE isbn = :isbn AND status = 'AVAILABLE' LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING barcode`. Lock the member row first (`FOR UPDATE`) to enforce the loan limit, and always take member then copy so two transactions can't deadlock. See [Deadlock, livelock and starvation]({{ '/lld/deadlock-livelock-starvation/' | relative_url }}).

**What survives a restart?** Everything is rows: `book`, `book_copy`, `member`, `loan`, and `reservation (isbn, member_id, created_at, status)`. The waitlist is reservations ordered by `created_at`. Hold expiry is a scheduled job (cron or Celery beat) calling `expire_holds`, which is safe to run twice.

**How do retries stay safe?** Return is `UPDATE loan SET returned_at = now() WHERE copy = :barcode AND returned_at IS NULL`; zero rows means already returned, so the desk shows the existing loan and no second fine. A partial unique index on active `(isbn, member_id)` reservations blocks duplicates. Notifications go through an outbox table written in the same transaction as the hold, and a worker delivers them at least once with the hold id as a dedupe key.

**How do you add a fine rule or a channel without editing old code?** A new `FinePolicy` class; map member type to policy so students and faculty differ. A new channel is a new `Notifier` that subscribes. DVDs or e-books turn `Book` into a catalog item with a type that sets the loan period.

**What breaks at 10× or across machines?** One library is small. A university or city network has per-branch inventory, catalog search in PostgreSQL full-text or a search engine, and holds that may trigger a transfer between branches. Reads (search) dominate, so cache them; writes are a few per minute per branch.

**What do you log and measure?** Loans per day, overdue count, outstanding fines, hold pickup rate and expired holds, waitlist length per title (a signal to buy more copies), notification failures, search latency and zero-result queries.

**How do you keep fines exact?** `Decimal` everywhere, capped per loan, and stored on the loan when it closes so a later policy change doesn't rewrite history.

## Interview follow-ups

- **Why separate `Book` and `BookCopy`?** Title data (search, reservations) and physical state (status, shelf, condition) change for different reasons. Merging them duplicates titles and makes "any copy free?" awkward.
- **Renewals?** Allowed when nobody is waiting for the title and under a renewal limit; extend `due_at`.
- **Why notify outside the lock?** Email and SMS calls take hundreds of milliseconds and can fail. Holding the lock would stall every desk, so events are collected under the lock and published after.
- **Better search?** A `tsvector` column weighted title over author, plus a trigram index for typos.
