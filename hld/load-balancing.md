---
title: Load balancing
section: hld
order: 4
summary: L4 vs L7 load balancers, balancing algorithms, health checks and when sticky sessions are worth it.
---

## What it is

A load balancer (LB) spreads requests across a pool of backends, removes unhealthy ones, and gives clients one stable address. It is also a natural place for TLS termination, timeouts and basic protection.

## L4 vs L7

| | L4 (transport) | L7 (application) |
|---|---|---|
| Sees | IP, port, TCP/UDP | HTTP method, path, headers, cookies |
| Routing | Per connection | Per request |
| TLS | Pass-through (usually) | Terminates TLS |
| Speed | Very fast, low overhead | More CPU per request |
| Features | Simple, protocol agnostic | Path/host routing, retries, header rewrite, auth, gzip |
| Examples | AWS NLB, LVS, HAProxy TCP mode | AWS ALB, Nginx, Envoy, HAProxy HTTP mode |

- Use L4 for raw throughput, non-HTTP protocols, long-lived TCP.
- Use L7 for HTTP microservices, path-based routing, canaries.
- **HTTP/2 and gRPC:** an L4 LB balances connections, not requests. One long-lived gRPC connection pins all traffic to one backend. Use L7 (Envoy) or client-side balancing.

## Algorithms

| Algorithm | How | Good for |
|---|---|---|
| Round robin | Next backend in order | Uniform backends, uniform requests |
| Weighted round robin | Proportional to weight | Mixed instance sizes, canaries |
| Least connections | Fewest active connections | Variable request duration |
| Least response time | Lowest latency + connections | Heterogeneous backends |
| Random / power of two choices | Pick 2 at random, take less loaded | Many LBs without shared state |
| IP / consistent hash | Hash(client or key) → backend | Cache affinity, sticky routing |

> **Rule of thumb:** Least connections (or power of two choices) is a safer default than plain round robin when request cost varies.

## Health checks

- **Active:** LB polls `/healthz` every N seconds; mark down after K failures, up after M successes.
- **Passive:** LB watches real traffic (5xx, timeouts) and ejects outliers (Envoy outlier detection).
- **Liveness vs readiness:** liveness = process alive (restart if not); readiness = can serve now (DB reachable, warmed up). LB uses readiness.
- Keep health checks cheap. A check that queries the DB can take the whole fleet down when the DB blips.

## Sticky sessions

The LB pins a client to one backend (cookie or IP hash).

| Pros | Cons |
|---|---|
| Works with in-memory session state | Uneven load, hot backends |
| Better local cache hit rate | Losing a node loses its sessions |
| Needed for some WebSocket setups | Fights autoscaling and deploys |

Prefer stateless backends with sessions in Redis or signed tokens. Use stickiness only for long-lived connections or deliberate cache affinity.

## Availability of the LB itself

- Run LBs in pairs (active-passive with a floating IP via VRRP/keepalived) or use a managed LB.
- DNS round robin or anycast in front of multiple LBs for global setups.
- GeoDNS / global LB routes users to the nearest healthy region.

```text
            DNS (GeoDNS / anycast)
             /                \
       LB (region A)       LB (region B)
       /    |    \          /    |    \
     app  app  app        app  app  app
```

## Gotchas

- **Connection draining:** on deploy, stop new requests to a node but let in-flight ones finish (deregistration delay).
- **Timeouts:** LB idle timeout shorter than the app keep-alive causes sporadic 502s. Set app keep-alive > LB idle timeout.
- **Retries at the LB** on non-idempotent requests (POST) can duplicate writes.
- **Slow start:** new instances with cold caches get flooded under least-connections. Ramp weight gradually.
- Client IP is lost behind an L7 LB; read `X-Forwarded-For` (trust only your LB's hop).

> **Interview tip:** Say "L7 LB terminating TLS, least-connections, readiness checks, connection draining on deploy". That covers what most interviewers want in one line, then move on.
