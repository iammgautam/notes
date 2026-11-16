---
title: Blue-green, canary and rolling deployments
section: backend
order: 66
phase: "Phase 9 · DevOps and observability"
summary: Rolling, blue-green and canary deploys with automated analysis, schema changes during deploys, feature flags, and how to roll back safely.
---

Most outages start with a change. A deployment strategy decides how many users a bad version can hurt before you notice (blast radius) and how quickly you can undo it (time to restore). The strategies differ in how traffic moves from the old version to the new one, how much spare capacity they need, and whether two versions run side by side. That last point is the hidden constraint: any strategy that avoids downtime runs old and new code at the same time against the same database, queues and caches, so every change must be compatible in both directions.

```text
 Rolling (one pool, pods replaced in batches)
   v1 v1 v1 v1 v1 v1  ->  v2 v2 v1 v1 v1 v1  ->  v2 v2 v2 v2 v1 v1 -> ...

 Blue-green (two full pools, router flips all traffic at once)
                 +--> blue  (v1) x10   idle after switch, kept for rollback
   router -------+
                 +--> green (v2) x10   gets 100% after smoke tests pass

 Canary (weighted split, automated analysis between steps)
                 +--> stable (v1)  95% -> 75% -> 50% ->   0%
   router -------+
                 +--> canary (v2)   5% -> 25% -> 50% -> 100%
                          |
                          +-- compare with baseline: errors, p99, saturation
```

| | Rolling | Blue-green | Canary |
|---|---|---|---|
| Extra capacity | Small (`maxSurge`) | 2x during the switch | Small to moderate |
| Mixed versions serving | Yes, during the rollout | No (except brief drain) | Yes, by design |
| Blast radius of a bad version | Grows as the rollout proceeds | 100% at switch | The canary weight (1–5% at first) |
| Rollback speed | Minutes (another rollout) | Seconds (flip back) | Seconds (weight to 0) |
| Needs | Readiness probes | Router you can flip, warm standby | Weighted routing, good metrics, analysis |
| Default for | Most stateless services | Infrequent, high-risk releases; non-Kubernetes stacks | High-traffic, user-facing services |

## Rolling deployments

A rolling deployment replaces instances in batches inside one pool. In Kubernetes it is the Deployment default: a new ReplicaSet scales up while the old one scales down, bounded by two settings:

- `maxSurge`: how many pods above the desired count may exist. Percentages round **up**.
- `maxUnavailable`: how many pods below the desired count may be unavailable. Percentages round **down**.

With 10 replicas and the defaults (25% and 25%), surge is `ceil(2.5) = 3` and unavailable is `floor(2.5) = 2`, so the rollout keeps between 8 and 13 pods and needs headroom for 3 extra. Common settings:

| Setting | Behavior | Use when |
|---|---|---|
| `maxSurge: 25%`, `maxUnavailable: 0` | Never below full capacity; needs spare room | Services running near capacity |
| `maxSurge: 0`, `maxUnavailable: 1` | No extra pods, capacity dips by one | Tight quotas, licensed or very large pods |
| `maxSurge: 100%`, `maxUnavailable: 0` | Full new set first, then old removed | Fast, close to blue-green within one Deployment |

New pods count only after the readiness probe passes and `minReadySeconds` elapses, so a broken version stalls the rollout instead of replacing healthy pods. A stalled rollout is marked failed after `progressDeadlineSeconds` (600 seconds by default) but **not** rolled back automatically; that's your pipeline's job. The `Recreate` strategy kills all old pods before starting new ones, which means downtime but guarantees no version overlap, for the rare case where two versions must never coexist.

Weakness: no traffic control. Once 50% of pods are new, 50% of requests hit the new code, whether or not anyone has looked at a metric.

## Blue-green deployment

Run two complete production environments. Blue serves traffic; green gets the new version, is warmed up and smoke-tested with production configuration, and then the router switches all traffic to green. Blue stays up, idle, as the rollback target until you're confident.

Where the switch happens:

- **Load balancer**: swap the target group behind an ALB listener, or the backend pool in nginx or HAProxy. Fast and atomic.
- **Kubernetes Service selector**: change `version: blue` to `version: green`. Argo Rollouts automates this with an active and a preview Service.
- **DNS**: slow and unreliable for rollback, since resolvers and clients cache past the TTL.

Trade-offs:

- **Cost**: double capacity during the release. In the cloud that is only for the release window if green is created on demand.
- **All or nothing**: 100% of users move at once, so a bug that smoke tests missed hits everyone. Pair it with a short canary phase or strong smoke tests.
- **Shared state isn't blue-green.** Both colors use the same database, cache and queues, so schema and message formats must work for both. Background workers need care: green's Celery workers start consuming as soon as they start, before the HTTP switch.
- **Connections**: long-lived connections (WebSockets, gRPC streams) stay on blue until they close; drain them deliberately.

It shines where rollback must be instant and the platform doesn't do fine-grained traffic splitting, such as VM fleets behind a load balancer or a monolith with infrequent releases.

## Canary deployment

A canary sends a small share of real traffic to the new version, measures it, and increases the share step by step, or aborts.

### Traffic splitting

| Method | Precision | Notes |
|---|---|---|
| Replica ratio (1 canary pod of 20) | Coarse, tied to pod count | Works with plain Services; 5% needs 20 pods |
| Weighted routing at ingress or gateway | Exact percentages | Gateway API `HTTPRoute` weights, ingress-nginx canary annotations, ALB weighted target groups |
| Service mesh | Exact, per request, plus header matching | Istio or Linkerd; also gives per-version metrics |
| Header, cookie or user cohort | Targeted | Internal users or opted-in tenants first |
| Sticky by user or tenant hash | Consistent experience | A user shouldn't flip between versions per request |

In a multi-tenant SaaS, canarying by tenant cohort is often better than by request: internal tenant first, then a few low-risk tenants, then everyone, and a tenant always sees one version. That needs routing on a tenant header or subdomain (see [multi-tenant architecture]({{ '/backend/multi-tenant-architecture/' | relative_url }})).

### Automated canary analysis

Humans watching dashboards miss things and get bored. Automate the judgment:

- **Compare against a baseline, not against all of production.** The stable fleet has warm caches, long-running pods and a different size; a freshly started baseline pod running the old version, receiving the same traffic share as the canary, gives an apples-to-apples comparison. Netflix and Google publicly described this approach in Kayenta, which uses a Mann-Whitney U test per metric.
- **Metrics**: error rate (5xx, and business errors such as payment declines), latency percentiles, saturation (CPU, memory, worker utilization), and a business KPI where possible (checkout completions).
- **Statistics**: require a minimum sample size before judging, and test whether a difference is significant rather than comparing raw numbers.
- **Steps**: 1–5% for 10 minutes, 25%, 50%, 100%, with analysis at each step and automatic abort, which sets the canary weight back to 0.

A runnable judge: a one-sided two-proportion z-test on error rate, a p99 ratio check, and a minimum sample size.

