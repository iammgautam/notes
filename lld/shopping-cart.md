---
title: Design an online shopping cart
section: lld
order: 37
phase: "Phase 5 · Intermediate"
summary: Cart, all-or-nothing inventory reservation, a pricing pipeline of promotions, coupons and tax in Decimal, and an order state machine from checkout to delivery.
---

## Requirements

Clarifying questions worth asking:

- Is stock reserved when an item is added to the cart, or at checkout? (At checkout. Reserving on add lets idle carts hoard stock.)
- Do prices in the cart update if the catalog price changes? (Yes; the cart stores SKUs and quantities, and prices are computed at checkout.)
- Discount order: promotions, then coupon, then tax? (Yes, and tax is on the discounted amount.)
- One coupon per order? (Yes.)
- What happens to reserved stock if the user never pays? (Released when the order is cancelled or times out.)

Functional:

- Add, update and remove cart items.
- Price the cart: line totals, automatic promotions, one coupon, tax.
- Checkout: reserve stock for every line or none, then create an order awaiting payment.
- Order lifecycle: pay, ship, deliver, cancel (with refund if paid).

Non-functional:

- Money in `Decimal`, rounded half-up to cents once per amount; total never negative.
- Never sell more units than exist, even with many concurrent checkouts.
- Payment webhooks and cancels are safe to receive twice.

Out of scope: payment gateway, shipping rates, multi-seller carts, wishlists.

## Entities and relationships

```text
+-----------------+       +-----------------------------------+
| Cart            | reads | CheckoutService                   |
| user_id         |<------| checkout(cart, coupon) -> Order   |
| items: {sku:qty}|       | mark_paid(), ship(), cancel()     |
| add/update      |       +-----------------------------------+
+-----------------+          | uses        | uses          | creates
                             v             v               v
            +-----------------+ +---------------+ +------------------+
            | Inventory       | | <<abstract>>  | | Order            |
            | available       | | PricingStep   | | items, bill      |
            | reserved, lock  | | apply(bill)   | | status, lock     |
            | reserve()       | +---------------+ | move_to(status)  |
            | release()       |   ^ is-a          +------------------+
            | commit()        |   Subtotal, CategoryDiscount,
            +-----------------+   ApplyCoupon ---uses---> Coupon
                                  Tax                     <<abstract>>
Bill: lines, coupon, subtotal,                            discount(amt)
      discounts, tax, total                               ^ is-a
                                              PercentCoupon, FlatCoupon
```

Order states:

```text
PENDING_PAYMENT --pay--> PAID --ship--> SHIPPED --deliver--> DELIVERED
      |                   |
      +------cancel-------+---> CANCELLED (release stock; refund if PAID)
```

## Patterns and why

| Pattern | Where | Why (what it prevents) |
|---|---|---|
| Pipeline (chain of steps) | `PricingStep` list run in order | One `calculate_total()` that every promotion edits; order of discounts hidden in nested ifs |
| Strategy | `Coupon` subclasses | `if coupon.type == "percent"` branches in pricing |
| State machine | `TRANSITIONS` table + `Order.move_to` | Shipping a cancelled order, refunding twice |
| Reservation | `Inventory.reserve` / `release` / `commit` | Overselling between "in stock" check and payment |

## Code

