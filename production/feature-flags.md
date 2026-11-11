---
title: Feature flags
section: production
order: 8
summary: Release, ops and experiment flags and how they differ, per-tenant targeting in a multi-tenant app, evaluation and caching, and paying down flag debt.
---

A feature flag is a runtime decision about which code path to run. Its main value is separating **deploy** (code is on the servers) from **release** (users see it). That lets you merge unfinished work to main, roll out to one tenant before all of them, and turn a bad feature off in seconds without a deploy. The cost is branching logic that accumulates if you don't remove it.

## How it works

### Flag types

| Type | Purpose | Lifetime | Who flips it | Example |
|---|---|---|---|---|
| Release | Hide incomplete or risky work, gradual rollout | Days to weeks | Engineers / PM | `new_report_builder` |
| Ops (kill switch) | Turn off or degrade a feature under load or failure | Permanent | On-call | `disable_pdf_export`, `llm_fallback_only` |
| Experiment | A/B test variants, measure impact | Weeks | Product / data | `onboarding_variant` |
| Permission / entitlement | Gate by plan or contract | Permanent | Billing / config | `sso_enabled` |

Mixing these up is the root of most flag problems. A release flag should die after rollout. An ops flag should live forever and be tested. An entitlement "flag" is really product configuration and belongs in your tenant/plan model, not in a flag service that someone might clean up.

### Evaluation

A flag evaluation takes a **context** (tenant, user, environment, plan, region) and returns a variant:

```text
evaluate("new_report_builder", {tenant: 512, user: 9021, plan: "enterprise"})
  1. kill switch / global override?       -> off
  2. explicit tenant allowlist?           -> on
  3. rule: plan == enterprise             -> on
  4. percentage rollout: hash(tenant) % 100 < 20 -> on
  5. default                              -> off
```

For percentage rollouts, hash a **stable key** (tenant ID, or user ID for per-user features) with the flag name, so the same tenant gets the same answer on every request and different flags bucket independently. Random per request would flip the UI between page loads.

In multi-tenant B2B software, **bucket by tenant, not user**. Half the users in a company seeing a different invoicing screen generates support tickets, not data.

### Where flags live

- **Hosted service** (LaunchDarkly, Unleash, Flagsmith, GrowthBook): SDK keeps rules in memory and receives updates via streaming; evaluation is local and microsecond-fast.
- **Database table + cache**: fine for a handful of flags. Read from Postgres, cache in-process for 30–60 s or invalidate on change.
- **django-waffle**: Django-native flags, switches and samples, with per-user/group targeting. Per-tenant targeting needs a custom check.

## In practice

### A minimal per-tenant flag in Django

```python
# models.py
from django.db import models

class Flag(models.Model):
    name = models.SlugField(unique=True)
    enabled_globally = models.BooleanField(default=False)
    rollout_percent = models.PositiveSmallIntegerField(default=0)
    tenants = models.ManyToManyField("tenants.Tenant", blank=True)
    owner = models.CharField(max_length=100)
    expires_on = models.DateField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
```

```python
# flags.py
import hashlib
from django.core.cache import cache
from .models import Flag

def _bucket(flag_name: str, key: str) -> int:
    digest = hashlib.sha1(f"{flag_name}:{key}".encode()).hexdigest()
    return int(digest[:8], 16) % 100

def _load(name):
    data = cache.get(f"flag:{name}")
    if data is None:
        try:
            f = Flag.objects.prefetch_related("tenants").get(name=name)
            data = (f.enabled_globally, f.rollout_percent,
                    {t.id for t in f.tenants.all()})
        except Flag.DoesNotExist:
            data = (False, 0, set())
        cache.set(f"flag:{name}", data, timeout=60)
    return data

def is_enabled(name: str, tenant_id: int) -> bool:
    globally, percent, tenant_ids = _load(name)
    if globally or tenant_id in tenant_ids:
        return True
    return _bucket(name, str(tenant_id)) < percent
```

Usage keeps the branch narrow:

```python
def report_view(request):
    if is_enabled("new_report_builder", request.tenant.id):
        return new_report_builder(request)
    return legacy_report(request)
```

Evaluate once per request or task and pass the result down, rather than checking the same flag in ten places.

### Flags in background jobs

A task enqueued while the flag was on may run after it was turned off. Decide explicitly:

- Evaluate at **enqueue** time and store the decision in the task payload when the job must be consistent with what the user saw.
- Evaluate at **execution** time for kill switches, so turning it off stops work immediately.

### Ops flags as kill switches

Design them for the incident you'll have:

```python
def generate_answer(question, tenant):
    if is_enabled("llm_primary_disabled", tenant.id):
        return route_to_fallback_model(question)
    return route_to_primary_model(question)
```

Kill switches should default to the safe behavior if the flag service is unreachable, and be exercised periodically so the "off" path doesn't rot.

### Observability

- Log flag decisions that change behavior (`flag=new_report_builder variant=on`) and add them as tags in Sentry. "Errors started at 14:02" is much easier when you can see the flag that flipped at 14:01.
- Keep an audit log: who changed which flag, from what to what, when.

## Flag debt

Every flag doubles the code paths it touches. Ten interacting release flags means configurations nobody has tested. Old flags also confuse readers: is this branch live?

Keep it under control:

- Every flag has an **owner** and an **expiry date** at creation.
- A release flag at 100% for two weeks gets a cleanup ticket automatically.
- CI or a weekly job lists flags past their expiry and flags with no evaluations in 30 days.
- Remove the flag in the code **and** in the flag store, in that order.
- Cap the number of active release flags per team.

## Pitfalls

- **Flag service as a hard dependency.** If evaluation makes a network call per check and the service is down, your app is down. Use local evaluation with cached rules and sensible defaults.
- **Inconsistent bucketing** across services (one hashes user, another tenant), so a tenant sees half a feature.
- **Testing only the "on" path.** Run the test suite with the flag on and off for anything in active rollout.
- **Schema tied to a flag.** Turning the flag off doesn't revert a migration. Make the schema change backward compatible independently of the flag.
- **Entitlements in a release flag tool,** cleaned up by someone who thought the flag was stale, removing a paid feature from customers.
- **Flag checks in hot loops** hitting the cache or DB per row. Evaluate once outside the loop.

## Rules of thumb

- Decide the type (release, ops, experiment, entitlement) when you create the flag.
- Bucket by tenant in B2B; hash stable keys.
- Local evaluation, safe defaults, audit log.
- Owner and expiry on every release flag; delete after full rollout.
- Deploy dark, enable for internal tenant, then one friendly customer, then percentages.

## Interview angle

- **"How would you roll out a risky feature in a multi-tenant SaaS?"** Deploy behind a per-tenant flag, enable for internal and pilot tenants, watch errors and latency tagged by flag, ramp by percentage of tenants, keep a kill switch, then remove the flag.
- **"Downsides of feature flags?"** Combinatorial code paths, stale flags, a new runtime dependency, and confusion between temporary flags and permanent configuration.

Related: [CI/CD and deploy strategies]({{ '/production/ci-cd-and-deploys/' | relative_url }}).
