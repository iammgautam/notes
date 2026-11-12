---
title: Structural patterns
section: lld
order: 7
phase: "Phase 3 · Design patterns"
summary: Adapter, decorator (GoF vs Python's @), facade, proxy, composite and bridge in Python, and how to tell the look-alikes apart.
---

Structural patterns **compose objects into larger structures** without hard-wiring them together. Most of them are "wrap one object in another that has the same (or a nicer) interface".

| Pattern | Intent in one line | LLD example |
|---|---|---|
| Adapter | Make an incompatible interface fit yours | Wrap a third-party payment SDK |
| Decorator | Add behavior by wrapping, same interface, stackable | Notifier add-ons, pizza toppings |
| Facade | One simple entry point over a subsystem | `CheckoutFacade.place_order()` |
| Proxy | Stand-in that controls access to one object | Lazy load, cache, permission check |
| Composite | Treat a leaf and a group of leaves the same way | File system, menus, org chart |
| Bridge | Two dimensions vary independently | Notification type x channel |

## Adapter

Problem: a library or legacy class does what you need but with the wrong method names, units or return types. You can't change it, and you don't want its shape leaking into your code.

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal


@dataclass
class User:
    email: str


class PaymentGateway(ABC):                    # the interface your code uses
    @abstractmethod
    def charge(self, user: User, cents: int) -> bool: ...


class LegacyPayPalSDK:                        # third-party: can't change it
    def make_payment(self, amount: Decimal, payer_email: str) -> dict:
        return {"status": "OK", "amount": amount, "payer": payer_email}


class PayPalAdapter(PaymentGateway):
    def __init__(self, sdk: LegacyPayPalSDK) -> None:
        self._sdk = sdk

    def charge(self, user: User, cents: int) -> bool:
        resp = self._sdk.make_payment(Decimal(cents) / 100, user.email)  # in
        return resp["status"] == "OK"                                    # out


assert PayPalAdapter(LegacyPayPalSDK()).charge(User("a@x.com"), 1999)
```

- **Use when** integrating an external or legacy API behind your own interface; the adapter is where unit and error translation lives.
- **Avoid when** you own both sides: change the interface instead.

## Decorator

Problem: add behavior (redaction, retry, logging, extra cost) to some objects without subclassing every combination.

### GoF decorator: wraps an object

```python
from abc import ABC, abstractmethod


class Notifier(ABC):
    @abstractmethod
    def send(self, to: str, msg: str) -> None: ...


class EmailNotifier(Notifier):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, to: str, msg: str) -> None:
        self.sent.append((to, msg))


class NotifierDecorator(Notifier):            # same interface as what it wraps
    def __init__(self, inner: Notifier) -> None:
        self._inner = inner


class Redacting(NotifierDecorator):
    def send(self, to: str, msg: str) -> None:
        self._inner.send(to, msg.replace("password", "********"))


class Branded(NotifierDecorator):
    def __init__(self, inner: Notifier, brand: str) -> None:
        super().__init__(inner)
        self._brand = brand

    def send(self, to: str, msg: str) -> None:
        self._inner.send(to, f"[{self._brand}] {msg}")


email = EmailNotifier()
notifier = Branded(Redacting(email), "ACME")  # composed per object, at runtime
notifier.send("a@x.com", "your password reset")
assert email.sent == [("a@x.com", "[ACME] your ******** reset")]
```

### Python's `@decorator`: wraps a function

```python
import functools
import time


def retry(times: int, delay_s: float = 0.0):
    def wrap(fn):
        @functools.wraps(fn)                  # keep __name__ and docstring
        def inner(*args, **kwargs):
            for attempt in range(1, times + 1):
                try:
                    return fn(*args, **kwargs)
                except ConnectionError:
                    if attempt == times:
                        raise
                    time.sleep(delay_s)
        return inner
    return wrap


calls = 0


@retry(times=3)
def fetch() -> str:
    global calls
    calls += 1
    if calls < 3:
        raise ConnectionError("flaky")
    return "ok"


assert fetch() == "ok" and calls == 3
```

They share a name and the "wrap and delegate" idea, but they are different tools:

| | GoF decorator | Python `@decorator` |
|---|---|---|
| Wraps | An object | A function or class |
| When applied | At runtime, per instance | Once, at definition (import) time |
| Who gets it | Only objects you wrap | Every caller of that function |
| Composition | `Branded(Redacting(x))`, chosen by config | Fixed stack of `@` lines |
| Everyday examples | I/O stream wrappers, toppings | `@retry`, `@lru_cache`, `@login_required` |

`@deco` is just syntax for `fn = deco(fn)`. Say "decorator pattern" for the object version and "function decorator" for the `@` version.

> **Gotcha:** Decorator order matters. `Branded(Redacting(x))` and `Redacting(Branded(x))` differ if the brand contains a redacted word.

## Facade

Problem: callers must coordinate five subsystems in the right order to do one business action.

```python
class Inventory:
    def reserve(self, sku: str, qty: int) -> None: ...


class Payments:
    def charge(self, user_id: int, cents: int) -> str:
        return "txn-1"


class Mailer:
    def send(self, user_id: int, text: str) -> None: ...


class CheckoutFacade:
    def __init__(self, inv: Inventory, pay: Payments, mail: Mailer) -> None:
        self._inventory, self._payments, self._mailer = inv, pay, mail

    def place_order(self, user_id: int, sku: str, qty: int, cents: int) -> str:
        self._inventory.reserve(sku, qty)
        txn = self._payments.charge(user_id, cents)
        self._mailer.send(user_id, f"Order confirmed: {txn}")
        return txn


