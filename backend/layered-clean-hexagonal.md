---
title: Layered, clean and hexagonal architecture
section: backend
order: 4
phase: "Phase 1 · Backend architecture"
summary: Layered, clean and hexagonal architecture compared, covering the dependency rule, ports and adapters, testing without a database, and how far to go in Django.
---

These three styles answer the same question inside one service: where does business logic live, and what is it allowed to depend on? Get it wrong and a pricing rule ends up split across a DRF serializer, a model `save()` override, a signal and a Celery task, and the only way to test it is with a database and a mocked HTTP client. Microservices and modules decide the boundaries between parts of a system. These styles decide the structure inside each part.

## Layered architecture

The oldest and most common style: code is organized into horizontal layers, each with one responsibility, and each layer depends only on the layers below it.

```text
 +------------------------------+
 | presentation                 |  views, serializers, templates
 +------------------------------+
 | application / service        |  use cases, transactions
 +------------------------------+
 | domain / business            |  rules, calculations
 +------------------------------+
 | persistence / data access    |  ORM, queries
 +------------------------------+
               |
          [ database ]
     dependencies point down, toward the database
```

- **Closed (strict) layers**: a request must pass through every layer in order. This gives good isolation but forces pass-through code.
- **Open (relaxed) layers**: a layer may skip lower layers, for example a view calling a read query directly. Less ceremony, but erosion is easier.

### How it maps to Django

| Layer | Django pieces |
|---|---|
| Presentation | URLs, views, DRF serializers (input parsing and output shape only) |
| Application | `services.py` functions: one per use case, owns `transaction.atomic()` |
| Domain | Model methods, plain functions, value objects |
| Persistence | Models, managers, querysets, `selectors.py` for reads |

Django models are Active Record: one class holds both the data and its persistence. So in practice the domain and persistence layers merge, which is fine for most apps.

### When to use it

CRUD-heavy apps, small teams and most Django and Rails codebases. It's familiar, low-ceremony and easy to onboard into.

### When not to

- **The domain depends on the database.** Dependencies point down, so business rules import the ORM. You can't test pricing without a database, and replacing a storage or integration means editing the domain.
- **The architecture sinkhole.** Mark Richards' term for requests that pass through every layer with no logic in most of them. A few pass-through requests are normal. If most are, the layers are ceremony.
- **Horizontal slicing across a big codebase.** Every feature touches every layer, and layers say nothing about which business module owns what. Combine layers with vertical modules (package by feature): `billing/` has its own views, services and models ([Monoliths and modular monoliths]({{ '/backend/monolith-and-modular-monolith/' | relative_url }})).

## Hexagonal architecture

Alistair Cockburn's **ports and adapters** (2005). The application core sits in the middle and knows nothing about HTTP, databases, brokers or vendors. It talks to the outside only through **ports**, which are interfaces the core defines. **Adapters** implement those ports for a specific technology. The hexagon shape doesn't mean anything; it just leaves room to draw several ports.

```text
     driving side                                driven side
 +-----------------+                         +----------------------+
 | DRF view        |---+                 +-->| DjangoOrderRepository|
 +-----------------+   |   +---------+   |   +----------------------+
 | Kafka consumer  |---+-->|  core   |---+-->| StripePaymentGateway |
 +-----------------+   |   | use     |   |   +----------------------+
 | CLI, tests      |---+   | cases + |   +-->| EmailNotifier        |
 +-----------------+       | domain  |       +----------------------+
                           +---------+
   adapters call the       core defines       adapters implement
   core's driving ports    all the ports      the driven ports
```

- **Driving (primary) ports** are what the outside world can ask the core to do: the use cases. Driving adapters (an HTTP view, a Kafka consumer, a CLI command, a test) translate their input into a call on a driving port.
- **Driven (secondary) ports** are what the core needs from the outside world: `OrderRepository`, `PaymentGateway`, `Notifier`, `Clock`, `EventPublisher`. Driven adapters implement them with Postgres, Stripe, SMTP or Kafka.

The mechanism underneath is dependency inversion. The core owns the interface and the adapter depends on the core, never the reverse. A **composition root** (a bootstrap module, a Django `AppConfig.ready()` or a factory function) is the one place that wires concrete adapters to the core.

There's a key symmetry here: the UI and the database are both just adapters. That's what makes the core testable with in-memory fakes and lets you swap Stripe for Adyen without touching business rules.

