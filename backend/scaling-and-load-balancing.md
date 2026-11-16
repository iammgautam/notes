---
title: Scaling and load balancing
section: backend
order: 33
phase: "Phase 5 · Distributed systems"
summary: Vertical vs horizontal scaling, stateless services, autoscaling, L4 vs L7 balancers, algorithms incl. power of two choices, health checks, GSLB and anycast.
---

A single Django process on one VM serves a few hundred requests per second at best. Getting to tens of thousands means two decisions: make the box bigger or add more boxes, and, once you have more boxes, decide which one serves each request. The first decision is mostly about where your state lives. The second is a stack of load balancers, from DNS down to a proxy picking one pod, each with its own failure modes.

## Horizontal vs vertical scaling

### Vertical scaling

Vertical scaling (scale up) means a bigger machine: more cores, more RAM, faster disks. It needs no code changes and keeps everything in one process and one database, so transactions, joins and in-memory caches keep working.

Its limits:

- **A ceiling.** The largest cloud instances top out at a few hundred vCPUs and a few TB to tens of TB of RAM (as of 2025–2026). After that there is nothing to buy.
- **Cost curve.** Price per core grows faster than linearly near the top end, and you pay for peak capacity 24/7.
- **One failure domain.** One machine, one AZ, one kernel. A hardware fault or a bad deploy takes everything down.
- **Resize means restart.** Moving to a bigger instance type usually means a failover or downtime window.
- **Diminishing returns.** Lock contention, a single-threaded event loop, or a hot row stop benefiting from more cores well before the machine is saturated.

### Horizontal scaling

Horizontal scaling (scale out) means more machines behind a load balancer. Capacity grows roughly linearly while the work is independent, and losing one node out of twenty costs 5% of capacity rather than all of it.

Its limits:

- **State must move out of the process.** Anything a request leaves behind in memory (sessions, uploads in `/tmp`, in-process caches, rate-limit counters) breaks once the next request lands elsewhere.
- **Coordination costs.** Amdahl's law caps speedup by the serial fraction; the Universal Scalability Law adds a coherency term, so throughput can actually *fall* past some node count when nodes must agree with each other (shared locks, cache invalidation, consensus).
- **The data tier does not scale out for free.** Stateless app servers are easy; the database behind them becomes the bottleneck. Scaling it means read replicas, caching, then [partitioning and sharding]({{ '/backend/partitioning-and-sharding/' | relative_url }}).
- **Tail latency.** A request that fans out to 100 backends is as slow as the slowest one. More nodes means more chances of hitting a slow one.

| | Vertical | Horizontal |
|---|---|---|
| Code changes | None | Stateless app, external state |
| Ceiling | Largest instance | Coordination and data tier |
| Failure blast radius | Everything | One node's share |
| Cost shape | Superlinear near the top | Roughly linear, elastic |
| Good for | Primary database, early stage | Web/API tier, workers, caches |

> **Rule of thumb:** scale the stateless tier horizontally from day one (it costs almost nothing) and scale the primary database vertically for as long as you can. Sharding is the most expensive move in this note.

### Stateless services and session externalization

A service is stateless when any instance can serve any request because everything that must survive between requests lives elsewhere:

| State | Where it goes |
|---|---|
| Login sessions | Redis, the database, or a signed token (JWT / signed cookie) |
| Uploaded files | Object storage (S3, GCS), never local disk |
| Caches | Redis/Memcached; local caches only as a short-TTL layer |
| Rate-limit counters | Redis or the gateway |
| Background work | A queue (Celery broker, Postgres queue, SQS) |
| WebSocket connections | Inherently sticky; route by user and use pub/sub for fan-out |

Session externalization is the classic case. Django's default database-backed sessions are already external; in-memory or file-based sessions are not. Moving sessions to Redis or using signed cookies is what lets you kill any pod at any time. The trade-offs between server-side sessions and tokens are in [authentication and sessions]({{ '/backend/authentication-and-sessions/' | relative_url }}).

### Autoscaling

Autoscaling adjusts the instance count to a metric. The Kubernetes HPA computes `desired = ceil(current * currentMetric / targetMetric)` and clamps it to min/max replicas.

| Signal | Good for | Watch out |
|---|---|---|
| CPU utilization | CPU-bound APIs | I/O-bound services sit at 10% CPU while saturated |
| Requests per second per pod | Web tiers with stable cost per request | Needs a metrics adapter |
| Queue depth / lag | Workers, consumers | Scale on age of oldest message, not just count |
| Concurrency / in-flight | Async services | Needs app instrumentation |
| Schedule | Known daily peaks | Drifts when traffic patterns change |

What breaks autoscaling in practice:

- **Lag.** Metric scrape, decision, pod scheduling, image pull, app boot and readiness add up to one to several minutes. Autoscaling absorbs ramps, not spikes; keep headroom (target 60–70% CPU, not 90%) and a sane `minReplicas`.
- **Flapping.** Scale-down without a stabilization window oscillates. Scale up fast, scale down slowly.
- **The downstream does not scale.** Doubling pods doubles database connections. Pair autoscaling with [connection pooling]({{ '/backend/connection-pooling/' | relative_url }}) and caps.
- **Cold starts.** New pods with empty caches and unwarmed connection pools are slower; use readiness probes and slow-start in the balancer.

## Load balancing

A load balancer spreads requests across healthy backends, hides backend churn from clients, and is the place where TLS, retries and routing rules live. Large systems stack several layers.

```text
               clients
                  |
       DNS / GSLB: picks a region (geo, latency, health)
                  |
      anycast VIP: BGP routes to the nearest edge PoP
                  |
       +----------+----------+
       |  L4 LB (TCP/UDP)    |  ECMP + flow hash, no payload parsing
       +----------+----------+
                  |
       +----------+----------+
       |  L7 proxy (HTTP)    |  TLS, host/path routing, retries
       +----+-----+-----+----+
            |     |     |
           app   app   app     stateless, gated by readiness
            \     |     /
       Redis (sessions, cache)    Postgres (primary + replicas)
```

### L4 vs L7

| | L4 (transport) | L7 (application) |
|---|---|---|
| Sees | IPs, ports, TCP/UDP | HTTP method, host, path, headers, cookies |
| Balances | Connections (flows) | Individual requests |
| TLS | Passes through (or terminates) | Terminates, can re-encrypt |
| Routing | By address and port | By host, path, header, weight |
| Features | Very high throughput, low latency | Retries, timeouts, rewrites, auth, canaries |
| Examples | AWS NLB, Google Maglev, IPVS, HAProxy in TCP mode | AWS ALB, nginx, Envoy, HAProxy in HTTP mode |

The big practical difference: an L4 balancer pins a whole connection to one backend. With HTTP/2 or gRPC, a client multiplexes thousands of requests over one long-lived connection, so L4 balancing sends all of them to one pod while the rest sit idle. Use an L7 proxy or client-side balancing for gRPC (see [gRPC and SOAP]({{ '/backend/grpc-and-soap/' | relative_url }})).

### Algorithms

| Algorithm | How it picks | Use when | Weakness |
|---|---|---|---|
| Round robin | Next in order | Identical backends, uniform requests | Ignores slow or loaded nodes |
| Weighted round robin | Proportional to weight | Mixed instance sizes, canaries | Static weights |
| Random | Uniform random | Many independent balancers | Gap above mean grows with load |
| Least connections | Fewest open connections | Long or variable request durations | Needs accurate, shared counts |
| Least response time / EWMA | Lowest latency-weighted score | Heterogeneous backends | Can herd onto a node that fails fast |
| Power of two choices | Sample 2 at random, take the less loaded | Many balancers with stale views | Slightly worse than perfect least-loaded |
| Hash (IP, header, URI) | Consistent hash of a key | Cache affinity, sharded backends | Hot keys overload one node |

### Power of two choices

Least connections sounds optimal but assumes every balancer knows the true load right now. In reality there are many balancer instances, each with its own counters or a snapshot refreshed periodically. When they all see the same "least loaded" server they all send to it at once, and it gets crushed: the herd effect.

Power of two choices (P2C) picks two backends at random and sends to the less loaded of the two. The theory (Azar et al. for balls into bins; Mitzenmacher for queueing) is striking: with `n` requests on `n` servers, random placement gives the busiest server about `log n / log log n` requests, while two random choices cut that to about `log log n / log 2`. When requests far outnumber servers, the random gap above the mean keeps growing with load while the two-choice gap stays at that small constant. Going from one choice to two is an exponential improvement; going from two to three adds little. Because the choice is randomized, stale data does not cause herding. Envoy's least-request balancer uses P2C by default, and nginx exposes it as `random two least_conn`.

A runnable simulation. Each balancer decision uses either a fresh view of load or a snapshot refreshed every 50 requests:

```python
import random


def simulate(pick, n_servers=100, n_requests=20_000, refresh_every=1, seed=7):
    """Return the worst gap (max load minus mean load) seen during the run.

    refresh_every > 1 models a balancer whose load view is stale: it only
    re-reads server load every `refresh_every` requests.
    """
    rng = random.Random(seed)
    load = [0] * n_servers
    view = list(load)
    worst = 0.0
    for i in range(n_requests):
        if i % refresh_every == 0:
            view = list(load)
        s = pick(rng, view)
        load[s] += 1
        if refresh_every == 1:
            view[s] += 1
        worst = max(worst, max(load) - (i + 1) / n_servers)
    return worst


def pick_random(rng, view):
    return rng.randrange(len(view))


def pick_two(rng, view):
    a, b = rng.randrange(len(view)), rng.randrange(len(view))
    return a if view[a] <= view[b] else b


def pick_least(rng, view):
    return min(range(len(view)), key=view.__getitem__)


if __name__ == "__main__":
    pickers = (pick_random, pick_two, pick_least)
    fresh = {f.__name__: simulate(f) for f in pickers}
    stale = {f.__name__: simulate(f, refresh_every=50) for f in pickers}
    for name in fresh:
        print(f"{name:12} fresh gap={fresh[name]:6.1f}  stale gap={stale[name]:6.1f}")
    # Perfect information: least-loaded wins, P2C is close behind.
    assert fresh["pick_least"] <= fresh["pick_two"] < fresh["pick_random"] / 4
    # Stale information: least-loaded herds onto one server; P2C does not.
    assert stale["pick_two"] < stale["pick_least"] / 4
    assert stale["pick_two"] < stale["pick_random"] / 2
    print("ok")
```

Typical output: random about 46 above the mean, P2C about 3 (fresh) and 5 (stale), least-loaded 1 with fresh data but about 50 with stale data, because every request in a 50-request window goes to the same "least loaded" server.

### Health checks

The balancer must stop sending traffic to dead or overloaded backends.

- **Active checks.** The balancer probes `GET /healthz` every few seconds; a backend is marked down after N consecutive failures and back up after M successes. Detects dead nodes even with no traffic.
- **Passive checks (outlier detection).** The balancer watches real traffic and ejects a backend after consecutive 5xx or timeouts (nginx `max_fails`/`fail_timeout`, Envoy outlier detection). Reacts to failures that a shallow probe misses.
- **Liveness vs readiness.** Liveness answers "should this process be restarted?" and must not depend on the database. Readiness answers "should this get traffic right now?" and may check critical dependencies, warm-up and draining. Mixing them up causes restart storms when the database blips. Details in [graceful shutdown and health checks]({{ '/backend/graceful-shutdown-and-health-checks/' | relative_url }}).
- **Fail open.** If every backend fails its health check (often because the shared dependency in the check is down), many balancers route to all of them anyway rather than returning 503 for everything. Envoy's panic threshold does this; know what yours does.
- **Slow start.** Ramp traffic gradually to a newly healthy node so cold caches and JIT warm-up do not turn into timeouts.

### Sticky sessions

Session affinity routes all requests from one client to the same backend, using a balancer-issued cookie (AWS ALB, HAProxy `cookie`) or a hash of the client IP or a session cookie.

Use it when the backend holds per-client state that is expensive to move: WebSocket servers, in-memory game or collaboration state, large per-user caches. Avoid it as a substitute for externalizing sessions:

- Load becomes uneven (one heavy tenant pins a node) and least-connections cannot fix it.
- Scaling out does not help existing users; scaling in or a crash logs them out.
- IP hashing breaks behind carrier NAT and corporate proxies, where thousands of users share an IP.

### Global and DNS load balancing

Global server load balancing (GSLB) picks a region before any TCP connection exists, usually through DNS: the authoritative DNS server answers with different IPs based on the resolver's location, measured latency, region health, or weights (Route 53 latency, geolocation, weighted and failover routing; Cloudflare and NS1 offer similar policies).

Limits of DNS-based balancing:

- **Caching.** Resolvers and clients cache answers for the TTL, and some ignore short TTLs. Failover takes minutes, not seconds, for part of the traffic.
- **Coarse location.** The DNS server sees the resolver's IP, not the client's, unless EDNS Client Subnet is used. A user on a public resolver may be mapped to a distant region.
- **No load awareness per request.** DNS steers aggregate traffic; it cannot react to one overloaded backend.

### Anycast

With anycast, many sites announce the same IP prefix over BGP and Internet routing delivers each packet to the topologically nearest site. One IP serves the whole world; if a site withdraws its route, traffic shifts to the next nearest within seconds of BGP convergence, without waiting for DNS TTLs. It also absorbs DDoS by spreading attack traffic across all sites.

Anycast is a natural fit for UDP request/response (DNS root servers and public resolvers use it). For TCP it works because routes are stable for the life of most connections; a route change mid-connection lands packets at a site with no connection state, so long-lived connections can reset. CDNs such as Cloudflare and Google Cloud's global external load balancer publicly describe serving a single anycast IP from many edge locations, then using L4 and L7 layers inside each site.

| | DNS GSLB | Anycast |
|---|---|---|
| Steering unit | Resolver answer | Packet route |
| Failover speed | TTL-bound (minutes) | BGP convergence (seconds) |
| "Nearest" means | Geo/latency database | BGP path, not always lowest latency |
| Per-region control | Fine-grained weights | Coarse (announce or withdraw) |

