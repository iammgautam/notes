---
title: Python data model
section: python-django
order: 1
summary: How objects, names, dunder methods, mutability, identity and hashing actually work, and why Django models behave the way they do.
---

Every bug of the form "why did this dict lose my key", "why is this default list shared between requests" or "why can't I put this model in a set" comes from the data model. Django leans on it heavily: model fields are descriptors, `QuerySet` implements `__iter__`, `__len__`, `__bool__` and `__getitem__`, and `Model.__eq__` / `__hash__` decide what happens when you dedupe instances. Knowing the rules lets you predict behavior instead of debugging it.

## How it works

### Everything is an object, names are references

Every value (ints, functions, classes, modules) is a heap object with three things: an **identity** (`id()`, the address in CPython), a **type** (`type()`), and a **value**. Variables are names bound to objects. Assignment never copies.

```python
a = [1, 2]
b = a          # second name, same object
b.append(3)
assert a == [1, 2, 3] and a is b
```

Function arguments are passed the same way ("call by sharing"): the callee gets a reference. Mutating it is visible to the caller; rebinding the parameter name is not.

### Dunder methods are the protocol

Operators and builtins dispatch to special methods looked up **on the type**, not the instance:

| You write | Python calls |
|---|---|
| `len(x)` | `type(x).__len__(x)` |
| `if x:` | `__bool__`, falling back to `__len__` |
| `for i in x` | `__iter__` (or `__getitem__` with 0, 1, 2…) |
| `x[k]` | `__getitem__` |
| `k in x` | `__contains__`, falling back to iteration |
| `x == y` | `__eq__` (then the reflected `y.__eq__(x)`) |
| `hash(x)` | `__hash__` |
| `with x:` | `__enter__` / `__exit__` |
| `x.attr` | `__getattribute__`, then `__getattr__` on miss |

This is why `bool(queryset)` runs a query: `QuerySet.__bool__` evaluates the queryset to see if the result cache is non-empty.

### Attribute lookup and descriptors

`obj.attr` follows a fixed order:

1. **Data descriptors** on the type (objects defining `__get__` and `__set__`, e.g. `property`).
2. The instance `__dict__`.
3. **Non-data descriptors** (only `__get__`, e.g. plain functions, which become bound methods).
4. `__getattr__` if defined.

Django uses this everywhere. `order.customer` is a `ForwardManyToOneDescriptor` that checks a per-instance cache and otherwise runs a query. A deferred field (`.only("id")`) is a `DeferredAttribute` that loads the value on first access. That one attribute access hides a database round trip is the root of most N+1 bugs.

### Mutability

Immutable: `int`, `float`, `str`, `bytes`, `tuple`, `frozenset`, `None`. Mutable: `list`, `dict`, `set`, most user classes. A tuple is only as immutable as its contents; `(1, [])` is not hashable.

### Identity vs equality

- `is` compares identity (same object). Use it for singletons: `None`, `True`, `False`, sentinels.
- `==` compares value via `__eq__`. The default `object.__eq__` falls back to identity.

CPython caches small ints (-5 to 256) and interns some strings, so `x is 256` can be `True` while `x is 257` is `False`. Never rely on it. Since 3.12, `None`, `True`, small ints and some other objects are **immortal** (PEP 683): their refcount is never modified.

### Hashing

A dict or set stores items by `hash(key)` then confirms with `==`. The contract:

- If `a == b`, then `hash(a) == hash(b)`.
- The hash must not change while the object is in a dict or set.

Defining `__eq__` without `__hash__` sets `__hash__ = None`, making instances unhashable. That is deliberate: Python refuses to guess a hash consistent with your equality.

## In practice

### Mutable default arguments

Defaults are evaluated once, at function definition.

```python
def add_line(order_id, lines=[]):       # bug: one list for every call
    lines.append(order_id)
    return lines

def add_line(order_id, lines=None):     # fix
    lines = [] if lines is None else lines
    lines.append(order_id)
    return lines
```