### When to use it

- Complex business logic that deserves fast, isolated tests.
- Many integrations: payment providers, LLM providers, ERPs, message brokers. Each gets an adapter.
- Multiple entry points into the same use cases (API, consumer, scheduled job, admin action).
- Long-lived core logic expected to outlive its framework.

### When not to

- Thin CRUD over a table: the ports simply mirror the ORM, which adds files without adding value.
- Teams that will write one interface per class "for testability" but only ever have one implementation, while the domain logic stays in the views.

## Clean architecture

Robert C. Martin's synthesis (a 2012 blog post, then the 2017 book) of hexagonal, onion (Jeffrey Palermo, 2008) and similar ideas into concentric circles:

```text
 +-------------------------------------------------------+
 | frameworks and drivers: Django, Postgres, Kafka, web  |
 |  +-------------------------------------------------+  |
 |  | interface adapters: controllers, presenters,    |  |
 |  | gateways, repositories implementations          |  |
 |  |  +-------------------------------------------+  |  |
 |  |  | use cases: application business rules     |  |  |
 |  |  |  +-------------------------------------+  |  |  |
 |  |  |  | entities: enterprise business rules |  |  |  |
 |  |  |  +-------------------------------------+  |  |  |
 |  |  +-------------------------------------------+  |  |
 |  +-------------------------------------------------+  |
 +-------------------------------------------------------+
     source code dependencies point inward only
```

**The dependency rule**: source code dependencies point only inward. Nothing in an inner circle may name anything in an outer circle: no class, function, variable or framework type. Data crosses boundaries as simple structures (DTOs or plain dataclasses), never as ORM rows or HTTP request objects. When an inner circle needs to call outward (save, send, publish), it calls an interface it owns, which an outer circle implements.

Martin's vocabulary adds **use case interactors** with input and output ports, and **presenters** that format the output for a delivery mechanism. In Python this is usually lighter: a use case is a class or function with its ports injected, and it returns a dataclass that the view serializes.

### When to use it

The same situations as hexagonal: complex, long-lived domains with several delivery mechanisms and integrations. It adds explicit rules about layers inside the core (entities vs use cases) that help large teams stay consistent.

### When not to

When the framework gives you most of the value. Django's admin, forms, generic views, DRF model serializers and the ORM all assume Active Record models. Full clean architecture means mapping between domain objects and ORM rows on every read and write, giving up much of that tooling, and maintaining parallel classes. It's worth it for a complex core and rarely worth it for the whole app.

## Comparing the three

| | Layered | Hexagonal | Clean |
|---|---|---|---|
| Dependency direction | Down, toward the DB | Inward, toward the core | Inward, through circles |
| Core imports the DB layer | Yes | No | No |
| Unit tests without a DB | Hard | Easy, with fake adapters | Easy |
| Ceremony | Low | Medium | High |
| Django fit | Native | Good for a focused core | Friction with Active Record |
| Best for | CRUD, small teams | Integration-heavy cores | Large, long-lived domains |

Hexagonal, onion and clean are one family: dependency inversion around a framework-free core, with different vocabularies. Layered architecture is the odd one out because its dependencies point at the database.

### How far to go in a Django codebase

A pragmatic ladder. Stop at the first rung that solves your problem:

1. **Fat models, thin views.** Fine for CRUD.
2. **Service layer plus selectors** (the HackSoft Django Styleguide pattern): writes go through `services.py` functions with keyword-only arguments, reads through `selectors.py`, views stay thin and serializers only handle input and output. This is the sweet spot for most teams.
3. **Ports and adapters for the complex core only**: the pricing engine, billing, scheduling, an LLM routing layer. Plain Python domain objects, `Protocol` ports, Django ORM adapters.
4. **Full clean architecture** across the app. Rarely worth it with Django.

*Architecture Patterns with Python* by Percival and Gregory walks the hexagonal route in Python with Flask and SQLAlchemy and includes a Django appendix. It's the best single reference for rung 3.

## In practice

### Runnable: a hexagonal payment use case

The core defines ports as `typing.Protocol` classes, so adapters satisfy them structurally without inheriting anything. The tests use in-memory adapters: no database, no network, milliseconds per run.