## In practice

nginx as an L7 balancer for a Django fleet (illustrative):

```nginx
upstream django_api {
    random two least_conn;          # power of two choices
    server 10.0.1.11:8000 max_fails=3 fail_timeout=10s;
    server 10.0.1.12:8000 max_fails=3 fail_timeout=10s;
    server 10.0.1.13:8000 weight=2; # bigger instance
    keepalive 64;                   # reuse upstream connections
}

server {
    listen 443 ssl;
    location /api/ {
        proxy_pass http://django_api;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_next_upstream error timeout http_502 http_503;
        proxy_next_upstream_tries 2;    # bounded retries
        proxy_connect_timeout 1s;
        proxy_read_timeout 15s;
    }
}
```

Externalized Django sessions (illustrative, Django 4.0+):

```python
# settings.py
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": "redis://redis.internal:6379/1",
    }
}
SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"  # cache + DB fallback
```

Kubernetes HPA scaling the API on CPU with asymmetric behavior (illustrative):

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: api
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: api
  minReplicas: 3
  maxReplicas: 40
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 65
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 0
      policies:
        - type: Percent
          value: 100
          periodSeconds: 30
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
        - type: Percent
          value: 20
          periodSeconds: 60
```

See [Kubernetes]({{ '/backend/kubernetes/' | relative_url }}) for requests/limits, which the CPU percentage is computed against.

## Pitfalls

- **Hidden local state.** Symptom: users randomly logged out, uploads "missing", rate limits that do not limit. Fix: grep for module-level dicts, `/tmp` writes and `LocMemCache`; move them to shared stores.
- **gRPC or HTTP/2 behind an L4 balancer.** Symptom: one pod at 100% CPU, others idle. Fix: L7 balancing, client-side balancing with periodic re-resolution, or max connection age.
- **Retry amplification.** The balancer retries, the app retries, the client retries: one slow dependency triples load. Fix: retry at one layer, with budgets (see [retries and backoff]({{ '/backend/retries-and-backoff/' | relative_url }})).
- **Deep health checks.** A readiness probe that queries Postgres takes the whole fleet out when Postgres has a 2-second hiccup. Fix: shallow liveness, readiness with care, fail-open behavior understood.
- **Autoscaling into a wall.** More pods, more DB connections, DB falls over, latency rises, HPA adds more pods. Fix: cap replicas by downstream capacity, pool connections, scale on the right metric.
- **Long DNS TTLs during failover.** Fix: 30–60 s TTLs on records you intend to move, and test failover before you need it.
- **No connection draining.** Pods killed mid-request during deploys. Fix: readiness flips to failing on SIGTERM, wait for in-flight requests, then exit.

## Interview questions

**How do you make a service horizontally scalable?** Make it stateless: sessions in Redis or signed tokens, files in object storage, jobs in a queue, caches shared. Then put it behind a balancer with health checks and autoscale on a metric that tracks saturation.

**L4 or L7 load balancer?** L4 for raw throughput, non-HTTP protocols and TLS pass-through; L7 when you need routing by path or header, per-request balancing (HTTP/2, gRPC), retries and observability. Large setups use L4 in front of a fleet of L7 proxies.

**Why is least-connections worse than power of two choices in practice?** Least-connections needs an accurate global view. With many balancers and stale counts, they all pick the same server and overload it. P2C randomizes the candidate set, so stale data cannot herd, and it still gets most of the benefit of least-loaded.

**What is the difference between liveness and readiness?** Liveness failing means restart the container; readiness failing means stop sending traffic. Liveness should only detect a wedged process; readiness covers warm-up, draining and critical dependencies.

**Are sticky sessions a good idea?** Only when the backend holds connection-bound state (WebSockets, in-memory collaboration). Otherwise externalize session state; stickiness causes uneven load and lost sessions on scale-in.

**How does a request reach the right region?** DNS-based GSLB returns region-specific IPs by geography, latency and health, or an anycast IP lets BGP deliver to the nearest edge, which then proxies to a healthy region. DNS failover is TTL-bound; anycast failover is BGP-bound.

**When would you scale vertically?** For the primary database and other stateful components that are hard to partition, and early on when one machine is enough. It buys time without changing the architecture.

## Further reading

- *The Power of Two Choices in Randomized Load Balancing*, Michael Mitzenmacher (IEEE TPDS, 2001).
- *Maglev: A Fast and Reliable Software Network Load Balancer*, Eisenbud et al. (NSDI 2016).
- *The Tail at Scale*, Jeffrey Dean and Luiz André Barroso (CACM, 2013).
- Kubernetes documentation: Horizontal Pod Autoscaling.
- *Designing Data-Intensive Applications*, Martin Kleppmann, chapter 1 (scalability) and part II.
