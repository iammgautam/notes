---
title: Caching in Django
section: python-django
order: 14
summary: Per-view, fragment and low-level caching in Django, building tenant-safe cache keys, invalidation strategies, and the stampede and leak bugs to avoid.
---

Caching is the fastest way to cut database load and the fastest way to show one tenant's data to another. In a multi-tenant SaaS the hard parts aren't the API calls; they're choosing what is safe to cache, building keys that always include the tenant and the user's visibility scope, and invalidating without a thundering herd. Fix N+1s and indexes first; cache what remains expensive.

## How it works

### Backends

```python
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",   # 4.0+
        "LOCATION": "redis://redis:6379/1",
        "KEY_PREFIX": "app",
        "VERSION": 1,
        "TIMEOUT": 300,
    }
}
```

| Backend | Shared across processes | Notes |
|---|---|---|
| `RedisCache` | Yes | Built in since Django 4.0; `django-redis` adds extras (locks, pattern delete) |
| `PyMemcacheCache` | Yes | Simple, no persistence |
| `LocMemCache` | **No**, per process | Each Gunicorn worker has its own copy; invalidation doesn't reach the others |
| `DatabaseCache` | Yes | Extra DB load; rarely worth it |
| `DummyCache` | - | For tests or disabling |

### Keys

The final key is built by `KEY_FUNCTION` (default `"<KEY_PREFIX>:<VERSION>:<key>"`). Memcached limits keys to 250 characters without spaces; hash long or user-supplied parts.

### Layers Django gives you

- **Per-site middleware** (`UpdateCacheMiddleware` + `FetchFromCacheMiddleware`): caches every anonymous-safe GET. Rarely right for an authenticated SaaS app.
- **Per-view**: `@cache_page(timeout)`.
- **Template fragment**: the `cache` template tag.
- **Low-level API**: `cache.get/set/add/delete/get_or_set/incr/get_many/set_many`.
- **In-process memoization**: `functools.lru_cache`, `cached_property` (per instance).

## In practice

### Per-view caching and the tenant trap

`cache_page` builds the key from the full URL (including host) plus the headers listed in the response's `Vary`. If tenants are separated by subdomain, the host keeps them apart. If the tenant comes from a header, JWT claim, or session, it does **not**, unless you vary on it.

```python
from django.views.decorators.cache import cache_page
from django.views.decorators.vary import vary_on_headers

@cache_page(60)
@vary_on_headers("X-Tenant-ID", "Authorization")
def public_pricing(request):
    ...
```

Session-based views that touch `request.user` get `Vary: Cookie` automatically, so each session gets its own entry, which mostly defeats the cache. In practice, per-view caching suits public or tenant-wide-identical pages. For authenticated APIs, use the low-level API with explicit keys.

### Low-level caching with tenant-scoped keys

```python
from django.core.cache import cache

def tenant_key(tenant_id: int, *parts: object) -> str:
    return ":".join(["t", str(tenant_id), *map(str, parts)])

def dashboard_stats(tenant_id: int) -> dict:
    key = tenant_key(tenant_id, "dashboard", "v3")
    return cache.get_or_set(key, lambda: compute_stats(tenant_id), timeout=300)
```

Rules for keys:

- Always include the tenant ID, built by one helper so nobody forgets.
- Include anything that changes the result: user role or permission scope, locale, currency, query params (normalized and hashed).
- Include a schema version (`v3`) so a deploy that changes the payload shape doesn't read old entries.

### Invalidation strategies

**1. TTL only.** Accept staleness up to N seconds. Simplest; right for dashboards and counts.

**2. Delete on write.**

```python
from django.db import transaction

def update_plan(tenant, plan):
    with transaction.atomic():
        tenant.plan = plan
        tenant.save(update_fields=["plan"])
        transaction.on_commit(lambda: cache.delete(tenant_key(tenant.id, "plan")))
```

Delete **after commit**. Deleting before commit lets a concurrent read repopulate the cache with the old value from the database.

