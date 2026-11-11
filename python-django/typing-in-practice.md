---
title: Type hints in practice
section: python-django
order: 5
summary: Protocols, TypedDict, generics and modern 3.12 syntax, plus how to run mypy or pyright on a Django and DRF codebase without drowning in errors.
---

Type hints don't make Python faster and aren't enforced at runtime. What they buy you in a large Django codebase is refactoring safety: rename a field on `Invoice`, change a service function to return `Decimal | None`, or split a module, and the checker finds every caller in seconds. The payoff is highest at boundaries: service functions, task signatures, external API payloads and settings.

## How it works

- Annotations are stored on the object (`func.__annotations__`) and mostly ignored by the interpreter. Since 3.14 they are evaluated lazily (PEP 649), so forward references no longer need quotes.
- A **static checker** (mypy, pyright, or newer Rust-based ones) reads source, infers types, and reports mismatches. It never runs your code.
- **Gradual typing**: unannotated code is `Any`, which is compatible with everything. You can type one module at a time.
- **Nominal vs structural**: classes match by inheritance (nominal). `Protocol` matches by shape (structural, i.e. static duck typing).

### Modern syntax you should use (3.12+)

```python
def first[T](items: list[T]) -> T | None:          # PEP 695 generics
    return items[0] if items else None

type TenantId = int                                # PEP 695 alias
type JSON = dict[str, JSON] | list[JSON] | str | int | float | bool | None

from typing import Self, override

class Repo[ModelT]:
    def with_tenant(self, tenant_id: TenantId) -> Self: ...
```

`X | None` instead of `Optional[X]`, built-in generics (`list[int]`, `dict[str, int]`) instead of `typing.List`, `@override` (3.12) to catch methods that no longer override anything after a base class rename.

## In practice

### Protocols for pluggable dependencies

You route payments or LLM calls to different providers. Instead of a base class every adapter must inherit, describe the shape:

```python
from decimal import Decimal
from typing import Protocol

class PaymentGateway(Protocol):
    def charge(self, *, tenant_id: int, amount: Decimal, ref: str) -> str: ...
    def refund(self, *, charge_id: str) -> None: ...

class StripeGateway:                      # no inheritance needed
    def charge(self, *, tenant_id: int, amount: Decimal, ref: str) -> str: ...
    def refund(self, *, charge_id: str) -> None: ...

def settle_invoice(invoice: "Invoice", gateway: PaymentGateway) -> str:
    return gateway.charge(
        tenant_id=invoice.tenant_id, amount=invoice.total, ref=invoice.number
    )
```

Tests pass a small fake class with the same two methods; the checker verifies it matches. Add `@runtime_checkable` only if you need `isinstance` checks, and know it checks method names, not signatures.

### TypedDict for JSON payloads

Webhooks, `JSONField` contents and third-party API responses are dicts. `TypedDict` documents and checks the keys without changing runtime behavior:

```python
from typing import NotRequired, ReadOnly, TypedDict

class InvoiceWebhook(TypedDict):
    event: str
    invoice_id: int
    tenant_id: ReadOnly[int]                  # 3.13+
    amount_cents: int
    metadata: NotRequired[dict[str, str]]

def handle(payload: InvoiceWebhook) -> None:
    if payload["event"] == "invoice.paid":
        mark_paid(payload["invoice_id"])
    payload.get("metadata")                    # fine, NotRequired
```

It is **not validation**. A malformed payload still reaches `handle`. Validate at the trust boundary with a DRF serializer or Pydantic, then pass the typed dict or model inward.

`Unpack` types `**kwargs`:

```python
from typing import Unpack

class ExportOptions(TypedDict, total=False):
    include_drafts: bool
    currency: str

def export_invoices(tenant_id: int, **opts: Unpack[ExportOptions]) -> bytes: ...
```

### Generics for reusable services