The same trap exists at class level: a `tags = []` class attribute is shared by every instance. In Django, `JSONField(default={})` is caught by a system check (`fields.E010`) for exactly this reason. Pass a callable: `default=dict`.

### Value objects with dataclasses

```python
from dataclasses import dataclass
from decimal import Decimal

@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    currency: str

    def __add__(self, other: "Money") -> "Money":
        if other.currency != self.currency:
            raise ValueError("currency mismatch")
        return Money(self.amount + other.amount, self.currency)

totals = {Money(Decimal("10"), "INR")}   # hashable because frozen
```

`@dataclass` generates `__eq__`; with `eq=True` and `frozen=False` it sets `__hash__ = None`. `frozen=True` gives you a field-based `__hash__`. `slots=True` drops the per-instance `__dict__`, cutting memory (useful when you hold 100k of these in a worker).

### How Django models compare and hash

`Model.__eq__` returns `True` when both instances have the same concrete model and the same non-`None` primary key. `Model.__hash__` is `hash(pk)` and raises `TypeError` if `pk is None`.

```python
o1 = Order.objects.get(pk=42)
o2 = Order.objects.get(pk=42)
assert o1 == o2 and o1 is not o2   # equal, different objects

{Order(tenant=t)}                  # TypeError: unhashable before save
```

Two consequences: deduping fetched instances with a set works, and mutating `o1` does not touch `o2`. Each query builds fresh instances, so stale copies are easy to create. Call `refresh_from_db()` when you need the latest row.

### Making your own collection

If you wrap a list of invoices, implement the protocol rather than inventing methods:

```python
class InvoiceBatch:
    def __init__(self, invoices):
        self._items = list(invoices)

    def __len__(self):
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    def __contains__(self, invoice):
        return invoice in self._items

    def __repr__(self):
        return f"InvoiceBatch(n={len(self._items)})"
```

Now `len(batch)`, `for inv in batch`, `inv in batch` and `if batch:` all work.

## Pitfalls

- **Mutating a dict key.** Putting a mutable object with a value-based hash in a set, then mutating it, makes it unfindable. Symptom: `x in s` is `False` right after `s.add(x)`. Use frozen dataclasses or tuples as keys.
- **`__eq__` that returns `False` for other types.** Return `NotImplemented` so Python tries the reflected operation. Returning `False` breaks symmetric comparisons.
- **Shared state at class level.** Class attributes that are lists or dicts are shared across instances, and in a long-running Gunicorn or Celery worker, across requests and tenants.
- **Copy confusion.** `list(x)` and `copy.copy` are shallow. Nested JSON from a `JSONField` that you "copy" and then edit can mutate the original. Use `copy.deepcopy` for nested structures.
- **`is` on strings or numbers.** Works in the REPL, fails in production. Linters (`F632`) catch this.
- **Truthiness that hits the DB.** `if qs:` evaluates the whole queryset. Use `qs.exists()` when you only need a yes/no and won't iterate.

## Checklist

- Use `is` only for `None`, booleans and sentinels.
- Never use a mutable default. Use `None` or `default=dict` / `default=list` on Django fields.
- Value objects: `@dataclass(frozen=True, slots=True)`.
- Override `__eq__` and `__hash__` together, using the same fields.
- Return `NotImplemented` from binary dunders for unsupported types.
- Remember that attribute access on a model can be a query.

## Interview angle

**Why are lists unhashable?** Their value can change, which would change the hash and break the dict invariant. Tuples of hashables are hashable.

**What's the difference between `is` and `==`?** Identity versus `__eq__`. `is` is for singletons; small-int and string caching makes `is` on values appear to work, which is an implementation detail.

**Why does defining `__eq__` make my class unhashable?** Python sets `__hash__` to `None` so you can't accidentally break "equal objects have equal hashes". Define `__hash__` from the same fields.

**How does `order.customer` trigger a query?** It's a data descriptor on the class. Its `__get__` checks the instance's field cache and, on a miss, runs `SELECT ... WHERE id = %s`.
