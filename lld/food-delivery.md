---
title: Design a food delivery system
section: lld
order: 41
phase: "Phase 5 · Advanced"
summary: Restaurants, menus, a single-restaurant cart, an order state machine, partner assignment strategy, live tracking via observers, and refund rules.
---

A food delivery app (Swiggy, DoorDash, Uber Eats) has three parties on every order: customer, restaurant and delivery partner. The design centers on the order state machine, who is allowed to move it, and what a cancellation costs at each step.

## Requirements

Clarifying questions:
- Can a cart hold items from two restaurants? No, one restaurant per cart.
- Does the restaurant confirm each order? Yes, it can accept or reject.
- When is a partner assigned? On restaurant accept, so they arrive as food is ready.
- What does cancellation refund? Full before accept, half after the kitchen starts, nothing once food is ready.
- Payment? Assume prepaid; we only compute refund amounts.

Functional:
- Browse restaurants and menus; add available items to a cart.
- Place an order (prices snapshotted at order time).
- Restaurant accepts or rejects; the order moves through ready, picked up, delivered.
- Assign the best free delivery partner via a pluggable strategy.
- Customers track status and partner location live.
- Customer cancellation with refund rules; rejection refunds fully.

Non-functional:
- A partner carries one order at a time; no double assignment under load.
- Status updates reach subscribers within seconds.

Out of scope: payments, ratings, search, batching multiple orders per partner, ETA prediction.

## Entities and relationships

```text
+-------------+ 1   * +-------------+
| Restaurant  |------>| MenuItem    |
| id, loc     |       | id, price   |
| menu, open  |       | available   |
+------^------+       +-------------+
       | 0..1
+------+------+ place() +---------------+ uses +----------------------+
| Cart        |-------->| OrderService  |----->| AssignmentStrategy   |
| customer_id |         | orders        |      | pick(order, partners)|
| restaurant  |         | partners      |      +----------^-----------+
| items {id:q}|         | subscribers   |                 | is-a
+-------------+         | respond()     |      +----------+-----------+
                        | mark_ready()  |      | NearestFreePartner   |
                        | pick_up()     |      +----------------------+
                        | deliver()     | notifies +-----------------+
                        | cancel()      |--------->| OrderObserver   |
                        +-------+-------+          | on_event()      |
                                | 1..*             +-----------------+
                        +-------v----------+ 0..1 +-----------------+
                        | Order            |----->| DeliveryPartner |
                        | id, customer_id  |      | id, location    |
                        | lines, total     |      | current_order   |
                        | status, refunded |      +-----------------+
                        +------------------+
```

## Patterns and why

| Pattern | Where | Why |
|---|---|---|
| State machine | `ALLOWED` + `_move()` | Stops illegal jumps like delivering an unaccepted order |
| Strategy | `AssignmentStrategy` | Swap nearest-first for load-balanced or batched assignment |
| Observer | `OrderObserver` subscribers per order | Customer app, restaurant screen and analytics get events without the service knowing them |
| Table-driven policy | `REFUND_SHARE` | Refund rules change without touching cancel logic |

## Code

