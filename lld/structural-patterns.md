---
title: Structural patterns
section: lld
order: 5
summary: Adapter, decorator, facade, proxy, composite and bridge, with how to tell the look-alikes apart and a Python sketch of each.
---

Structural patterns are about **composing classes and objects** into larger structures while keeping them flexible.

| Pattern | Intent in one line | LLD example |
|---------|-------------------|-------------|
| Adapter | Make an incompatible interface fit | Wrap a third-party payment SDK |
| Decorator | Add behavior by wrapping, same interface | Pizza toppings, logging/retry wrappers |
| Facade | One simple entry point over a subsystem | `BookingFacade.book()` |
| Proxy | Stand-in that controls access | Lazy load, cache, auth check |
| Composite | Treat tree leaves and branches uniformly | File system, org chart, menus |
| Bridge | Split abstraction and implementation hierarchies | Shape x Renderer, Notification x Channel |

## Adapter

```python
from abc import ABC, abstractmethod


class LegacyPayPal:                      # can't change this
    def make_payment(self, cents: int, email: str) -> bool: ...


class PaymentGateway(ABC):
    @abstractmethod
    def charge(self, user, amount: int) -> bool: ...


class PayPalAdapter(PaymentGateway):
    def __init__(self, client: LegacyPayPal):
        self.client = client

    def charge(self, user, amount: int) -> bool:
        return self.client.make_payment(amount, user.email)
```

Use when integrating external or legacy code behind your own interface.

## Decorator

Wraps an object of the same interface and adds behavior before or after delegating. Stackable.

```python
class Coffee(ABC):
    @abstractmethod
    def cost(self) -> int: ...


class Espresso(Coffee):
    def cost(self) -> int:
        return 100


class AddOn(Coffee):
    def __init__(self, inner: Coffee):
        self.inner = inner


class Milk(AddOn):
    def cost(self) -> int:
        return self.inner.cost() + 20


class Caramel(AddOn):
    def cost(self) -> int:
        return self.inner.cost() + 30


drink = Caramel(Milk(Espresso()))        # 150
```

> **Gotcha:** Python's `@decorator` syntax is function wrapping, closely related but not the same as the GoF object decorator. Mention both; `@retry`, `@lru_cache` are the everyday versions.

## Facade

```python
class BookingFacade:
    def __init__(self, seats, payments, notifier):
        self.seats, self.payments, self.notifier = seats, payments, notifier

    def book(self, user, show, seat_ids):
        self.seats.lock(show, seat_ids, user)
        txn = self.payments.charge(user, self.seats.price(show, seat_ids))
        self.seats.confirm(show, seat_ids, user)
        self.notifier.send(user, f"Booked {seat_ids}")
        return txn
```

Clients call one method; the subsystem stays decoupled. A Django service layer is effectively a facade over models and integrations.

## Proxy

Same interface as the real object, controls access to it.

```python
class Image(ABC):
    @abstractmethod
    def display(self): ...


class RealImage(Image):
    def __init__(self, path):
        self.data = open(path, "rb").read()   # expensive

    def display(self): ...


class LazyImage(Image):                       # virtual proxy
    def __init__(self, path):
        self.path, self._real = path, None

    def display(self):
        if self._real is None:
            self._real = RealImage(self.path)
        return self._real.display()
```

Kinds: virtual (lazy), protection (auth), caching, remote (RPC stub). Django's `SimpleLazyObject` for `request.user` is a virtual proxy.

## Composite

```python
class Node(ABC):
    @abstractmethod
    def size(self) -> int: ...


class File(Node):
    def __init__(self, n: int):
        self.n = n

    def size(self) -> int:
        return self.n


class Folder(Node):
    def __init__(self):
        self.children: list[Node] = []

    def add(self, node: Node):
        self.children.append(node)

    def size(self) -> int:
        return sum(c.size() for c in self.children)
```

Use whenever the problem says "tree" and clients shouldn't care whether they hold a leaf or a group.

## Bridge

Avoid a class explosion when two dimensions vary independently (M x N subclasses become M + N).

```python
class Channel(ABC):                         # implementation hierarchy
    @abstractmethod
    def deliver(self, to: str, text: str): ...


class Email(Channel):
    def deliver(self, to, text): ...


class SMS(Channel):
    def deliver(self, to, text): ...


class Notification(ABC):                    # abstraction hierarchy
    def __init__(self, channel: Channel):
        self.channel = channel


class OTPNotification(Notification):
    def send(self, to, code):
        self.channel.deliver(to, f"OTP: {code}")


class PromoNotification(Notification):
    def send(self, to, offer):
        self.channel.deliver(to, f"Deal: {offer}")
```

## Telling look-alikes apart

| Pair | Difference |
|------|------------|
| Adapter vs facade | Adapter converts one interface; facade simplifies many |
| Decorator vs proxy | Decorator adds behavior, stackable; proxy controls access to one object |
| Adapter vs bridge | Adapter is retrofitted; bridge is designed up front |
| Composite vs decorator | Composite has many children; decorator wraps exactly one |

## Pitfalls

- Deep decorator stacks are hard to debug; order matters.
- A facade that grows business logic turns into a god class.
- Proxies that silently cache stale data.
- Bridge where only one dimension actually varies: plain composition is enough.
