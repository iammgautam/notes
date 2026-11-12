---
title: Design a stock trading engine
section: lld
order: 43
phase: "Phase 5 · Advanced"
summary: Limit and market orders, a per-symbol order book with price-time priority, partial fills, cancels and trade events, with a tested matching loop.
---

The matching engine is the core of an exchange: it takes orders, keeps an order book per symbol, and turns crossing orders into trades. Interviewers care about one thing above all: the matching loop must be correct.

## Requirements

Clarifying questions:
- Which order types? Limit and market to start; stop, IOC, FOK later?
- Price representation? Integer ticks (cents), never floats.
- Partial fills allowed? Yes. At what price does a trade execute? The resting (maker) order's price.
- What happens to an unfilled market order? Cancel the remainder (immediate-or-cancel).
- One process, many symbols? Yes, books are independent.

Functional:
- Submit limit and market buy/sell orders for a symbol.
- Match by **price-time priority**: best price first, then earliest arrival at that price.
- Partial fills on both sides; a limit remainder rests on the book.
- Cancel a resting order.
- Emit every trade to registered listeners; query best bid and ask.

Non-functional:
- Correct: no overfill, the book is never left crossed (best bid < best ask).
- Fast: `O(log P)` to add a new price level, `O(1)` to cancel.
- Deterministic: the same input sequence produces the same trades.
- Thread-safe per symbol.

Out of scope: stop orders, opening auctions, risk checks, market-data depth feeds.

## Entities and relationships

```text
+--------------------+ 1     * +-----------------------+
| MatchingEngine     |-------->| OrderBook             |
| books {sym: book}  |         | symbol, lock          |
| submit(order)      |         | bids, asks: BookSide  |
| cancel(sym, id)    |         | listeners: [fn(Trade)]|
+--------------------+         | submit(), cancel()    |
                               +-----+------------+----+
                                     | 2          | emits
                               +-----v--------+ +-v-----------+
                               | BookSide     | | Trade       |
                               | heap [price] | | buy_id      |
                               | levels       | | sell_id     |
                               | {price: deq} | | price, qty  |
                               | best()       | +-------------+
                               +-----+--------+
                                     | * (FIFO per price)
                               +-----v----------------+
                               | Order                |
                               | id, side, qty, price |
                               | filled, status       |
                               | fill(qty)            |
                               +----------------------+
```

`Order.price is None` means market. `Status`: NEW → PARTIAL → FILLED, or → CANCELLED.

## Patterns and why

| Pattern | Where | Why |
|---|---|---|
| Observer | `OrderBook.listeners` | Clearing, market data and audit react to trades without the matcher knowing them |
| Command | `Order` objects | Orders are data you can journal and replay to rebuild the book |
| Facade | `MatchingEngine` | One entry point that routes to the right per-symbol book |
| Lazy deletion | `cancel()` + `BookSide.best()` | `O(1)` cancel without searching a deque |

## Code