```python
from __future__ import annotations

import itertools
import math
import threading
from abc import ABC, abstractmethod
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Protocol


class OrderStatus(Enum):
    PLACED = "placed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    READY = "ready"
    PICKED_UP = "picked_up"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


S = OrderStatus
ALLOWED = {
    S.PLACED: {S.ACCEPTED, S.REJECTED, S.CANCELLED},
    S.ACCEPTED: {S.READY, S.CANCELLED},
    S.READY: {S.PICKED_UP},
    S.PICKED_UP: {S.DELIVERED},
}
REFUND_SHARE = {S.PLACED: Decimal("1"), S.ACCEPTED: Decimal("0.5")}


@dataclass(frozen=True)
class MenuItem:
    id: str
    name: str
    price: Decimal
    available: bool = True


@dataclass(eq=False)
class Restaurant:
    id: str
    location: tuple[float, float]
    menu: dict[str, MenuItem]
    is_open: bool = True


@dataclass(eq=False)
class DeliveryPartner:
    id: str
    location: tuple[float, float]
    current_order: int | None = None


@dataclass(eq=False)
class Order:
    id: int
    customer_id: str
    restaurant: Restaurant
    lines: dict[str, tuple[int, Decimal]]  # item -> (qty, price at order time)
    total: Decimal
    status: OrderStatus = S.PLACED
    partner: DeliveryPartner | None = None
    refunded: Decimal = Decimal("0")


class Cart:
    def __init__(self, customer_id: str):
        self.customer_id = customer_id
        self.restaurant: Restaurant | None = None
        self.items: Counter[str] = Counter()

    def add(self, restaurant: Restaurant, item_id: str, qty: int = 1) -> None:
        if self.restaurant not in (None, restaurant):
            raise ValueError("a cart holds items from one restaurant")
        item = restaurant.menu.get(item_id)
        if item is None or not item.available or qty <= 0:
            raise ValueError(f"cannot add {item_id}")
        self.restaurant = restaurant
        self.items[item_id] += qty


class OrderObserver(Protocol):
    def on_event(self, order: Order, event: str) -> None: ...


class AssignmentStrategy(ABC):
    @abstractmethod
    def pick(self, order: Order, partners: list[DeliveryPartner]) -> DeliveryPartner | None: ...


class NearestFreePartner(AssignmentStrategy):
    def pick(self, order, partners):
        free = [p for p in partners if p.current_order is None]
        here = order.restaurant.location
        return min(free, key=lambda p: math.dist(p.location, here), default=None)


class InvalidTransition(Exception):
    pass


class OrderService:
    def __init__(self, strategy: AssignmentStrategy, partners: list[DeliveryPartner]):
        self.strategy, self.partners = strategy, partners
        self.orders: dict[int, Order] = {}
        self.subscribers: dict[int, list[OrderObserver]] = defaultdict(list)
        self._lock = threading.Lock()  # Simplification: one lock; shard by city/zone at scale
        self._ids = itertools.count(1)

    def subscribe(self, order_id: int, observer: OrderObserver) -> None:
        self.subscribers[order_id].append(observer)

    def place(self, cart: Cart) -> Order:
        r = cart.restaurant
        if r is None or not cart.items or not r.is_open:
            raise ValueError("empty cart or restaurant closed")
        lines = {i: (q, r.menu[i].price) for i, q in cart.items.items()}
        total = sum((q * p for q, p in lines.values()), Decimal("0"))
        with self._lock:
            order = Order(next(self._ids), cart.customer_id, r, lines, total)
            self.orders[order.id] = order
        cart.items.clear()
        cart.restaurant = None
        return order

    def respond(self, order_id: int, accept: bool) -> None:
        with self._lock:
            order = self.orders[order_id]
            if not accept:
                self._move(order, S.REJECTED)
                order.refunded = order.total
                events = ["rejected"]
            else:
                self._move(order, S.ACCEPTED)
                events = ["accepted", self._assign(order)]
        self._emit(order, *events)

    def mark_ready(self, order_id: int) -> None:
        self._step(order_id, S.READY)

    def pick_up(self, order_id: int) -> None:
        self._step(order_id, S.PICKED_UP)

    def deliver(self, order_id: int) -> None:
        self._step(order_id, S.DELIVERED)

    def cancel(self, order_id: int) -> Decimal:
        with self._lock:
            order = self.orders[order_id]
            share = REFUND_SHARE.get(order.status)
            if share is None:
                raise InvalidTransition(f"cannot cancel when {order.status.value}")
            self._move(order, S.CANCELLED)
            order.refunded = (order.total * share).quantize(Decimal("0.01"))
        self._emit(order, "cancelled")
        return order.refunded

    def update_partner_location(self, partner: DeliveryPartner, loc) -> None:
        partner.location = loc
        if partner.current_order is not None:
            self._emit(self.orders[partner.current_order], f"location {loc}")

    def _step(self, order_id: int, new: OrderStatus) -> None:
        with self._lock:
            order = self.orders[order_id]
            self._move(order, new)
        self._emit(order, new.value)

    def _assign(self, order: Order) -> str:  # caller holds the lock
        partner = self.strategy.pick(order, self.partners)
        if partner is None:
            return "awaiting partner"
        partner.current_order, order.partner = order.id, partner
        return f"partner {partner.id}"

    def _move(self, order: Order, new: OrderStatus) -> None:  # caller holds the lock
        if new not in ALLOWED.get(order.status, set()):
            raise InvalidTransition(f"{order.status.value} -> {new.value}")
        if new is S.PICKED_UP and order.partner is None:
            raise InvalidTransition("no partner assigned")
        order.status = new
        if new in (S.DELIVERED, S.CANCELLED) and order.partner:
            order.partner.current_order = None  # free the partner

    def _emit(self, order: Order, *events: str) -> None:  # outside the lock
        for event in events:
            for obs in list(self.subscribers[order.id]):
                obs.on_event(order, event)


class Recorder(list):
    def on_event(self, order, event):
        self.append(event)


if __name__ == "__main__":
    menu = {"dosa": MenuItem("dosa", "Masala dosa", Decimal("120.00")),
            "coffee": MenuItem("coffee", "Filter coffee", Decimal("40.00"))}
    cafe = Restaurant("cafe", (0.0, 0.0), menu)
    other = Restaurant("other", (5.0, 5.0), dict(menu))
    near, far = DeliveryPartner("near", (1.0, 1.0)), DeliveryPartner("far", (9.0, 9.0))
    svc = OrderService(NearestFreePartner(), [far, near])

    cart = Cart("c1")
    cart.add(cafe, "dosa", 2)
    cart.add(cafe, "coffee")
    try:
        cart.add(other, "dosa")
        raise AssertionError("mixed restaurants")
    except ValueError:
        pass
    order = svc.place(cart)
    assert order.total == Decimal("280.00") and not cart.items

    tracker = Recorder()
    svc.subscribe(order.id, tracker)
    svc.respond(order.id, accept=True)
    assert order.partner is near
    svc.mark_ready(order.id)
    svc.pick_up(order.id)
    svc.update_partner_location(near, (0.5, 0.5))
    svc.deliver(order.id)
    assert tracker == ["accepted", "partner near", "ready", "picked_up",
                       "location (0.5, 0.5)", "delivered"]
    assert near.current_order is None

    def new_order() -> Order:
        c = Cart("c2")
        c.add(cafe, "dosa")
        return svc.place(c)

    o1 = new_order()
    assert svc.cancel(o1.id) == Decimal("120.00")  # before accept: full
    o2 = new_order()
    svc.respond(o2.id, accept=True)
    assert svc.cancel(o2.id) == Decimal("60.00")  # kitchen started: half
    assert o2.partner.current_order is None
    o3 = new_order()
    svc.respond(o3.id, accept=False)
    assert o3.status is S.REJECTED and o3.refunded == Decimal("120.00")

    # stress: 50 orders accepted at once, 5 partners: no partner gets two
    crew = [DeliveryPartner(f"p{i}", (i, i)) for i in range(5)]
    svc.partners = crew
    batch = [new_order() for _ in range(50)]
    threads = [threading.Thread(target=svc.respond, args=(o.id, True)) for o in batch]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assigned = [o.partner.id for o in batch if o.partner]
    assert len(assigned) == 5 == len(set(assigned))
    print("ok")
```

