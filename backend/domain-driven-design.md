---
title: Domain-driven design
section: backend
order: 5
phase: "Phase 1 · Backend architecture"
summary: Strategic DDD (ubiquitous language, bounded contexts, context maps) and tactical DDD (entities, value objects, aggregates, events, repositories).
---

Domain-driven design (DDD) is a toolkit for software whose difficulty comes from the business rather than the technology: billing with proration and credits, insurance policies that lapse and reinstate, scheduling with resource conflicts. It works at two levels. **Strategic design** decides where the boundaries go and how the parts relate, which is also how you choose module and service boundaries. **Tactical design** decides how to model inside one boundary so that business rules hold under concurrency. Most "how would you split this into services?" interview questions are strategic DDD questions in disguise.

## Strategic design

### Ubiquitous language

A shared, precise vocabulary that domain experts and developers use for one part of the business, everywhere: in conversation, tickets, docs, class names, method names, event names and API fields. If underwriters say "a policy lapses when a premium is missed past the grace period", the code says `policy.lapse()` and emits `PolicyLapsed`, not `policy.status = 3` inside a nightly job called `cleanup_task`.

Translation is where bugs come from: if the business says "subscription" and the code says `Plan`, `Account` or `BillingProfile` depending on the file, every requirement is translated twice and edge cases get lost. When experts adopt a new term ("a paused subscription isn't a cancelled one"), that's a model change, and the code changes with it. A language is valid only within one context, which is what bounded contexts capture.

### Subdomains

Before drawing boundaries, classify the problem space:

| Subdomain | Definition | Investment | Example in a SaaS billing product |
|---|---|---|---|
| Core | Your competitive advantage | Best engineers, rich model, build in-house | Usage rating, pricing rules |
| Supporting | Necessary and specific to you, not differentiating | Simpler model, build lean | Tenant onboarding, reporting |
| Generic | Solved problems | Buy or use SaaS | Auth, email delivery, card processing |

Tactical DDD patterns pay off in the core. Spending them on a generic subdomain is waste.

### Bounded contexts

A bounded context is an explicit boundary inside which one model and one ubiquitous language are consistent. Outside it, the same words may mean something else, and that's allowed.

The canonical example is "product" in e-commerce:

```text
 +-------------+  +-------------+  +-------------+  +-------------+
 |  Catalog    |  |  Pricing    |  |  Inventory  |  |  Shipping   |
 |             |  |             |  |             |  |             |
 | Product:    |  | Product:    |  | Product:    |  | Product:    |
 |  title      |  |  price list |  |  SKU        |  |  weight     |
 |  images     |  |  promotions |  |  stock per  |  |  dimensions |
 |  SEO slug   |  |  tax class  |  |  warehouse  |  |  hazmat     |
 +-------------+  +-------------+  +-------------+  +-------------+
     one product_id shared across contexts, four different models
```

The alternative is one `Product` table with 80 columns, owned by nobody, where every team's migration blocks every other team. Bounded contexts let each team model just what it needs, linked by a shared identifier.

How contexts relate to the rest of the architecture:

- **Subdomain vs bounded context.** A subdomain is part of the problem, a bounded context part of the solution. Ideally one maps to one.
- **Context vs service.** A bounded context is the best first cut for a module in a [modular monolith]({{ '/backend/monolith-and-modular-monolith/' | relative_url }}) or a [microservice]({{ '/backend/microservices-and-soa/' | relative_url }}). One context may become several services, but one service should not span contexts.
- **Context vs team.** One team per context keeps the language coherent.

To find boundaries, listen for the same word used with different meanings, and look at rates of change and at which data must be consistent together. **Event storming** (Alberto Brandolini) is the standard workshop: put domain events on a timeline, add the commands, actors and policies behind them, and look for pivotal events where responsibility hands off. Those handoffs are usually context boundaries.

### Context maps

A context map documents how bounded contexts relate: both the technical integration and the power dynamics between the teams. Relationships are directional. The **upstream** context's changes affect the **downstream** one, not the other way around.

| Pattern | Relationship | Use when |
|---|---|---|
| Partnership | Two teams plan together, succeed or fail together | Tightly linked features, aligned teams |
| Shared kernel | Two contexts share a small piece of model or code | The overlap is small and stable, changes are agreed |
| Customer–supplier | Upstream plans with downstream's needs in mind | Internal teams with a working relationship |
| Conformist | Downstream adopts upstream's model as is | No leverage, upstream model is good enough |
| Anticorruption layer (ACL) | Downstream translates upstream into its own model | Legacy or external model would pollute yours |
| Open host service (OHS) | Upstream offers one well-defined API for all consumers | Many consumers, avoid bespoke integrations |
| Published language | A documented exchange format (schema, standard) | Paired with OHS, or industry standards |
| Separate ways | No integration, duplicate if needed | Integration costs more than it returns |
| Big ball of mud | Draw a boundary around the mess | Legacy you contain rather than fix |