```python
from __future__ import annotations

import threading
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def money(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Product:
    sku: str
    price: Decimal
    category: str


class Cart:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self.items: dict[str, int] = {}

    def add(self, sku: str, qty: int = 1) -> None:
        self.update(sku, self.items.get(sku, 0) + qty)

    def update(self, sku: str, qty: int) -> None:
        if qty < 0:
            raise ValueError("quantity cannot be negative")
        if qty == 0:
            self.items.pop(sku, None)
        else:
            self.items[sku] = qty


class OutOfStock(Exception):
    pass


class Inventory:
    def __init__(self, stock: dict[str, int]) -> None:
        self.available = dict(stock)
        self.reserved: dict[str, int] = defaultdict(int)
        self.lock = threading.Lock()

    def reserve(self, items: dict[str, int]) -> None:
        with self.lock:                                   # all or nothing
            short = [s for s, q in items.items() if self.available.get(s, 0) < q]
            if short:
                raise OutOfStock(short)
            for sku, qty in items.items():
                self.available[sku] -= qty
                self.reserved[sku] += qty

    def release(self, items: dict[str, int]) -> None:     # order cancelled
        with self.lock:
            for sku, qty in items.items():
                self.reserved[sku] -= qty
                self.available[sku] += qty

    def commit(self, items: dict[str, int]) -> None:      # order shipped
        with self.lock:
            for sku, qty in items.items():
                self.reserved[sku] -= qty


class Coupon(ABC):
    @abstractmethod
    def discount(self, amount: Decimal) -> Decimal: ...


@dataclass(frozen=True)
class PercentCoupon(Coupon):
    pct: Decimal
    max_off: Decimal

    def discount(self, amount):
        return min(money(amount * self.pct / 100), self.max_off)


@dataclass(frozen=True)
class FlatCoupon(Coupon):
    off: Decimal
    min_order: Decimal

    def discount(self, amount):
        return self.off if amount >= self.min_order else ZERO


@dataclass
class Bill:
    lines: list[tuple[Product, int]]
    coupon: Coupon | None = None
    subtotal: Decimal = ZERO
    discounts: list[tuple[str, Decimal]] = field(default_factory=list)
    tax: Decimal = ZERO

    @property
    def after_discounts(self) -> Decimal:
        return self.subtotal - sum((d for _, d in self.discounts), ZERO)

    @property
    def total(self) -> Decimal:
        return self.after_discounts + self.tax


class PricingStep(ABC):
    @abstractmethod
    def apply(self, bill: Bill) -> None: ...


class Subtotal(PricingStep):
    def apply(self, bill):
        bill.subtotal = sum((p.price * q for p, q in bill.lines), ZERO)


class CategoryDiscount(PricingStep):
    def __init__(self, category: str, pct: Decimal) -> None:
        self.category, self.pct = category, pct

    def apply(self, bill):
        base = sum((p.price * q for p, q in bill.lines
                    if p.category == self.category), ZERO)
        if base:
            bill.discounts.append((f"{self.pct}% off {self.category}",
                                   money(base * self.pct / 100)))


class ApplyCoupon(PricingStep):
    def apply(self, bill):
        if bill.coupon:            # clamp: a coupon never makes the total negative
            off = min(bill.coupon.discount(bill.after_discounts), bill.after_discounts)
            bill.discounts.append(("coupon", off))


class Tax(PricingStep):
    def __init__(self, rate: Decimal) -> None:
        self.rate = rate

    def apply(self, bill):
        bill.tax = money(bill.after_discounts * self.rate)


class OrderStatus(Enum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


S = OrderStatus
TRANSITIONS = {
    S.PENDING_PAYMENT: {S.PAID, S.CANCELLED},
    S.PAID: {S.SHIPPED, S.CANCELLED},
    S.SHIPPED: {S.DELIVERED},
    S.DELIVERED: set(),
    S.CANCELLED: set(),
}


class InvalidTransition(Exception):
    pass


@dataclass
class Order:
    id: str
    user_id: str
    items: dict[str, int]
    bill: Bill
    status: OrderStatus = S.PENDING_PAYMENT
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def move_to(self, new: OrderStatus) -> OrderStatus | None:
        """Returns the previous status, or None if already `new` (a retry)."""
        with self._lock:
            if self.status is new:
                return None
            if new not in TRANSITIONS[self.status]:
                raise InvalidTransition(f"{self.status.name} -> {new.name}")
            old, self.status = self.status, new
            return old


class CheckoutService:
    def __init__(self, catalog: dict[str, Product], inventory: Inventory,
                 steps: list[PricingStep]) -> None:
        self.catalog, self.inventory, self.steps = catalog, inventory, steps

    def price(self, cart: Cart, coupon: Coupon | None = None) -> Bill:
        bill = Bill([(self.catalog[s], q) for s, q in cart.items.items()], coupon)
        for step in self.steps:
            step.apply(bill)
        return bill

    def checkout(self, cart: Cart, coupon: Coupon | None = None) -> Order:
        if not cart.items:
            raise ValueError("empty cart")
        bill = self.price(cart, coupon)
        items = dict(cart.items)
        self.inventory.reserve(items)                     # may raise OutOfStock
        cart.items.clear()
        return Order(uuid.uuid4().hex, cart.user_id, items, bill)

    def mark_paid(self, order: Order) -> None:            # payment webhook
        order.move_to(S.PAID)

    def ship(self, order: Order) -> None:
        if order.move_to(S.SHIPPED):
            self.inventory.commit(order.items)

    def cancel(self, order: Order) -> Decimal:
        """Returns the amount to refund now (zero on a repeated cancel)."""
        old = order.move_to(S.CANCELLED)
        if old is None:
            return ZERO
        self.inventory.release(order.items)
        return order.bill.total if old is S.PAID else ZERO


if __name__ == "__main__":
    from concurrent.futures import ThreadPoolExecutor

    D = Decimal
    catalog = {p.sku: p for p in [Product("book1", D("499.00"), "books"),
                                  Product("book2", D("350.00"), "books"),
                                  Product("pen", D("25.50"), "stationery"),
                                  Product("hot", D("999.00"), "phones")]}
    inv = Inventory({"book1": 2, "book2": 5, "pen": 100, "hot": 5})
    svc = CheckoutService(catalog, inv, [Subtotal(), CategoryDiscount("books", D(10)),
                                         ApplyCoupon(), Tax(D("0.18"))])
    cart = Cart("u1")
    for sku, qty in [("book1", 1), ("book2", 2), ("pen", 3)]:
        cart.add(sku, qty)
    bill = svc.price(cart, FlatCoupon(D("100.00"), min_order=D("1000.00")))
    assert bill.subtotal == D("1275.50")
    assert bill.discounts == [("10% off books", D("119.90")), ("coupon", D("100.00"))]
    assert (bill.tax, bill.total) == (D("190.01"), D("1245.61"))
    assert svc.price(cart, PercentCoupon(D(20), D("150"))).discounts[1][1] == D("150")
    assert svc.price(cart, FlatCoupon(D("5000"), D(0))).total == ZERO   # clamped

    order = svc.checkout(cart, FlatCoupon(D("100.00"), D("1000.00")))
    assert cart.items == {} and inv.available["book1"] == 1
    svc.mark_paid(order)
    svc.mark_paid(order)                                  # duplicate webhook
    svc.ship(order)
    assert inv.reserved["book1"] == 0 and inv.available["book1"] == 1
    try:
        svc.cancel(order)
        raise AssertionError("cancelled a shipped order")
    except InvalidTransition:
        pass

    c2 = Cart("u2")
    c2.add("book1")
    o2 = svc.checkout(c2)
    svc.mark_paid(o2)
    assert svc.cancel(o2) == o2.bill.total and svc.cancel(o2) == ZERO
    assert inv.available["book1"] == 1

    # Stress: 5 phones, 50 concurrent buyers; exactly 5 orders, no oversell.
    def buy(i: int) -> Order | None:
        c = Cart(f"buyer{i}")
        c.add("hot")
        try:
            return svc.checkout(c)
        except OutOfStock:
            return None

    with ThreadPoolExecutor(16) as pool:
        orders = [o for o in pool.map(buy, range(50)) if o]
    assert len(orders) == 5 and inv.available["hot"] == 0 and inv.reserved["hot"] == 5
    print("shopping cart ok")
```