## Key flows

Place, accept and assign:

```text
Customer  Restaurant    OrderService    Strategy    Partner     Observer
  |------- place(cart) ------>|               |           |           |
  |            |              | PLACED        |           |           |
  |<--------- order ----------|               |           |           |
  |            |--- accept -->|               |           |           |
  |            |              | lock: ACCEPTED|           |           |
  |            |              |---- pick() -->|           |           |
  |            |              |<-- nearest ---|           |           |
  |            |              |---------- assign -------->|           |
  |            |              | unlock        |           |           |
  |            |              |------------- emit events ------------>|
```

Cancel: lock → look up `REFUND_SHARE[status]` (missing means not cancellable) → move to CANCELLED → free partner → emit outside the lock.

## Edge cases

- Item goes unavailable after it is in the cart: re-check availability at `place()` in a real system and reject the line.
- Menu price changes after placing: the order keeps its snapshot price.
- No free partner on accept: order stays ACCEPTED with "awaiting partner"; a scheduler re-runs `_assign` for orders without a partner.
- Pickup without a partner: rejected.
- Restaurant never responds: auto-reject after a timeout (say 5 minutes) with a full refund.
- Observer raises: wrap each call so one bad subscriber doesn't block the rest (omitted for brevity).

## Production follow-ups

**Thread safety / concurrency?** Assignment is the race: two accepted orders can both see the same partner as free. One service lock makes "pick a free partner and mark them busy" atomic; events are emitted after releasing it so a slow subscriber can't stall assignment. Across machines this becomes a conditional update on the partner row (`WHERE current_order IS NULL`) or a per-zone single-writer dispatcher.

**Persistence and restart?** Orders and their transitions go to PostgreSQL with guarded updates (`UPDATE ... WHERE status='accepted'`). Write domain events to an outbox table in the same transaction and publish from it, so a crash between "state changed" and "event sent" never loses a notification. On restart, a sweeper finds ACCEPTED orders without a partner and retries assignment.

**Idempotency and retries?** `place()` takes a client idempotency key so a double tap creates one order. Restaurant and partner actions are transitions, and repeating one fails cleanly; return success if the order is already in the target state. Refunds carry a key derived from the order id so the payment provider never refunds twice.

**Extensibility?** New assignment logic (least busy, batched by route, rating-aware) is a new `AssignmentStrategy`. New refund rules are table edits, or a `RefundPolicy` strategy per city. New notification channels are new observers.

**Scale (10×, across machines)?** Shard by city: orders, partners and the assignment lock never cross a city. Partner locations stream through Redis geo or a stream processor, not the order database. Tracking fan-out (location every 5 seconds to every active customer) moves to a pub-sub layer with WebSockets.

**Observability?** Time in each state (placed to accepted, ready to picked up), rejection and cancellation rates per restaurant, unassigned orders older than N minutes, partner utilization. Log each transition with order, restaurant and partner ids so support can reconstruct a complaint.

**Money precision?** Totals and refunds are `Decimal`, rounded to cents once, at the end. Store the price per line at order time so later menu edits never change a past order.

## Interview follow-ups

- **Why assign on accept rather than on ready?** The partner needs travel time; assigning at ready makes food wait. Some systems assign at a predicted "ready minus travel time" moment.
- **How do you support multiple restaurants in one order?** Split into one sub-order per restaurant, each with its own state machine, under a parent for payment.
- **How do you handle a partner cancelling mid-delivery?** Clear `current_order`, keep the order in its state, re-run assignment, and notify the customer.
- **Who can trigger which transition?** Restaurant: accept, reject, ready. Partner: pick up, deliver. Customer: cancel. Check the actor's role before calling the service.