```python
import math
from statistics import NormalDist, quantiles

MIN_REQUESTS = 500           # below this, the comparison is noise
MAX_P99_RATIO = 1.20         # canary p99 may be at most 20% worse
ALPHA = 0.01                 # significance for "error rate is higher"


def error_rate_worse(base_err, base_n, can_err, can_n):
    """One-sided two-proportion z-test: is the canary's error rate higher?"""
    p_pool = (base_err + can_err) / (base_n + can_n)
    if p_pool in (0.0, 1.0):
        return False
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / base_n + 1 / can_n))
    z = (can_err / can_n - base_err / base_n) / se
    return 1 - NormalDist().cdf(z) < ALPHA


def p99(latencies_ms):
    return quantiles(latencies_ms, n=100)[98]


def judge(baseline, canary):
    """baseline/canary: dicts with errors, requests, latencies_ms."""
    if canary["requests"] < MIN_REQUESTS or baseline["requests"] < MIN_REQUESTS:
        return "INCONCLUSIVE"
    if error_rate_worse(baseline["errors"], baseline["requests"],
                        canary["errors"], canary["requests"]):
        return "FAIL"
    if p99(canary["latencies_ms"]) > MAX_P99_RATIO * p99(baseline["latencies_ms"]):
        return "FAIL"
    return "PASS"


def sample(n, errors, base_ms, slow_every=0):
    lat = [base_ms + (i % 50) for i in range(n)]
    if slow_every:
        lat = [x * 3 if i % slow_every == 0 else x for i, x in enumerate(lat)]
    return dict(requests=n, errors=errors, latencies_ms=lat)


if __name__ == "__main__":
    baseline = sample(5000, 25, 80)                 # 0.5% errors
    assert judge(baseline, sample(5000, 30, 80)) == "PASS"     # 0.6%: noise
    assert judge(baseline, sample(5000, 90, 80)) == "FAIL"     # 1.8%: real
    assert judge(baseline, sample(5000, 25, 80, slow_every=20)) == "FAIL"
    assert judge(baseline, sample(200, 50, 80)) == "INCONCLUSIVE"
    print("canary judge ok")
```

In Kubernetes, Argo Rollouts and Flagger implement the steps, traffic shifting and Prometheus-backed analysis; see the manifest under "In practice".

## Database changes during deploys

Code rolls back in a minute; a dropped column doesn't come back. During any zero-downtime rollout, old and new code run against one schema, so each deploy must satisfy two rules:

1. The new schema works with the **old** code (old pods are still serving while and after migrations run).
2. The new code works with the **old** schema until migrations finish, or migrations run strictly before the rollout.

The technique is **expand and contract** (parallel change). Renaming `customer_name` to `full_name` on a busy table takes four releases, not one:

| Release | Schema | Code |
|---|---|---|
| 1. Expand | Add nullable `full_name` | Write both columns, read the old one |
| (job) | Backfill `full_name` in batches | |
| 2. Migrate reads | | Read `full_name`, still write both |
| 3. Contract code | Make `customer_name` nullable; remove it from the Django model only | Write only `full_name` |
| 4. Contract schema | `DROP COLUMN customer_name` | |

Each release is independently rollback-safe to the one before it. Illustrative Django migrations for steps 3 and 4:

```python
# release 3: Django forgets the field; the column stays (it must be nullable)
class Migration(migrations.Migration):
    dependencies = [("orders", "0041_customer_name_nullable")]
    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.RemoveField("order", "customer_name")],
            database_operations=[],
        ),
    ]


# release 4: no running code references the column, drop it
class Migration(migrations.Migration):
    dependencies = [("orders", "0042_remove_customer_name_state")]
    operations = [
        migrations.RunSQL(
            "ALTER TABLE orders_order DROP COLUMN customer_name",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
```