```text
            +-----------------+
            |  Identity (U)   |  OHS + published language (OIDC)
            +--------+--------+
                     | conformist
        +------------v-----------+        +-------------------+
        |   Billing (core) (D)   |<-------|  Legacy CRM (U)   |
        |                        |  ACL   |  big ball of mud  |
        +------------+-----------+        +-------------------+
                     | customer-supplier
            +--------v--------+
            |  Reporting (D)  |
            +-----------------+
      U = upstream, D = downstream
```

The map is a planning tool: if your core domain is conformist to a vendor's model, that's a strategic risk worth an ACL.

## Tactical design

Tactical patterns are the building blocks inside one bounded context.

### Entities

An entity is defined by **identity and continuity**, not by its attributes. An order stays the same order as its lines and status change, and two orders with identical fields are still different orders. Equality is by ID, and changes go through methods that enforce rules rather than public setters. IDs can come from a database sequence or the application (UUID, ULID). Application-generated IDs let you build events and references before the insert, which simplifies the outbox and idempotency. Django model instances are already entities keyed by primary key.

### Value objects

A value object is defined entirely by its attributes and has no identity: `Money(10.00, "USD")`, `Address`, `DateRange`, `EmailAddress`, `Percentage`.

- Immutable: you replace it rather than change it.
- Equality is by value.
- Self-validating: an invalid `Money` can't be constructed.
- Behavior lives on it: `Money.__add__` refuses to add USD to EUR, and `DateRange.overlaps()` lives with the range.

They cure **primitive obsession**: passing `amount: float, currency: str` through 30 functions that may or may not check the currency. In Python, use `@dataclass(frozen=True)` with `Decimal`. In Django, map one to several columns or a custom field.

### Aggregates

An aggregate is a cluster of entities and value objects treated as **one unit for changes**, with one entity as the **aggregate root**. Outside code holds references only to the root and changes the aggregate only through the root's methods. The aggregate is the **consistency boundary**: its invariants are true at the end of every transaction.

For an `Order` aggregate (root `Order`, child `OrderLine`s), the invariants are: a placed order can't be edited, the total equals the sum of the lines, quantities are positive, there are at most 50 lines, and every line uses the order's currency.

Vaughn Vernon's rules from "Effective Aggregate Design" are the standard guidance:

1. **Model true invariants in consistency boundaries.** Only rules that must hold *immediately* belong inside one aggregate.
2. **Design small aggregates.** Large ones load slowly and conflict often under concurrency.
3. **Reference other aggregates by identity.** `Order` holds `customer_id`, not a `Customer` object.
4. **Use eventual consistency outside the boundary.** Rules across aggregates are enforced through domain events and follow-up transactions.

A corollary that works as a rule of thumb: **modify one aggregate per transaction**.

| | Large aggregate (Customer with all orders) | Small aggregates (Customer, Order) |
|---|---|---|
| Invariants across children | Immediate | Eventual, via events |
| Load cost | Whole graph | One root and its children |
| Concurrency conflicts | Frequent | Rare |
| Transaction scope | Wide, more locks | Narrow |

Rules that span many aggregates, like "usernames are unique" or "don't oversell stock across thousands of orders", don't fit inside one aggregate. Use a database constraint (a unique index is a perfectly good set-level invariant), a dedicated aggregate that holds the scarce resource (a `StockItem` with a reservation method), or eventual consistency with compensation.

Concurrency control is per aggregate, usually **optimistic locking**: a `version` column checked and incremented on every save, so one of two concurrent edits fails and retries instead of silently overwriting the other ([Locking]({{ '/postgresql/locking/' | relative_url }})). The aggregate ID is also the natural partition key for its events, which gives per-aggregate ordering in Kafka.

### Domain events

A domain event records something domain experts care about, in the past tense: `OrderPlaced`, `PaymentCaptured`, `PolicyLapsed`. The aggregate raises it as part of a state change, and the application dispatches it **after the transaction commits**, so handlers never react to rolled-back changes.

- **Inside the context**, handlers run follow-up work: a `StockReservation` reacts to `OrderPlaced`, which is how cross-aggregate rules become eventually consistent.
- **Across contexts**, translate the event into an **integration event**, a deliberate, versioned contract published through an outbox ([Event-driven architecture]({{ '/backend/event-driven-architecture/' | relative_url }})). Internal domain events can change freely, while integration events can't.

### Repositories

A repository gives collection-like access to aggregates: `get(order_id)`, `add(order)`, `save(order)`. It loads and stores **whole aggregates**, root plus children, so callers never touch child tables directly.