```python
from __future__ import annotations

import heapq
import itertools
import threading
from collections import Counter, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class Side(Enum):
    BUY = "buy"
    SELL = "sell"


class Status(Enum):
    NEW = "new"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"


_ids = itertools.count(1)  # next() is one C call: atomic under the GIL


@dataclass(eq=False)
class Order:
    symbol: str
    side: Side
    qty: int
    price: int | None = None  # ticks (cents); None = market order
    id: int = field(default_factory=lambda: next(_ids))
    filled: int = 0
    status: Status = Status.NEW

    @property
    def remaining(self) -> int:
        return self.qty - self.filled

    @property
    def is_open(self) -> bool:
        return self.status in (Status.NEW, Status.PARTIAL)

    def fill(self, qty: int) -> None:
        self.filled += qty
        self.status = Status.FILLED if self.remaining == 0 else Status.PARTIAL


@dataclass(frozen=True)
class Trade:
    symbol: str
    buy_id: int
    sell_id: int
    price: int
    qty: int


class BookSide:
    """Heap finds the best price; a deque per price keeps arrival order."""

    def __init__(self, side: Side):
        self.sign = -1 if side is Side.BUY else 1  # bids: max-heap via negation
        self.heap: list[int] = []
        self.levels: dict[int, deque[Order]] = {}  # price in heap <=> in levels

    def add(self, order: Order) -> None:
        if order.price not in self.levels:
            self.levels[order.price] = deque()
            heapq.heappush(self.heap, self.sign * order.price)
        self.levels[order.price].append(order)

    def best(self) -> tuple[int, deque[Order]] | None:
        while self.heap:
            price = self.sign * self.heap[0]
            level = self.levels[price]
            while level and not level[0].is_open:  # drop filled/cancelled
                level.popleft()
            if level:
                return price, level
            heapq.heappop(self.heap)
            del self.levels[price]
        return None


class OrderBook:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.bids, self.asks = BookSide(Side.BUY), BookSide(Side.SELL)
        self.orders: dict[int, Order] = {}
        self.listeners: list[Callable[[Trade], None]] = []
        self._lock = threading.Lock()

    def submit(self, order: Order) -> list[Trade]:
        if order.qty <= 0 or (order.price is not None and order.price <= 0):
            raise ValueError("qty and price must be positive")
        with self._lock:
            trades = self._match(order)
            if order.is_open:
                if order.price is None:
                    order.status = Status.CANCELLED  # market: drop remainder
                else:
                    own = self.bids if order.side is Side.BUY else self.asks
                    own.add(order)
                    self.orders[order.id] = order
            # Simplification: listeners run under the lock to keep trade order;
            # hand off to a queue if they get slow
            for trade in trades:
                for listener in self.listeners:
                    listener(trade)
            return trades

    def _match(self, taker: Order) -> list[Trade]:
        book = self.asks if taker.side is Side.BUY else self.bids
        trades = []
        while taker.remaining and (best := book.best()):
            price, level = best
            if taker.price is not None and (
                price > taker.price if taker.side is Side.BUY
                else price < taker.price
            ):
                break  # best opposite price doesn't cross the limit
            maker = level[0]
            qty = min(taker.remaining, maker.remaining)
            taker.fill(qty)
            maker.fill(qty)  # a filled maker is dropped by best()
            if not maker.is_open:
                del self.orders[maker.id]
            buy, sell = (taker, maker) if taker.side is Side.BUY else (maker, taker)
            trades.append(Trade(self.symbol, buy.id, sell.id, price, qty))
        return trades

    def cancel(self, order_id: int) -> bool:
        with self._lock:
            order = self.orders.pop(order_id, None)
            if order is None or not order.is_open:
                return False
            order.status = Status.CANCELLED  # lazy: best() skips it later
            return True

    def top(self) -> tuple[int | None, int | None]:
        with self._lock:
            bid, ask = self.bids.best(), self.asks.best()
            return (bid[0] if bid else None, ask[0] if ask else None)


class MatchingEngine:
    def __init__(self, symbols: list[str]):
        self.books = {s: OrderBook(s) for s in symbols}

    def submit(self, order: Order) -> list[Trade]:
        if order.symbol not in self.books:
            raise ValueError(f"unknown symbol {order.symbol}")
        return self.books[order.symbol].submit(order)

    def cancel(self, symbol: str, order_id: int) -> bool:
        return self.books[symbol].cancel(order_id)


def test_price_time_priority_and_partial_fills() -> None:
    eng = MatchingEngine(["ACME"])
    s1 = Order("ACME", Side.SELL, 100, 101)
    s2 = Order("ACME", Side.SELL, 100, 101)  # same price, later: behind s1
    s3 = Order("ACME", Side.SELL, 100, 100)  # better price: goes first
    for o in (s1, s2, s3):
        assert eng.submit(o) == []
    buy = Order("ACME", Side.BUY, 250, 101)
    trades = eng.submit(buy)
    assert [(t.sell_id, t.price, t.qty) for t in trades] == [
        (s3.id, 100, 100), (s1.id, 101, 100), (s2.id, 101, 50)]
    assert buy.status is Status.FILLED
    assert s2.status is Status.PARTIAL and s2.remaining == 50
    assert eng.books["ACME"].top() == (None, 101)


def test_resting_market_and_cancel() -> None:
    eng = MatchingEngine(["ACME"])
    b1, b2 = Order("ACME", Side.BUY, 10, 99), Order("ACME", Side.BUY, 10, 98)
    for o in (b1, b2):
        eng.submit(o)
    assert eng.submit(Order("ACME", Side.SELL, 5, 100)) == []  # no cross
    assert eng.books["ACME"].top() == (99, 100)
    assert eng.cancel("ACME", b1.id) and not eng.cancel("ACME", b1.id)
    mkt = Order("ACME", Side.SELL, 15)
    trades = eng.submit(mkt)
    assert [(t.buy_id, t.price, t.qty) for t in trades] == [(b2.id, 98, 10)]
    assert mkt.filled == 10 and mkt.status is Status.CANCELLED
    assert eng.books["ACME"].top() == (None, 100)


def test_concurrent_submits() -> None:
    import random
    book, trades, sent = OrderBook("ACME"), [], []
    book.listeners.append(trades.append)

    def trader(seed: int) -> None:
        rnd = random.Random(seed)
        for _ in range(500):
            price = rnd.choice([None] + list(range(95, 106)))
            o = Order("ACME", rnd.choice(list(Side)), rnd.randint(1, 20), price)
            sent.append(o)
            book.submit(o)

    threads = [threading.Thread(target=trader, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    traded = Counter()
    for t in trades:
        traded[t.buy_id] += t.qty
        traded[t.sell_id] += t.qty
    assert all(o.filled == traded[o.id] <= o.qty for o in sent)  # no overfill
    bid, ask = book.top()
    assert bid is None or ask is None or bid < ask  # never left crossed


if __name__ == "__main__":
    test_price_time_priority_and_partial_fills()
    test_resting_market_and_cancel()
    test_concurrent_submits()
    print("ok")
```