The same thinking applies beyond the database: message schemas on Kafka or Celery task signatures (new consumers must accept old messages; new producers must not emit what old consumers can't parse), cache entry formats (version the cache key), and API responses consumed by mobile clients you can't redeploy. Lock-safe DDL, `lock_timeout` and batched backfills are covered in [zero-downtime migrations]({{ '/postgresql/zero-downtime-migrations/' | relative_url }}) and [database migrations]({{ '/backend/database-migrations/' | relative_url }}).

## Feature flags

A feature flag is a runtime switch around a code path. Flags **decouple deploy from release**: code ships dark, and turning it on is a configuration change you can make for 1% of users, one tenant, or internal staff, and undo in seconds without a deploy.

| Flag type | Lifetime | Example |
|---|---|---|
| Release toggle | Days to weeks, then delete | New invoicing flow behind `new-invoicing` |
| Experiment | Weeks | A/B test of a pricing page |
| Ops toggle / kill switch | Long-lived | Disable expensive recommendations under load |
| Permission or entitlement | Permanent | Feature available on the enterprise plan |

How they work: the service keeps the flag rules in memory, refreshed by streaming or polling from a flag service (LaunchDarkly, Unleash, Flagsmith, or a table in your own database), and evaluates locally per request, so a flag check costs microseconds and survives the flag service being down. OpenFeature (a CNCF project) standardizes the SDK interface across vendors. Percentage rollouts must be **deterministic**: hash the flag name and the user or tenant ID into a bucket, so the same tenant gets the same answer on every request and raising 10% to 25% only adds tenants. Runnable:

```python
import hashlib


def bucket(flag, subject_id):
    """Stable bucket 0..9999 for a (flag, subject) pair."""
    digest = hashlib.sha256(f"{flag}:{subject_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 10_000


def is_enabled(flag, subject_id, percent, allow=(), deny=()):
    if subject_id in deny:
        return False
    if subject_id in allow:
        return True
    return bucket(flag, subject_id) < percent * 100   # percent in 0..100


if __name__ == "__main__":
    tenants = [f"tenant-{i}" for i in range(20_000)]
    on_10 = {t for t in tenants if is_enabled("new-invoicing", t, 10)}
    on_25 = {t for t in tenants if is_enabled("new-invoicing", t, 25)}
    assert 0.09 < len(on_10) / len(tenants) < 0.11
    assert on_10 <= on_25          # raising the percentage only adds tenants
    # another flag buckets independently, so the same tenants aren't always first
    other = {t for t in tenants if is_enabled("new-search", t, 10)}
    assert len(on_10 & other) < len(on_10) * 0.2
    assert is_enabled("new-invoicing", "tenant-internal", 0, allow={"tenant-internal"})
    assert not is_enabled("new-invoicing", "tenant-1", 100, deny={"tenant-1"})
    print("flags ok")
```

The cost is flag debt: every flag doubles a code path's states. Give release flags an owner and an expiry, test both branches, and delete the flag and the dead branch once rolled out. Knight Capital's 2012 loss of about $440 million in 45 minutes is the classic warning: a repurposed flag reactivated obsolete code on one server that a manual deploy had missed.

## Rollback

Decide before the release how you'll undo it, and prefer the fastest lever that works:

| Lever | Speed | Undoes |
|---|---|---|
| Turn off the feature flag | Seconds | The feature, not the deploy |
| Canary weight to 0 / blue-green flip back | Seconds | The new version's traffic |
| Redeploy the previous image digest (`kubectl rollout undo`, revert in GitOps) | Minutes | Code and config |
| Roll forward with a fix | Tens of minutes | Anything, but under pressure |
| Restore data | Hours | Data corruption (see [disaster recovery and backups]({{ '/backend/disaster-recovery-and-backups/' | relative_url }})) |

What a code rollback can't undo: schema changes that old code doesn't understand, rows written in a new format, messages already published with a new schema, emails sent and payments captured. That's why rule 1 above exists: if every release is compatible with its predecessor, rollback is always just "run the previous artifact". Don't run down-migrations automatically in a rollback; they're rarely tested and often destroy data.

Automate the trigger: abort the canary on failed analysis, and roll back when the SLO burn rate spikes right after a deploy (see [alerting and incidents]({{ '/backend/alerting-and-incidents/' | relative_url }})). Annotate deploys on dashboards so "what changed?" takes one glance.

## In practice

An illustrative rolling update tuned for zero capacity loss:

```yaml
spec:
  minReadySeconds: 10
  progressDeadlineSeconds: 600
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 25%
      maxUnavailable: 0
```

An illustrative Argo Rollouts canary with ingress-nginx traffic splitting and a Prometheus analysis that aborts when the canary's 5xx ratio exceeds 1% twice:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: orders-api
spec:
  replicas: 10
  selector:
    matchLabels: {app: orders-api}
  template:
    metadata:
      labels: {app: orders-api}
    spec:
      containers:
        - name: api
          image: ghcr.io/acme/orders-api@sha256:9d2f7a...
  strategy:
    canary:
      stableService: orders-api-stable
      canaryService: orders-api-canary
      trafficRouting:
        nginx:
          stableIngress: orders-api
      analysis:
        templates:
          - templateName: error-ratio
        startingStep: 1
      steps:
        - setWeight: 5
        - pause: {duration: 10m}
        - setWeight: 25
        - pause: {duration: 10m}
        - setWeight: 50
        - pause: {duration: 10m}
---
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: error-ratio
spec:
  metrics:
    - name: error-ratio
      interval: 1m
      failureLimit: 2
      successCondition: result[0] < 0.01
      provider:
        prometheus:
          address: http://prometheus.monitoring:9090
          query: |
            sum(rate(http_requests_total{service="orders-api-canary",code=~"5.."}[2m]))
            /
            sum(rate(http_requests_total{service="orders-api-canary"}[2m]))
```

## Pitfalls

- **Breaking schema change in one release.** Symptom: errors from old pods mid-rollout, failed rollback. Fix: expand and contract across releases.
- **Canary judged on too little traffic.** Symptom: canaries pass at 3 a.m. and fail at noon, or random aborts. Fix: minimum sample sizes, longer steps at low traffic, statistical tests.
- **Canary compared with the whole fleet.** Cold caches make every canary look slow. Fix: compare with a fresh baseline.
- **Background workers ignored.** HTTP traffic is canaried but the new Celery workers consume 100% of the queue immediately. Fix: canary workers separately (a small worker deployment on the new version) or gate behavior behind flags.
- **Readiness probe that always passes.** Rolling updates replace good pods with broken ones at full speed. Fix: readiness reflects the app being able to serve.
- **Blue-green switch via DNS.** Rollback takes as long as the longest client cache. Fix: switch at the load balancer.
- **Flags never removed.** Hundreds of stale flags, untested combinations. Fix: owners, expiry dates, lint for stale flags.
- **Rollback never rehearsed.** Fix: practice redeploying the previous digest; keep it in the registry.

## Interview questions

**Rolling vs blue-green vs canary: how do you choose?** Rolling is the cheap default for stateless services with good readiness checks. Blue-green when instant, total rollback matters and you can afford double capacity briefly. Canary when traffic is high enough to judge a small slice statistically and the blast radius must stay small; it needs weighted routing and good metrics.

**What do `maxSurge` and `maxUnavailable` do?** They bound a rolling update: how many pods may exist above the desired count and how many may be unavailable below it. Surge rounds up, unavailable rounds down; `maxUnavailable: 0` keeps full capacity at the cost of needing spare room.

**How do you deploy a column rename with zero downtime?** Expand and contract: add the new column, dual-write, backfill, switch reads, stop writing the old column, remove it from the ORM state, and drop it in a later release. Each step stays compatible with the previous release.

**How does automated canary analysis decide?** It compares canary metrics with a baseline running the old version under the same traffic share, requires enough samples, applies a statistical test per metric (error rate, latency, saturation), and aborts or promotes per step.

**Feature flag vs canary?** A canary limits exposure of a whole build by traffic share; a flag limits exposure of one feature by user, tenant or percentage, and can be flipped without a deploy. They complement each other: canary the deploy, flag the feature.

**What can't a rollback fix?** Data and side effects: incompatible schema changes, rows written in a new format, published messages, sent emails, captured payments. Design every release to be compatible with the previous one so rollback is just running the old artifact.

**Why is blue-green not enough for a stateful system?** The database, caches and queues are shared between blue and green, so the switch only covers stateless compute. State changes still need backward compatibility, and workers on green may start processing before the traffic flip.

## Further reading

- *Continuous Delivery*, Jez Humble and David Farley (Addison-Wesley, 2010), chapter on deployment and release strategies.
- Martin Fowler's bliki: *BlueGreenDeployment*, *ParallelChange*; Danilo Sato, *CanaryRelease*; Pete Hodgson, *Feature Toggles (aka Feature Flags)*, all on martinfowler.com.
- *The Site Reliability Workbook* (Google, 2018), chapter "Canarying Releases".
- Netflix Technology Blog, *Automated Canary Analysis at Netflix with Kayenta* (2018).
- Argo Rollouts documentation: Canary, BlueGreen and Analysis.