```python
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

# ---- core: domain and use case, no framework imports ----


class PaymentDeclined(Exception):
    pass


@dataclass
class Order:
    id: str
    customer_id: str
    total: Decimal
    status: str = "pending"
    payment_ref: str | None = None

    def ensure_payable(self) -> None:
        if self.status != "pending":
            raise ValueError(f"order {self.id} is {self.status}")

    def mark_paid(self, payment_ref: str) -> None:
        self.ensure_payable()
        self.status, self.payment_ref = "paid", payment_ref


class OrderRepository(Protocol):  # driven port
    def get(self, order_id: str) -> Order: ...
    def save(self, order: Order) -> None: ...


class PaymentGateway(Protocol):  # driven port
    def charge(self, customer_id: str, amount: Decimal, idempotency_key: str) -> str: ...


class Notifier(Protocol):  # driven port
    def order_paid(self, order: Order) -> None: ...


@dataclass
class PayForOrder:  # driving port: the use case
    orders: OrderRepository
    payments: PaymentGateway
    notifier: Notifier

    def __call__(self, order_id: str) -> Order:
        order = self.orders.get(order_id)
        order.ensure_payable()  # fail before any side effect
        ref = self.payments.charge(
            order.customer_id, order.total, idempotency_key=f"order-{order.id}"
        )
        order.mark_paid(ref)
        self.orders.save(order)
        self.notifier.order_paid(order)
        return order


# ---- driven adapters for tests ----


class InMemoryOrders:
    def __init__(self, *orders: Order):
        self.rows = {o.id: o for o in orders}

    def get(self, order_id: str) -> Order:
        return self.rows[order_id]

    def save(self, order: Order) -> None:
        self.rows[order.id] = order


class FakeGateway:
    def __init__(self, limit: Decimal):
        self.limit, self.keys = limit, []

    def charge(self, customer_id: str, amount: Decimal, idempotency_key: str) -> str:
        self.keys.append(idempotency_key)
        if amount > self.limit:
            raise PaymentDeclined(customer_id)
        return f"pay_{len(self.keys)}"


class RecordingNotifier:
    def __init__(self):
        self.sent: list[str] = []

    def order_paid(self, order: Order) -> None:
        self.sent.append(order.id)


if __name__ == "__main__":
    orders = InMemoryOrders(
        Order("o1", "c1", Decimal("40.00")),
        Order("o2", "c2", Decimal("5000.00")),
    )
    gateway, notifier = FakeGateway(limit=Decimal("1000")), RecordingNotifier()
    pay = PayForOrder(orders, gateway, notifier)

    paid = pay("o1")
    assert paid.status == "paid" and paid.payment_ref == "pay_1"
    assert notifier.sent == ["o1"] and gateway.keys == ["order-o1"]

    try:
        pay("o1")  # already paid: rejected before charging again
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert gateway.keys == ["order-o1"]

    try:
        pay("o2")
        raise AssertionError("expected PaymentDeclined")
    except PaymentDeclined:
        pass
    assert orders.get("o2").status == "pending" and notifier.sent == ["o1"]
    print("hexagonal use case: ok")
```

### The Django side: adapters and the composition root

Illustrative. The ORM model lives outside the core, and the repository maps between rows and domain objects:

```python
# orders/adapters/django_repo.py (illustrative)
from orders.core import Order
from orders.models import OrderRow  # Django model, outside the core


class DjangoOrderRepository:
    def get(self, order_id: str) -> Order:
        row = OrderRow.objects.get(public_id=order_id)
        return Order(
            id=row.public_id,
            customer_id=row.customer_public_id,
            total=row.total,
            status=row.status,
            payment_ref=row.payment_ref,
        )

    def save(self, order: Order) -> None:
        OrderRow.objects.filter(public_id=order.id).update(
            status=order.status, payment_ref=order.payment_ref
        )
```

```python
# orders/bootstrap.py (illustrative): the only module that names concrete adapters
from django.conf import settings

from orders.adapters.django_repo import DjangoOrderRepository
from orders.adapters.notifier import CeleryNotifier
from orders.adapters.stripe_gateway import StripeGateway
from orders.core import PayForOrder

pay_for_order = PayForOrder(
    orders=DjangoOrderRepository(),
    payments=StripeGateway(api_key=settings.STRIPE_API_KEY),
    notifier=CeleryNotifier(),
)
```