```python
from django.db import models

class TenantScoped[M: models.Model]:
    def __init__(self, model: type[M]) -> None:
        self.model = model

    def for_tenant(self, tenant_id: int) -> models.QuerySet[M]:
        return self.model._default_manager.filter(tenant_id=tenant_id)

orders = TenantScoped(Order).for_tenant(7)     # QuerySet[Order]
```

`[M: models.Model]` is a bounded type variable.

### Typing Django: django-stubs and the mypy plugin

Django models are built by a metaclass, managers are attached dynamically, and fields are descriptors. Plain type checkers can't see through that. `django-stubs` ships stubs plus a **mypy plugin** that understands models, managers, querysets and settings.

```toml
# pyproject.toml
[tool.mypy]
python_version = "3.12"
plugins = ["mypy_django_plugin.main", "mypy_drf_plugin.main"]
strict = false
warn_unused_ignores = true
disallow_untyped_defs = false

[[tool.mypy.overrides]]
module = ["billing.services.*", "billing.tasks"]
disallow_untyped_defs = true

[tool.django-stubs]
django_settings_module = "config.settings.test"
```

What the plugin gives you:

- `Order.objects.filter(...)` is `QuerySet[Order]`, `.get()` returns `Order`.
- `order.customer` is `Customer`; `order.customer_id` is `int`.
- Nullable fields become `X | None`.
- `.annotate()` results can be typed with `WithAnnotations[Order, TypedDictOfAnnotations]`.

`djangorestframework-stubs` (with `mypy_drf_plugin`) covers DRF. Serializer `validated_data` is still mostly `dict[str, Any]`.

### mypy vs pyright in Django

| | mypy + plugin | pyright / Pylance |
|---|---|---|
| Model fields and managers | Precise | Works with stubs, less precise for related managers and annotations |
| Speed on large repos | Slower, incremental cache helps | Fast |
| Editor integration | Via dmypy or LSP | Excellent (VS Code) |
| Plugin support | Yes | No plugin system |

A common setup: pyright in the editor for fast feedback, mypy with the Django plugin in CI as the gate.

### Rolling it out

1. Add the checker in CI with a permissive config; fix only new errors.
2. Turn on `disallow_untyped_defs` per package, starting with service and task modules.
3. Type the boundaries first: Celery task arguments, service layer, external clients.
4. Keep `# type: ignore[code]` specific and turn on `warn_unused_ignores`.

## Pitfalls

- **`Any` leaking.** One untyped helper returning `Any` silently disables checks downstream. Search for it with `--warn-return-any` or pyright's `reportUnknown*` rules.
- **Treating hints as validation.** `def charge(amount: Decimal)` still accepts a float at runtime. Validate at the edge.
- **Circular imports from annotations.** Importing models for type hints only: put them under `if TYPE_CHECKING:`. On 3.12/3.13 also add `from __future__ import annotations` or quote the names.
- **Celery task signatures.** Tasks receive JSON-serialized arguments. Annotate them with primitives (`int`, `str`), not model instances, which matches what should be sent anyway.
- **Over-precise types on internal code.** Type the boundary, let inference handle the locals.
- **Stubs out of sync.** Pin `django-stubs` to a version matching your Django minor version.

## Checklist

- Use 3.12 syntax: `X | None`, `list[int]`, `def f[T]`, `type Alias = ...`.
- `Protocol` for injected dependencies, `TypedDict` for dict payloads, `dataclass` for internal records.
- mypy + `django-stubs` plugin in CI, gradual strictness per package.
- Validate external input at runtime; types describe, they don't enforce.

## Interview angle

**Protocol vs ABC?** An ABC requires inheritance and can enforce at instantiation. A Protocol matches any class with the right methods, which suits adapters and test fakes.

**TypedDict vs dataclass?** TypedDict types a plain dict (JSON in and out, zero runtime cost). A dataclass is a real class with attributes, methods and equality.

**Why does Django need a mypy plugin?** Models, managers and related fields are created dynamically by a metaclass and descriptors, so the plugin teaches the checker what attributes and querysets exist.