Complexity: submit is `O(log P + k)` for `P` price levels and `k` fills; cancel is `O(1)`; `best()` is amortized `O(1)` because each dead order or empty level is removed once.

## Key flows

Buy 250 @ 101 against asks 100×100 (s3), 101×100 (s1), 101×100 (s2):

```text
Client         OrderBook                asks BookSide        Listeners
  | submit(buy)  |                        |                     |
  |------------->| lock                   |                     |
  |              |-------- best() ------->| 100: [s3]           |
  |              | fill 100 @ 100         |                     |
  |              |-------- best() ------->| s3 filled: pop      |
  |              |                        | 101: [s1, s2]       |
  |              | fill 100 @ 101 (s1)    |                     |
  |              | fill  50 @ 101 (s2)    | s2 PARTIAL          |
  |              | buy FILLED             |                     |
  |              |--------------- emit 3 trades --------------->|
  |<-------------| unlock, return trades  |                     |
```

Cancel: `orders.pop(id)` → mark CANCELLED → the next `best()` that reaches it pops it off its level.

## Edge cases

- Market order on an empty book: zero fills, status CANCELLED.
- Limit order priced through several levels: fills each level at the maker's price (price improvement for the taker).
- Cancel of a filled, cancelled or unknown order: returns `False`, no error.
- Same-price orders: FIFO by deque position, no timestamps needed, so clock skew can't reorder them.
- Zero or negative qty/price: rejected before taking the lock.
- A level holding only cancelled orders: stays in memory until it reaches the top, then is purged.

## Production follow-ups

**Thread safety / concurrency?** One lock per book, so different symbols match in parallel and one symbol is strictly serialized, which is what price-time priority requires. Real engines go further: a single thread owns each book and reads orders from a queue (the LMAX Disruptor design), so the hot path has no locks at all. In Python the GIL means threads won't run matching in parallel anyway; shard symbols across processes. See [Producer-consumer]({{ '/lld/producer-consumer/' | relative_url }}).

**Persistence and restart?** Write every accepted order and cancel to a sequenced, append-only journal (fsync or replicated) before matching. Because matching is deterministic, replaying the journal rebuilds the exact book and trade stream. Take periodic snapshots of the book so replay only covers the tail.

**Idempotency and retries?** Clients send a client order id per session; a duplicate submit returns the original order instead of creating a second one. Cancel is already idempotent (second call returns `False`). Downstream consumers dedupe trades by trade sequence number.

**Extensibility?** Add time-in-force as an enum handled at the "remainder" step: IOC cancels the rest, FOK pre-checks available depth before matching, post-only rejects if it would cross. Stop orders live in a separate trigger book keyed by stop price and are submitted as market/limit orders when the last trade price crosses. `_match` stays untouched.

**Scale (10×, across machines)?** Partition by symbol: one matcher process per symbol group, a gateway routing orders by symbol, and trades published to a log (Kafka-style) for clearing and market data. A single very hot symbol can't be split, so optimize that path (native code, preallocated objects, kernel bypass networking). Python is fine for the interview; real venues use C++, Rust or Java.

**Observability?** Histogram of submit-to-ack latency (p50/p99/p999), orders and trades per second, book depth and spread per symbol, reject counts by reason. Every event carries a sequence number in the audit log. Run invariant checks (book not crossed, `filled <= qty`) in a shadow consumer and alert on any violation.

**Self-trade prevention?** Tag orders with an account id; when taker and maker share it, cancel the resting order (or the incoming one, by configured policy) instead of trading.

**Money precision?** Prices are integer ticks and quantities are integers, so there is no float drift and comparisons are exact. Convert to `Decimal` only at the API edge.

## Interview follow-ups

- **Why a heap plus deques, not a sorted list?** Inserting into a sorted list is `O(P)`. A heap gives the best price in `O(1)` and a new level in `O(log P)`; the deque gives `O(1)` FIFO. A balanced tree (`SortedDict`) also works and makes depth snapshots easier.
- **How do you modify an order?** Price change or quantity increase = cancel + new order (loses time priority). Quantity decrease keeps priority: reduce `qty` in place.
- **Why trade at the maker's price?** The maker set that price first; the taker crossed it, so the taker gets any improvement.
- **Why lazy cancellation?** Removing from the middle of a deque is `O(n)`. Marking is `O(1)` and the cost is paid once when the order reaches the head.
- **How do you prove the loop is correct?** Unit tests for priority and partial fills, then a randomized concurrent test that checks the invariants: fills match trades, no overfill, book not crossed.