The DRF view is a driving adapter. It translates HTTP into a use case call and domain errors into status codes:

```python
# orders/api/views.py (illustrative)
from rest_framework.decorators import api_view
from rest_framework.response import Response

from orders.bootstrap import pay_for_order
from orders.core import PaymentDeclined


@api_view(["POST"])
def pay(request, order_id: str):
    try:
        order = pay_for_order(order_id)
    except PaymentDeclined:
        return Response({"detail": "payment declined"}, status=402)
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=409)
    return Response({"id": order.id, "status": order.status})
```

### Enforcing the dependency rule in CI

Illustrative import-linter contracts: the core may not import Django, DRF or any adapter, and the layers inside the app only depend downward.

```ini
[importlinter]
root_package = orders
include_external_packages = True

[importlinter:contract:core-is-pure]
name = Core does not import frameworks or adapters
type = forbidden
source_modules =
    orders.core
forbidden_modules =
    orders.adapters
    orders.api
    django
    rest_framework

[importlinter:contract:layers]
name = api -> bootstrap -> adapters -> core
type = layers
layers =
    orders.api
    orders.bootstrap
    orders.adapters
    orders.core
```

## Pitfalls

- **Ceremony without payoff.** An interface per class, a mapper per model and a use case per CRUD endpoint, while the hard logic still lives in a serializer. Apply ports where the logic and integrations are.
- **Anemic domain model.** Domain objects are bags of fields and every rule sits in services. Sometimes that's fine, but then admit you have a service layer over Active Record, not a domain model.
- **Framework types leaking inward.** Passing `request`, a `QuerySet` or a model instance into the core breaks the rule silently. Pass primitives and dataclasses.
- **Repositories that return querysets.** Persistence leaks out and callers chain filters anywhere. Return domain objects or lists of them. For complex reads, a selector that bypasses the domain model (CQRS-lite) is cleaner than an over-abstracted repository.
- **Business rules in `save()`, signals or serializers.** They run in surprising places, get skipped by `bulk_create` and `update()`, and can't be called from a Celery task or management command without faking a request. Move them into services or domain methods.
- **Transactions in the wrong place.** The use case owns the unit of work. Don't open a transaction in the view and then call a payment API inside it: a slow provider holds row locks and a pooled connection for seconds.
- **N+1 queries from mapping.** A repository that builds aggregates row by row fires a query per child. Prefetch inside the adapter ([Query optimization and N+1]({{ '/backend/query-optimization-and-n-plus-one/' | relative_url }})).

## Interview questions

**What's the main difference between layered and hexagonal architecture?**
Dependency direction. In layered architecture the business layer depends on the persistence layer below it. In hexagonal architecture the core defines ports and everything else, the database included, depends on the core through adapters.

**What is the dependency rule?**
Source code dependencies point only inward, toward higher-level policy. Inner code never names outer code. Data crosses boundaries as simple structures, and outward calls go through interfaces that the inner circle owns.

**What are driving and driven ports?**
Driving ports are the use cases the outside world invokes, through adapters like HTTP views, consumers, CLIs and tests. Driven ports are what the core needs from outside, such as repositories, gateways, notifiers and clocks, implemented by adapters for specific technologies.

**How do you test business logic without a database?**
Put it in a core that depends only on ports, then run it against in-memory adapters. Keep a small set of integration tests that run the real adapters against Postgres and the provider sandbox.

**Does clean architecture fit Django?**
Partially. Django is built around Active Record, so full clean architecture means mapping and giving up much of the admin and ORM tooling. Use a service layer everywhere and full ports and adapters only for the complex core.

**Where should transactions live?**
In the application layer (the use case), often behind a unit-of-work port. Not in views, and never spanning slow external calls.

**Hexagonal vs onion vs clean?**
The same idea, dependency inversion around a framework-free core. Onion and clean add named rings inside the core, while hexagonal focuses on the ports at its edge.

## Further reading

- Alistair Cockburn, "Hexagonal Architecture" (the original ports and adapters article, 2005).
- Robert C. Martin, *Clean Architecture* (Prentice Hall, 2017).
- Harry Percival and Bob Gregory, *Architecture Patterns with Python* (O'Reilly, 2020).
- Mark Richards, *Software Architecture Patterns* (O'Reilly report).
- HackSoft, "Django Styleguide" (GitHub).
