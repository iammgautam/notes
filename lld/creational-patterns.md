---
title: Creational patterns
section: lld
order: 6
phase: "Phase 3 · Design patterns"
summary: Thread-safe singleton, factory method, abstract factory, builder and prototype in Python, with when each pays off and when it is overkill.
---

Creational patterns decide **who creates an object and how**, so callers depend on an interface instead of a concrete class or a 12-argument constructor.

| Pattern | Problem it solves | Python-first answer |
|---|---|---|
| Singleton | One shared instance per process | A module-level instance |
| Factory method | "Which class do I build?" logic scattered everywhere | A registry `dict[Enum, type]` |
| Abstract factory | Related objects must come from the same family | One factory class per family |
| Builder | Many optional fields, cross-field validation | `@dataclass` with defaults, builder if built in steps |
| Prototype | Building from scratch is expensive or the setup is the value | `copy.deepcopy` + a registry |

## Singleton

Exactly one instance, shared: config, metrics registry, logger setup.

### The Pythonic answer: a module-level instance

```python
# settings.py
class _Settings:
    def __init__(self) -> None:
        self.values: dict[str, str] = {"env": "prod"}


settings = _Settings()   # runs once, on first import
```

A module body runs once per process, and the import system holds a per-module lock while it runs, so this is thread-safe with zero code. Everyone does `from settings import settings`.

### Lazy and thread-safe: double-checked locking

Use this when creation is expensive and should happen on first use. Java does this with `synchronized` and a `volatile` field; in Python it is a `threading.Lock`.

```python
import threading
from concurrent.futures import ThreadPoolExecutor


class ConfigStore:
    _instance: "ConfigStore | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.values = {"env": "prod"}      # imagine a slow file or network load

    @classmethod
    def instance(cls) -> "ConfigStore":
        if cls._instance is None:          # fast path: no lock once created
            with cls._lock:
                if cls._instance is None:  # another thread may have won the race
                    cls._instance = cls()
        return cls._instance


with ThreadPoolExecutor(max_workers=32) as pool:
    ids = set(pool.map(lambda _: id(ConfigStore.instance()), range(2_000)))
assert len(ids) == 1
```

- **Why the second check:** two threads both see `None` on the fast path. One takes the lock and creates; without the inner check the second would create again once it gets the lock.
- **The GIL does not save you:** check-then-act is several bytecodes, and a thread switch between them yields two instances. Free-threaded builds (3.13t+) have no GIL at all.
- Reading `_instance` outside the lock is safe in Python: a reference assignment is never seen half-written, so nothing like `volatile` is needed.

### Why `__new__`-based singletons are fragile

```python
class Registry:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        self.items: list[str] = []         # runs on EVERY Registry() call


a = Registry()
a.items.append("x")
b = Registry()
assert a is b and a.items == []            # same object, state silently wiped


class Child(Registry): ...


assert type(Child()) is Registry           # subclass gets the parent instance
```

- `__init__` re-runs on every call, so state resets unless you add an "already initialized" guard.
- `Registry()` looks like normal construction; readers assume a fresh object.
- No lock, so it races exactly like the unguarded check above.

**Use when** the thing is truly process-wide and read-mostly. **Avoid when** you need to swap it in tests, it holds per-request state, or it is really a pool (see [connection pool]({{ '/lld/connection-pool/' | relative_url }})). Deeper treatment: [Thread-safe singleton]({{ '/lld/thread-safe-singleton/' | relative_url }}).

> **Interview tip:** Write the double-checked version, explain the second check, then say you would create one instance at startup and inject it. Singletons are hidden global state.

## Factory method

Problem: `if kind == "car": Car() elif ...` copied across call sites; every new type edits all of them.

```python
from abc import ABC, abstractmethod
from enum import Enum


class VehicleType(Enum):
    CAR = "car"
    BIKE = "bike"


class Vehicle(ABC):
    @abstractmethod
    def wheels(self) -> int: ...


class Car(Vehicle):
    def wheels(self) -> int:
        return 4


class Bike(Vehicle):
    def wheels(self) -> int:
        return 2


class VehicleFactory:
    _registry: dict[VehicleType, type[Vehicle]] = {
        VehicleType.CAR: Car,
        VehicleType.BIKE: Bike,
    }

    @classmethod
    def register(cls, kind: VehicleType, klass: type[Vehicle]) -> None:
        cls._registry[kind] = klass        # new type: register, don't edit create()

    @classmethod
    def create(cls, kind: VehicleType) -> Vehicle:
        try:
            return cls._registry[kind]()
        except KeyError:
            raise ValueError(f"unknown vehicle type: {kind}") from None


assert VehicleFactory.create(VehicleType("bike")).wheels() == 2
```

- Strict GoF "factory method" is a base-class method like `create_channel()` that subclasses override. In interviews, the registry above ("simple factory") is what people usually mean.
- **Use when** the concrete class depends on input (request field, config) and is created in more than one place. **Avoid when** there is one product or the caller already knows the class: just call it.

## Abstract factory

Problem: several related objects must match. A Stripe client with a Razorpay webhook verifier is a bug.