- One repository per aggregate root, not per table. There's no `OrderLineRepository`.
- The interface belongs to the domain, and the implementation lives in infrastructure (the [hexagonal]({{ '/backend/layered-clean-hexagonal/' | relative_url }}) driven port and adapter).
- It returns domain objects, not query builders.
- A **unit of work** wraps the transaction around one use case's repository calls and collects events to dispatch after commit.

A DAO mirrors tables with CRUD per table, while a repository mirrors aggregates. Django's manager is already DAO-like, so add a repository only when the domain model is separate from the ORM or the aggregate must load and save as a unit.

### Domain services, application services and factories

- **Domain service**: a stateless domain operation that belongs to no single entity, such as transferring funds between two accounts.
- **Application service**: orchestrates a use case. It loads aggregates, calls their methods, saves them and owns the transaction, but holds no business rules.
- **Factory**: builds a valid aggregate when construction is complex. A classmethod is often enough.

## When to use DDD and when not

Strategic design is cheap and pays off almost everywhere: naming contexts, their owners and how they integrate helps even a CRUD-heavy product. Tactical patterns cost code, so spend them on a complex, long-lived core with real invariants. Skip them for admin CRUD, ETL pipelines and purely technical components like a rate limiter or a cache.

## In practice

### Runnable: an Order aggregate with value objects, events and a repository

```python
from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Money:  # value object
    amount: Decimal
    currency: str

    def __post_init__(self):
        if len(self.currency) != 3:
            raise ValueError("ISO 4217 currency code expected")
        if self.amount.as_tuple().exponent < -2:
            raise ValueError("at most two decimal places")

    def __add__(self, other: Money) -> Money:
        if other.currency != self.currency:
            raise ValueError("currency mismatch")
        return Money(self.amount + other.amount, self.currency)

    def times(self, n: int) -> Money:
        return Money(self.amount * n, self.currency)


@dataclass(frozen=True)
class OrderLine:  # value object inside the aggregate
    sku: str
    quantity: int
    unit_price: Money


@dataclass(frozen=True)
class OrderPlaced:  # domain event
    order_id: str
    customer_id: str
    total: Money


class Order:  # aggregate root, an entity
    MAX_LINES = 50

    def __init__(self, order_id: str, customer_id: str, currency: str):
        self.id, self.customer_id, self.currency = order_id, customer_id, currency
        self.status = "draft"
        self.lines: list[OrderLine] = []
        self.version = 0
        self.events: list[object] = []

    def __eq__(self, other):  # identity, not attributes
        return isinstance(other, Order) and other.id == self.id

    def __hash__(self):
        return hash(self.id)

    @property
    def total(self) -> Money:
        total = Money(Decimal("0"), self.currency)
        for line in self.lines:
            total = total + line.unit_price.times(line.quantity)
        return total

    def add_line(self, sku: str, quantity: int, unit_price: Money) -> None:
        self._require("draft")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if unit_price.currency != self.currency:
            raise ValueError("line currency differs from order currency")
        if len(self.lines) >= self.MAX_LINES:
            raise ValueError("too many lines")
        self.lines.append(OrderLine(sku, quantity, unit_price))

    def place(self) -> None:
        self._require("draft")
        if not self.lines:
            raise ValueError("cannot place an empty order")
        self.status = "placed"
        self.events.append(OrderPlaced(self.id, self.customer_id, self.total))

    def _require(self, status: str) -> None:
        if self.status != status:
            raise ValueError(f"order {self.id} is {self.status}, not {status}")


class ConcurrencyError(Exception):
    pass


class InMemoryOrderRepository:
    """Stores copies, like a database would, and checks the version on save."""

    def __init__(self):
        self._rows: dict[str, Order] = {}
        self.outbox: list[object] = []

    def get(self, order_id: str) -> Order:
        return copy.deepcopy(self._rows[order_id])

    def save(self, order: Order) -> None:
        stored = self._rows.get(order.id)
        if stored is not None and stored.version != order.version:
            raise ConcurrencyError(order.id)
        order.version += 1
        self.outbox.extend(order.events)  # same "transaction" as the state
        order.events = []
        self._rows[order.id] = copy.deepcopy(order)


def usd(amount: str) -> Money:
    return Money(Decimal(amount), "USD")


if __name__ == "__main__":
    assert usd("1.0") == usd("1.00")  # value equality
    for bad in (lambda: usd("1.001"), lambda: usd("1") + Money(Decimal("1"), "EUR")):
        try:
            bad()
            raise AssertionError("expected ValueError")
        except ValueError:
            pass

    repo = InMemoryOrderRepository()
    order = Order("o1", "c1", "USD")
    order.add_line("SKU-1", 2, usd("9.99"))
    repo.save(order)

    a, b = repo.get("o1"), repo.get("o1")
    assert a == b and a is not b  # same entity, different objects
    a.add_line("SKU-2", 1, usd("5.00"))
    a.place()
    repo.save(a)
    assert repo.outbox == [OrderPlaced("o1", "c1", usd("24.98"))]

    b.add_line("SKU-3", 1, usd("1.00"))  # stale copy, edited concurrently
    try:
        repo.save(b)
        raise AssertionError("expected ConcurrencyError")
    except ConcurrencyError:
        pass

    placed = repo.get("o1")
    try:
        placed.add_line("SKU-4", 1, usd("1.00"))
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert placed.status == "placed" and placed.total == usd("24.98")
    print("aggregate invariants, events and optimistic locking: ok")
```