## Key flows

```text
User      CheckoutService    PricingSteps       Inventory        Order
 | checkout(cart, coupon)       |                   |               |
 |------------->| price: Subtotal -> Promo          |               |
 |              |   -> ApplyCoupon -> Tax --------->|               |
 |              |<---------- Bill ------------------|               |
 |              | reserve(items) under lock ------->|               |
 |              |<-------- ok (or OutOfStock) ------|               |
 |              | clear cart; new Order(PENDING_PAYMENT) ---------->|
 |<-- Order ----|                                                   |
```

```text
Gateway / Ops      CheckoutService          Order              Inventory
 | webhook: paid       |                      |                    |
 |-------------------->| move_to(PAID) ------>|                    |
 | (same webhook again)| move_to(PAID): None, no-op                |
 | ship                |                      |                    |
 |-------------------->| move_to(SHIPPED) --->| commit(items) ---->|
 | cancel (too late)   | move_to(CANCELLED) -> InvalidTransition   |
```

## Edge cases

- Item goes out of stock between "add to cart" and checkout: checkout fails with the list of short SKUs; nothing is reserved.
- Price changed since the user looked: checkout prices from the catalog; show the new total before payment.
- Coupon worth more than the order: clamped, total is zero, tax is zero.
- Coupon below its minimum order after promotions: gives zero, because it sees the amount after promotions.
- Quantity zero or negative: zero removes the line, negative is rejected.
- User never pays: a sweeper cancels `PENDING_PAYMENT` orders after 15 minutes, which releases the stock.
- Payment webhook arrives after the timeout cancelled the order: `move_to(PAID)` raises; refund the charge.