```python
from abc import ABC, abstractmethod


class PaymentClient(ABC):
    @abstractmethod
    def charge(self, cents: int) -> str: ...


class WebhookVerifier(ABC):
    @abstractmethod
    def verify(self, signature: str) -> bool: ...


class StripeClient(PaymentClient):
    def charge(self, cents: int) -> str:
        return f"stripe:{cents}"


class StripeVerifier(WebhookVerifier):
    def verify(self, signature: str) -> bool:
        return signature.startswith("whsec_")


class PaymentProvider(ABC):                # the abstract factory
    @abstractmethod
    def client(self) -> PaymentClient: ...

    @abstractmethod
    def verifier(self) -> WebhookVerifier: ...


class StripeProvider(PaymentProvider):     # one concrete factory per family
    def client(self) -> PaymentClient:
        return StripeClient()

    def verifier(self) -> WebhookVerifier:
        return StripeVerifier()


def checkout(provider: PaymentProvider, cents: int) -> str:
    return provider.client().charge(cents)


assert checkout(StripeProvider(), 500) == "stripe:500"
```

Adding Razorpay means one new family (`RazorpayClient`, `RazorpayVerifier`, `RazorpayProvider`) and zero edits to `checkout`. **Avoid when** each family has a single product: that is just a factory method.

## Builder

Problem: config-heavy objects. Positional constructors with 8 optional arguments are unreadable and easy to get wrong.

Python's first answer is a dataclass with keyword-only defaults:

```python
from dataclasses import dataclass, field


@dataclass(frozen=True, kw_only=True)
class HttpClientConfig:
    base_url: str
    timeout_s: float = 5.0
    retries: int = 3
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:        # cross-field validation lives here
        if self.retries < 0:
            raise ValueError("retries must be >= 0")


cfg = HttpClientConfig(base_url="https://api.example.com", retries=5)
```

Reach for a fluent builder when the object is **assembled in steps**, often conditionally across code, and validated once at the end. Django's `QuerySet` chaining is this idea.

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Query:
    table: str
    filters: tuple[str, ...]
    order_by: str | None
    limit: int | None


class QueryBuilder:
    def __init__(self, table: str) -> None:
        self._table = table
        self._filters: list[str] = []
        self._order_by: str | None = None
        self._limit: int | None = None

    def where(self, condition: str) -> "QueryBuilder":
        self._filters.append(condition)
        return self                         # returning self enables chaining

    def order_by(self, column: str) -> "QueryBuilder":
        self._order_by = column
        return self

    def limit(self, n: int) -> "QueryBuilder":
        self._limit = n
        return self

    def build(self) -> Query:
        if self._limit is not None and self._order_by is None:
            raise ValueError("LIMIT without ORDER BY returns arbitrary rows")
        return Query(self._table, tuple(self._filters), self._order_by, self._limit)


qb = QueryBuilder("orders").where("status = %s")
show_recent = True
if show_recent:
    qb = qb.order_by("created_at").limit(20)
assert qb.build().limit == 20
```

| Situation | Dataclass with defaults | Fluent builder |
|---|---|---|
| All values known at one call site | Yes | Overkill |
| Built up in steps or conditionally | Awkward | Yes |
| Cross-field validation | `__post_init__` | `build()` |
| Immutable result | `frozen=True` | `build()` returns a frozen object |

## Prototype

Problem: building from scratch is expensive (parsed, loaded, computed), or the preconfigured setup is the valuable part, as with templates users save at runtime.

```python
import copy
from dataclasses import dataclass, field


@dataclass
class EmailTemplate:
    subject: str
    body: str
    tags: list[str] = field(default_factory=list)

    def clone(self, **changes: str) -> "EmailTemplate":
        twin = copy.deepcopy(self)          # nested lists are copied too
        for name, value in changes.items():
            setattr(twin, name, value)
        return twin


class TemplateRegistry:                     # registry of prototypes
    def __init__(self) -> None:
        self._protos: dict[str, EmailTemplate] = {}

    def register(self, key: str, proto: EmailTemplate) -> None:
        self._protos[key] = proto

    def create(self, key: str, **changes: str) -> EmailTemplate:
        return self._protos[key].clone(**changes)


reg = TemplateRegistry()
reg.register("welcome", EmailTemplate("Welcome", "Hi there", ["onboarding"]))
mail = reg.create("welcome", subject="Welcome, Asha")
mail.tags.append("vip")
assert reg.create("welcome").tags == ["onboarding"]   # prototype untouched
```

- `copy.copy` and `dataclasses.replace` are shallow: clones share `tags`, and mutating one mutates all.
- **Avoid when** the object holds locks, sockets or file handles. `copy.deepcopy` of a `threading.Lock` raises `TypeError`.

## Pitfalls

- Singleton for things that are not single (DB connections need a pool).
- A factory with one product, or an abstract factory with one family. YAGNI.
- A builder for a 3-field object.
- Lazy singletons without a lock in a threaded server (gunicorn threads, `ThreadPoolExecutor`).

## Interview questions

- **Does the GIL make a lazy singleton thread-safe?** No. Check-then-act spans several bytecodes; a switch between them creates two instances. Use a lock or a module-level instance.
- **Singleton vs dependency injection?** Same single instance, but injection makes the dependency visible and swappable in tests. Prefer creating once at startup and passing it in.
- **Factory vs abstract factory?** A factory builds one product from an input; an abstract factory builds a matching set of products.
- **Builder vs keyword arguments?** Keyword arguments with defaults cover most Python cases. A builder earns its place when construction is incremental or needs validation across steps.
- **Shallow vs deep copy?** Shallow shares nested objects; deep copies recursively and fails on unpicklable resources.
