---
title: UML for interviews
section: lld
order: 3
phase: "Phase 1 · OOP foundations"
summary: The class and sequence diagram notation an LLD round needs, how to draw both fast in text or on a whiteboard, and what to leave out.
---

An LLD round needs two diagrams. A **class diagram** shows structure: which classes exist, what each owns, and how they relate. A **sequence diagram** shows behavior: who calls whom, in what order, for one use case. Both are for talking, not for completeness. Budget about 5 minutes for each.

## Class box

```text
+---------------------------+
| ParkingLot                |   name (<<interface>>, <<enum>> above it)
+---------------------------+
| - floors: list[Floor]     |   fields: only the ones that matter
| - pricing: Pricing        |
+---------------------------+
| + park(v): Ticket         |   methods: the public API from use cases
| + unpark(id): Receipt     |
+---------------------------+
```

## Notation cheat sheet

| Visibility | Meaning | Python |
|---|---|---|
| `+` | Public | `name` |
| `-` | Private | `__name` (or `_name` in practice) |
| `#` | Protected | `_name` |

| Multiplicity | Reads as |
|---|---|
| `1` | Exactly one |
| `0..1` | Optional |
| `*` or `0..*` | Any number |
| `1..*` | At least one |

Multiplicity goes at the far end of the line, next to the class being counted: `Order 1 --- 1..* OrderItem` reads "an order has one or more items".

Relationships, in the text form Mermaid and PlantUML use (fast to type, and interviewers recognize it):

```text
Relationship  Text form            Arrow on a whiteboard
Inheritance   Car --|> Vehicle     solid line, hollow triangle at parent
Realization   Card ..|> Payment    dashed line, hollow triangle at parent
Composition   Order *-- OrderItem  filled diamond at the owner
Aggregation   Team o-- Player      hollow diamond at the owner
Association   Order --> Customer   solid line, open arrow
Dependency    Service ..> Gateway  dashed line, open arrow
```

| Relationship | Means | Python shape |
|---|---|---|
| Inheritance | Is-a | `class Car(Vehicle)` |
| Realization | Implements a contract | Subclass of an ABC, or matches a `Protocol` |
| Composition | Owns; part dies with whole | Created inside the owner (`self.items.append(OrderItem(...))`) |
| Aggregation | Has; part lives on | Passed in and stored (`self.players = players`) |
| Association | Knows about | Stored reference to an independent object |
| Dependency | Uses briefly | Method parameter or local variable, never stored |

## Worked class diagram

An order checkout: a customer orders products, pays with a card or UPI. In ASCII, `<*>` is a filled diamond, `^` a triangle, `|` and `-` solid lines, `:` a dashed line.

```text
                  +---------------------------+
                  | OrderService              |
                  +---------------------------+
                  | - orders: dict[int,Order] |
                  +---------------------------+
                  | + checkout(customer, cart,|
                  |     payment): Order       |
                  +---------------------------+
                     | 1                  :
                     |                    : uses
                     v *                  v
+-------------+ 1  * +----------------+  +----------------------------+
| Customer    |<-----| Order          |  | <<interface>>              |
+-------------+      +----------------+  | PaymentMethod              |
| + id: str   |      | - status       |  +----------------------------+
| + name: str |      +----------------+  | + pay(amt): bool           |
+-------------+      | + add(p, qty)  |  +----------------------------+
                     | + total()      |         ^              ^
                     | + mark_paid()  |         :              :
                     +----------------+  +-------------+ +------------+
                       <*> 1             | CardPayment | | UpiPayment |
                        |                +-------------+ +------------+
                        | 1..*
                     +----------------+
                     | OrderItem      |
                     | + qty: int     |
                     | + subtotal()   |
                     +----------------+
                        | *
                        v 1
                     +----------------+
                     | Product        |
                     | + sku, price   |
                     +----------------+
```

The same diagram as text, which is what you would type into a shared doc:

```text
OrderService "1" --> "*" Order
OrderService ..> PaymentMethod
Order "*" --> "1" Customer
Order "1" *-- "1..*" OrderItem
OrderItem "*" --> "1" Product
CardPayment ..|> PaymentMethod
UpiPayment ..|> PaymentMethod
```

Each arrow maps to one line of code:

```python
import itertools
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class OrderStatus(Enum):
    CREATED = "created"
    PAID = "paid"


@dataclass(frozen=True)
class Customer:
    id: str
    name: str


@dataclass(frozen=True)
class Product:
    sku: str
    price: Decimal


@dataclass
class OrderItem:
    product: Product                    # association
    qty: int

    def subtotal(self) -> Decimal:
        return self.product.price * self.qty


class PaymentMethod(ABC):               # <<interface>>
    @abstractmethod
    def pay(self, amount: Decimal) -> bool: ...


class CardPayment(PaymentMethod):       # realization
    def pay(self, amount: Decimal) -> bool:
        return amount <= Decimal("50000")


class UpiPayment(PaymentMethod):
    def pay(self, amount: Decimal) -> bool:
        return amount <= Decimal("100000")


@dataclass
class Order:
    id: int
    customer: Customer                  # association
    items: list[OrderItem] = field(default_factory=list)  # composition
    status: OrderStatus = OrderStatus.CREATED

    def add(self, product: Product, qty: int) -> None:
        self.items.append(OrderItem(product, qty))  # creates its parts

    def total(self) -> Decimal:
        return sum((i.subtotal() for i in self.items), Decimal("0"))

    def mark_paid(self) -> None:
        self.status = OrderStatus.PAID


class OrderService:
    def __init__(self):
        self._orders: dict[int, Order] = {}         # association, 1 to *
        self._ids = itertools.count(1)

    def checkout(self, customer: Customer, cart: list[tuple[Product, int]],
                 payment: PaymentMethod) -> Order:  # dependency: parameter
        order = Order(next(self._ids), customer)
        for product, qty in cart:
            order.add(product, qty)
        if not payment.pay(order.total()):
            raise RuntimeError("payment declined")
        order.mark_paid()
        self._orders[order.id] = order
        return order


if __name__ == "__main__":
    asha = Customer("c1", "Asha")
    pen = Product("pen", Decimal("20"))
    book = Product("book", Decimal("450"))
    cart = [(pen, 2), (book, 1)]
    order = OrderService().checkout(asha, cart, UpiPayment())
    assert order.total() == Decimal("490")
    assert order.status is OrderStatus.PAID
    try:
        OrderService().checkout(asha, [(book, 200)], CardPayment())
        raise AssertionError("expected a decline")
    except RuntimeError:
        pass
```

## Worked sequence diagram

The `checkout` flow. Participants across the top, time flows down. A solid arrow is a call, a dashed arrow is a return, and a frame (`alt`, `opt`, `loop`) marks a branch or repetition.

```text
Customer      OrderService        Order          PaymentMethod
   |               |                |                  |
   | checkout()    |                |                  |
   |-------------->|                |                  |
   |               | Order(id, c)   |                  |
   |               |--------------->|                  |
   |  +-[loop: each cart line]-------------+           |
   |  |            | add(p, qty)    |      |           |
   |  |            |--------------->|      |           |
   |  +------------------------------------+           |
   |               | total()        |                  |
   |               |--------------->|                  |
   |               |<- - - - amount |                  |
   |               | pay(amount)                       |
   |               |---------------------------------->|
   |               |<- - - - - - - - - - - - - - true  |
   |               | mark_paid()    |                  |
   |               |--------------->|                  |
   |<- - - order   |                |                  |
   |               |                |                  |
 +-[alt: pay() returns false]-------------------------------+
 | OrderService raises "payment declined"; order not saved  |
 +----------------------------------------------------------+
```

Pick the flow with the most collaborators or the trickiest failure path (payment, booking, a state change). One diagram is usually enough.

## How to draw fast

1. Class names only, from the nouns in your use cases (1 minute).
2. Relationships with multiplicity (2 minutes).
3. Fields that carry relationships or state, and the public methods from the use cases (2 minutes).
4. Tag interfaces and enums.
5. One sequence diagram for the riskiest flow, then code.

## What to skip

- Getters, setters, constructors, private helpers, types on every field.
- Use-case, activity, component and deployment diagrams. A bullet list of use cases is enough.
- Return arrows for trivial calls.
- Arrowhead debates. Unsure between aggregation and association? Draw an association and say why.

A small state list (`IDLE -> SELECTING -> DISPENSING`) is worth adding for lifecycle-heavy problems like a vending machine or an elevator.

## Pitfalls

- Diamond at the wrong end. It sits at the owner (the whole), not the part.
- Inheritance arrow pointing at the child. The triangle points at the parent.
- One class with arrows to everything: a god class, or a missing service.
- 15 minutes of diagramming and no code.

## Interview questions

- **Composition or aggregation?** Who creates the part and whether it survives the whole. `Order` creates `OrderItem` and items die with it: composition. `Team` receives existing `Player` objects: aggregation.
- **Association or dependency?** Association is stored as a field. Dependency is only a parameter or local variable, so the coupling lasts one call.
- **Interface vs abstract class in UML?** `<<interface>>` has only method signatures; an abstract class can carry state and concrete methods. In Python both are usually an `ABC`, or a `Protocol` for the interface.