### Optimistic locking in Django

Illustrative. Saving the aggregate is a conditional update on the root's version, and the children and outbox rows go in the same transaction:

```python
# orders/adapters/django_repo.py (illustrative)
from django.db import transaction
from django.db.models import F


class StaleOrder(Exception):
    pass


def save_order(order) -> None:
    with transaction.atomic():
        updated = OrderRow.objects.filter(
            public_id=order.id, version=order.version
        ).update(status=order.status, version=F("version") + 1)
        if updated == 0:
            raise StaleOrder(order.id)  # someone saved first: reload and retry
        replace_line_rows(order)  # children in the same transaction
        OutboxEvent.objects.bulk_create(to_outbox_rows(order.events))
```

### An anticorruption layer

Illustrative. Billing consumes customer data from a legacy CRM and translates it at the edge, so the rest of billing never sees the CRM's model:

```python
# billing/acl/crm.py (illustrative)
from dataclasses import dataclass


@dataclass(frozen=True)
class BillingContact:  # billing's model, in billing's language
    customer_id: str
    invoice_email: str
    country: str


def contact_from_crm(record: dict) -> BillingContact:
    account = record["ACCOUNT"]
    return BillingContact(
        customer_id=account["EXT_REF"],
        invoice_email=account["BILL_EMAIL_1"].strip().lower(),
        country=(account.get("CTRY_CD") or "US").upper(),
    )
```

## Pitfalls

- **Tactical cargo cult.** Repositories and factories for a CRUD settings page, while context boundaries are never discussed. Most of the value is strategic.
- **Anemic aggregates.** Getters, setters, and every invariant enforced in a service, if at all. Rules belong on the aggregate that owns the data.
- **Giant aggregates.** A `Tenant` aggregate holding every user, project and invoice means slow loads, constant version conflicts and wide locks. Split it and reference by ID.
- **Object references across aggregates.** `order.customer.orders.all()` loads half the database and invites multi-aggregate transactions. Hold IDs.
- **One shared model across contexts.** The 80-column `Product` every team edits. Give each context its own model keyed by the shared ID.
- **Domain events as public contracts.** Other teams couple to your internals and every refactor breaks them. Translate to integration events.
- **Dispatching events before commit.** Handlers email customers about orders that rolled back. Dispatch after commit, through an outbox when events cross a process.
- **Language drift.** Code names diverge from what the business says. Rename in code when the language changes.

## Interview questions

**Entity vs value object?**
An entity has identity and a lifecycle, and equality is by ID (an order, a customer). A value object has no identity, is immutable, and equality is by value (money, an address, a date range). Prefer value objects wherever identity doesn't matter.

**What is an aggregate and how big should it be?**
A cluster of objects changed as a unit through one root, which is the consistency boundary for invariants that must hold immediately. Keep it as small as the true invariants allow, reference other aggregates by ID, and use eventual consistency between them.

**How does a bounded context relate to a microservice?**
A context is a model and language boundary. It's the best first cut for a service or module. One context can be several services, but a service shouldn't span contexts. Start contexts as modules in a monolith and extract them later if needed.

**Domain event vs integration event?**
A domain event is internal to a context and can change freely. An integration event is a versioned public contract published to other contexts, usually through an outbox, translated from domain events.

**How do you enforce a rule that spans aggregates, like unique emails or not overselling stock?**
A database unique constraint for uniqueness. For scarce resources, model the resource as its own aggregate with a reserve operation, or accept eventual consistency with a compensating action. Don't build one giant aggregate.

**When would you use an anticorruption layer?**
When integrating with a legacy system, a vendor API or another team's model that you don't want leaking into your core domain. The ACL translates at the boundary, so upstream changes hit one adapter instead of your whole context.
## Further reading

- Eric Evans, *Domain-Driven Design: Tackling Complexity in the Heart of Software* (Addison-Wesley, 2003).
- Vaughn Vernon, *Implementing Domain-Driven Design* (Addison-Wesley, 2013).
- Vaughn Vernon, "Effective Aggregate Design" (three-part essay, 2011).
- Alberto Brandolini, *Introducing EventStorming* (Leanpub).
- Harry Percival and Bob Gregory, *Architecture Patterns with Python* (O'Reilly, 2020).