## Production follow-ups

**How do you prevent overselling under concurrency?** `reserve` checks every line and decrements under one lock, so two checkouts cannot both see the last unit. The GIL does not make `if available >= q: available -= q` atomic. In PostgreSQL the same thing is `UPDATE stock SET available = available - %s WHERE sku = %s AND available >= %s` per line inside one transaction, rolling back if any update touches zero rows; lock the rows in SKU order to avoid deadlocks between carts.

**What survives a restart?** Carts go in Redis or a table keyed by user (guest carts by a cookie id, merged at login). Orders, order lines with the price paid, and stock rows live in the database; the `Bill` is stored with the order so later price changes do not alter history.

**How do retries stay safe?** The client sends an idempotency key with checkout so a double submit creates one order. Payment webhooks are deduplicated by the gateway's event id, and `move_to` treats "already in that state" as a no-op, so side effects such as `commit` or a refund run once.

**How do you add "buy 2 get 1 free"?** Write a new `PricingStep` and insert it into the pipeline before `ApplyCoupon`. A new coupon type is a new `Coupon` subclass. No existing step changes; the order of the list is the business rule, in one place.

**What breaks at 10× (a flash sale)?** One hot SKU makes its stock row the bottleneck. Options: pre-split stock into N buckets and decrement a random bucket, keep the counter in Redis with an atomic Lua decrement and reconcile to the database, or put buyers through a queue. Everything else (carts, pricing) is stateless and scales horizontally.

**What do you log and measure?** Checkout success and failure by reason (out of stock, coupon invalid, payment failed), reservation timeouts, oversell attempts blocked, cart-to-order conversion, and the gap between reserved stock and pending orders. Log the full bill breakdown with the order id so support can explain any total.

**How do you keep money right?** `Decimal` everywhere, rounding half-up once per computed amount (each discount, the tax), never on intermediate sums. Store the currency with the order. When splitting a discount across lines for per-line tax or partial refunds, give leftover cents to the last line so lines add up to the total.

**How do coupon usage limits work?** Record a `coupon_redemption(coupon, user, order)` row with a unique constraint when the order is paid, not at checkout, and release it if the order is cancelled.

## Interview follow-ups

- **Why a pipeline instead of one function?** Each rule is small and testable on its own, and the order is explicit. Marketing adds rules weekly; each is a new class, not an edit.
- **Reserve at add-to-cart instead?** Only for scarce items (tickets, flash sales), with a short TTL. For normal products it locks stock in carts that never convert.
- **Partial cancellation of one line?** Add a line-level status, release that line's stock, and refund its share of the discounts; this is why storing per-line allocated discounts helps.
- **How does this connect to payment?** Checkout creates the order and calls a payment service with the order id as the idempotency key; the webhook drives `mark_paid`. See the [payment gateway]({{ '/lld/payment-gateway/' | relative_url }}).
