---
title: Creational patterns
section: lld
order: 4
summary: Singleton (with thread safety), factory method, abstract factory, builder and prototype, with when to use each and a Python sketch.
---

Creational patterns control **how objects are created** so callers don't hard-code concrete classes or juggle complex construction.

| Pattern | Use when | LLD example |
|---------|----------|-------------|
| Singleton | Exactly one shared instance | Logger, config, parking lot |
| Factory method | Pick a concrete class from input | `VehicleFactory.create("car")` |
| Abstract factory | Create families of related objects | UI kit per OS, DB driver set |
| Builder | Many optional params, step-by-step build | HTTP request, pizza order, query |
| Prototype | Cloning is cheaper than building | Game entities, document templates |

## Singleton

One instance, global access point.

```python
import threading


class Logger:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:              # fast path, no lock
            with cls._lock:
                if cls._instance is None:      # double-checked locking
                    cls._instance = super().__new__(cls)
        return cls._instance
```

- Double-checked locking: without the inner check, two threads that both saw `None` would each create an instance.
- `__init__` still runs on every `Logger()` call; guard init or do setup in `__new__`.
- **Pythonic alternative:** a module-level instance (`logger = Logger()` in `logging_setup.py`). Modules are imported once and import is guarded by a lock.

> **Gotcha:** Singletons are global state: hard to test, hide dependencies. Prefer creating one instance at startup and injecting it. Say this in the interview.

## Factory method

Centralize "which class do I instantiate?" logic.

```python
from enum import Enum


class VehicleType(Enum):
    CAR = "car"
    BIKE = "bike"


class Vehicle: ...
class Car(Vehicle): ...
class Bike(Vehicle): ...


class VehicleFactory:
    _registry = {VehicleType.CAR: Car, VehicleType.BIKE: Bike}

    @classmethod
    def create(cls, kind: VehicleType) -> Vehicle:
        return cls._registry[kind]()
```

- A registry dict beats `if/elif` and supports OCP: register a new type, don't edit the method.
- GoF "factory method" strictly means a subclass overrides a creation method (`create_transport()` in `RoadLogistics` vs `SeaLogistics`). In interviews, the simple factory above is what people usually mean.

## Abstract factory

A factory of factories: guarantees products from the same family go together.

```python
from abc import ABC, abstractmethod


class Button(ABC): ...
class Checkbox(ABC): ...
class MacButton(Button): ...
class MacCheckbox(Checkbox): ...
class WinButton(Button): ...
class WinCheckbox(Checkbox): ...


class UIFactory(ABC):
    @abstractmethod
    def button(self) -> Button: ...
    @abstractmethod
    def checkbox(self) -> Checkbox: ...


class MacFactory(UIFactory):
    def button(self): return MacButton()
    def checkbox(self): return MacCheckbox()


class WinFactory(UIFactory):
    def button(self): return WinButton()
    def checkbox(self): return WinCheckbox()
```

Use when mixing families would be a bug (Mac button with Windows checkbox).

## Builder

Construct complex objects step by step; avoid telescoping constructors.

```python
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: dict = field(default_factory=dict)
    body: bytes | None = None
    timeout: float = 5.0


class RequestBuilder:
    def __init__(self, url: str):
        self._url, self._method = url, "GET"
        self._headers: dict = {}
        self._body, self._timeout = None, 5.0

    def method(self, m: str):
        self._method = m
        return self                      # fluent chaining

    def header(self, k: str, v: str):
        self._headers[k] = v
        return self

    def body(self, b: bytes):
        self._body = b
        return self

    def build(self) -> HttpRequest:
        if self._method == "GET" and self._body:
            raise ValueError("GET with body")   # validate once, at build
        return HttpRequest(self._method, self._url, self._headers,
                           self._body, self._timeout)


req = RequestBuilder("/api").method("POST").header("X-Id", "1").body(b"{}").build()
```

> **Rule of thumb:** In Python, keyword arguments with defaults cover most builder use cases. Reach for a builder when construction has ordering, validation across fields, or produces an immutable object.

## Prototype

Create new objects by copying an existing one.

```python
import copy


class Document:
    def __init__(self, title, sections):
        self.title, self.sections = title, sections

    def clone(self) -> "Document":
        return copy.deepcopy(self)


template = Document("Invoice", [{"h": "Bill to"}])
inv = template.clone()
inv.title = "Invoice #42"
```

- Shallow vs deep copy matters: `copy.copy` shares nested lists; mutating one mutates all clones.
- A prototype registry (`dict[str, Document]`) gives "clone by name".

## Pitfalls

- Singleton for things that aren't truly single (DB connections: use a pool).
- Factories with one product. YAGNI.
- Builders for 2-field objects.
- Forgetting thread safety on lazy singletons in multi-threaded servers.

> **Interview tip:** Singleton is the most-asked pattern. Be ready to write the double-checked version, explain why the second check exists, and then argue for dependency injection instead.