checkout = CheckoutFacade(Inventory(), Payments(), Mailer())
assert checkout.place_order(user_id=1, sku="sku-9", qty=2, cents=500) == "txn-1"
```

A Django service layer (`services.place_order()`) is a facade over models and integrations. **Avoid** letting it accumulate business rules: then it is a god class.

## Proxy

Problem: control access to an object without callers knowing. Same interface, one subject.

```python
from abc import ABC, abstractmethod


class Reports(ABC):
    @abstractmethod
    def get(self, report_id: int) -> str: ...


class DbReports(Reports):
    def __init__(self) -> None:
        self.queries = 0                      # stand-in for an expensive DB setup

    def get(self, report_id: int) -> str:
        self.queries += 1
        return f"report-{report_id}"


class LazyReports(Reports):                   # virtual proxy: build on first use
    def __init__(self) -> None:
        self._real: DbReports | None = None

    def get(self, report_id: int) -> str:
        if self._real is None:
            self._real = DbReports()
        return self._real.get(report_id)


class CachingReports(Reports):                # caching proxy
    def __init__(self, real: Reports) -> None:
        self._real = real
        self._cache: dict[int, str] = {}      # unbounded: use an LRU/TTL cache

    def get(self, report_id: int) -> str:
        if report_id not in self._cache:
            self._cache[report_id] = self._real.get(report_id)
        return self._cache[report_id]


class GuardedReports(Reports):                # protection proxy
    def __init__(self, real: Reports, role: str) -> None:
        self._real, self._role = real, role

    def get(self, report_id: int) -> str:
        if self._role != "admin":
            raise PermissionError("admins only")
        return self._real.get(report_id)


db = DbReports()
service = GuardedReports(CachingReports(db), role="admin")
service.get(1)
service.get(1)
assert db.queries == 1
assert LazyReports().get(7) == "report-7"
```

- Django's `request.user` is a `SimpleLazyObject`: a virtual proxy. An RPC client stub is a remote proxy.
- A real caching proxy needs bounds and expiry: [LRU cache]({{ '/lld/lru-cache/' | relative_url }}), [TTL cache]({{ '/lld/ttl-cache/' | relative_url }}).

## Composite

Problem: a tree where clients should not care whether they hold one item or a group. Files and folders, menus and submenus, an org chart.

```python
from abc import ABC, abstractmethod


class Node(ABC):
    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    def size(self) -> int: ...

    def render(self, depth: int = 0) -> list[str]:
        return ["  " * depth + self.name]


class File(Node):                             # leaf
    def __init__(self, name: str, size: int) -> None:
        super().__init__(name)
        self._size = size

    def size(self) -> int:
        return self._size


class Folder(Node):                           # composite: holds Nodes
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.children: list[Node] = []

    def add(self, node: Node) -> "Folder":
        self.children.append(node)
        return self

    def size(self) -> int:
        return sum(child.size() for child in self.children)

    def render(self, depth: int = 0) -> list[str]:
        lines = super().render(depth)
        for child in self.children:
            lines += child.render(depth + 1)
        return lines


src = Folder("src").add(File("main.py", 32))
root = Folder("/").add(File("a.txt", 10)).add(src)
assert root.size() == 42
assert root.render() == ["/", "  a.txt", "  src", "    main.py"]
```

A menu is the same shape: `MenuItem` is the leaf, `Menu` holds items and submenus, and `price()` or `render()` recurses. Full case study: [In-memory file system]({{ '/lld/in-memory-file-system/' | relative_url }}).

## Bridge

Problem: two things vary independently. Subclassing both gives M x N classes (`EmailOtp`, `SmsOtp`, `EmailPromo`, ...); a bridge gives M + N by **holding** one dimension instead of inheriting it.

```python
from abc import ABC, abstractmethod


class Channel(ABC):                           # implementation side
    @abstractmethod
    def deliver(self, to: str, text: str) -> str: ...


class Sms(Channel):
    def deliver(self, to: str, text: str) -> str:
        return f"sms to {to}: {text}"


class OtpMessage:                             # abstraction side
    def __init__(self, channel: Channel) -> None:
        self.channel = channel

    def send(self, to: str, code: str) -> str:
        return self.channel.deliver(to, f"Your code is {code}")


assert OtpMessage(Sms()).send("+9198", "4821") == "sms to +9198: Your code is 4821"
```

If only one dimension actually varies, this is plain composition; don't name it.

## Telling look-alikes apart

| Pair | Difference |
|---|---|
| Adapter vs facade | Adapter converts one interface; facade simplifies many |
| Decorator vs proxy | Decorator adds behavior and stacks; proxy controls access to one subject |
| Adapter vs bridge | Adapter is retrofitted; bridge is designed up front |
| Composite vs decorator | Composite has many children; decorator wraps exactly one |

## Pitfalls

- Deep wrapper stacks: stack traces become ten frames of `send()`.
- A caching proxy with no eviction or invalidation serves stale data and grows forever.
- A facade that owns business logic instead of coordinating.
- Using `@decorator` when the behavior must differ per object or be chosen at runtime.

## Interview questions

- **Decorator vs inheritance?** Inheritance fixes behavior at class-definition time and explodes with combinations; decorators combine at runtime, one class per add-on.
- **Is Python's `@` the decorator pattern?** Related, not the same: it wraps a function once at definition, for every caller. The pattern wraps individual objects at runtime.
- **Proxy vs decorator, if the code looks identical?** Intent. A proxy decides whether and when the real call happens (lazy, cached, denied). A decorator always delegates and adds something.
- **Where does a facade sit in a Django app?** The service layer: views call one function, which coordinates models, payment clients and tasks.