**3. Generation (version) keys.** Deleting "everything for tenant 7" is hard without pattern scans. Instead keep a counter per tenant and embed it in every key:

```python
def tenant_gen(tenant_id: int) -> int:
    return cache.get_or_set(f"t:{tenant_id}:gen", 1, timeout=None)

def bump_tenant_gen(tenant_id: int) -> None:
    try:
        cache.incr(f"t:{tenant_id}:gen")
    except ValueError:                  # key missing
        cache.set(f"t:{tenant_id}:gen", 2, timeout=None)

def tenant_key(tenant_id, *parts):
    return ":".join(["t", str(tenant_id), f"g{tenant_gen(tenant_id)}", *map(str, parts)])
```

Bumping the generation orphans all old keys at once; they expire via TTL or LRU eviction. Costs one extra cache read per lookup (batch it with `get_many` if hot).

### Fragment caching

{% raw %}
```text
{% load cache %}
{% cache 300 invoice_sidebar request.tenant.id request.user.role %}
  ... expensive sidebar ...
{% endcache %}
```
{% endraw %}

The extra arguments become part of the key. Same tenant rule applies.

### Stampede protection

When a hot key expires, 200 concurrent requests all miss and all recompute. Options:

```python
def get_with_lock(key, compute, timeout=300, lock_timeout=30):
    value = cache.get(key)
    if value is not None:
        return value
    if cache.add(f"{key}:lock", 1, lock_timeout):   # atomic: only one wins
        try:
            value = compute()
            cache.set(key, value, timeout)
            return value
        finally:
            cache.delete(f"{key}:lock")
    return compute()     # losers recompute; serve a stale copy if this hurts
```

Other tools: jittered TTLs (`300 + random.randint(0, 60)`) so keys don't expire together, and refreshing hot keys in a Celery beat job before they expire.

### In-process caching

`lru_cache` is per process and never invalidated by other processes. Use it only for data that's immutable for the process lifetime (parsed config, compiled regexes), never for tenant data that can change.

## Pitfalls

- **Cross-tenant leaks.** A key missing the tenant ID returns tenant A's data to tenant B. Centralize key building and test it with two tenants.
- **Caching permissions-dependent output.** Admin and viewer roles see different fields. Include the role or permission scope in the key.
- **`LocMemCache` in production.** Invalidations only affect one worker. Symptom: "sometimes stale, sometimes fresh" depending on which worker serves the request.
- **Caching `None`.** `cache.get` returns `None` for misses; a legitimately `None` value is recomputed every time. Use a sentinel default: `cache.get(key, MISSING)`.
- **Caching querysets.** Pickling evaluates them; you store instances that go stale and may be large. Cache plain dicts or lists of IDs.
- **Redis as cache and broker on one instance.** An eviction policy like `allkeys-lru` that suits the cache can evict Celery messages. Separate instances or DBs with appropriate `maxmemory-policy`.
- **Unbounded keys.** Keys built from free-form query params explode cardinality. Normalize and hash, and always set a timeout.

## Checklist

- Fix queries first; cache what is still expensive.
- One key helper that always includes tenant, scope and version.
- Invalidate on commit; prefer generation keys for "everything for tenant X".
- TTL on everything, with jitter for hot keys.
- Shared backend (Redis) in production, never `LocMemCache` for shared data.
- Test cache behavior with two tenants and two roles.

## Interview angle

**How do you avoid cross-tenant cache leaks?** Build every key through one function that requires the tenant ID (and role/scope), and test with multiple tenants.

**How do you invalidate all cache entries for a tenant?** Generation keys: embed a per-tenant counter in every key and increment it; old entries age out.

**What's a cache stampede and how do you prevent it?** Many concurrent misses recomputing the same value. Use a lock via atomic `add`, jittered TTLs, or background refresh.

**Why delete the cache after commit, not before?** A concurrent reader can repopulate it from the old committed row between your delete and your commit.
