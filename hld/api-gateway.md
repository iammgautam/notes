---
title: API gateway and service communication
section: hld
order: 12
summary: What an API gateway should and should not do, sync vs async service calls, and how services find each other.
---

## What it is

An **API gateway** is the single entry point for external clients. It routes requests to internal services and handles cross-cutting concerns once instead of in every service.

```text
mobile / web / partners
          |
     API gateway  (TLS, authn, rate limit, routing)
      /    |     \
  users  orders  search      <- internal services
            |
          queue -> billing, notifications
```

## Gateway responsibilities

| Do at the gateway | Keep in services |
|---|---|
| TLS termination | Business logic |
| Authentication (verify JWT / API key) | Fine-grained authorization (can user X edit doc Y) |
| Rate limiting, quotas per API key / tenant | Data validation beyond shape |
| Routing, versioning (`/v1`, `/v2`) | Orchestration of business flows |
| Request/response size limits, CORS | |
| Request ids, access logs, metrics, tracing headers | |
| Caching of public GET responses | |

**BFF (backend for frontend):** a gateway layer per client type (web, mobile) that aggregates calls and shapes responses. Useful when clients need very different payloads.

Examples: Kong, AWS API Gateway, Envoy/Istio ingress, Nginx, Traefik.

## Sync vs async communication

| | Synchronous (REST/gRPC) | Asynchronous (queue/events) |
|---|---|---|
| Coupling | Temporal: callee must be up now | Decoupled in time |
| Latency | Caller waits | Caller returns immediately |
| Failure | Propagates up the chain | Buffered, retried later |
| Consistency | Easier to reason about | Eventual |
| Debugging | Simple traces | Needs correlation ids, harder |
| Use for | Queries, user-facing reads, need the answer now | Side effects, fan-out, slow work, cross-service updates |

- REST/JSON: universal, easy to debug. gRPC: typed contracts (protobuf), HTTP/2, streaming, faster; good for internal service-to-service calls.
- Long sync chains (A → B → C → D) multiply latency and failure probability: availability 0.999^4 ≈ 0.996.

> **Rule of thumb:** Sync for reads the user waits on; async events for "something happened, others may react".

### Orchestration vs choreography

- **Orchestration:** a coordinator calls each step (a saga orchestrator / workflow engine like Temporal). Clear flow, central place for compensation.
- **Choreography:** services react to each other's events. Loosely coupled, but the flow is hard to see and debug.

## Service discovery

How service A finds healthy instances of B.

| Approach | How | Examples |
|---|---|---|
| DNS | `orders.internal` resolves to instances/VIP | Kubernetes Services, Route 53 |
| Server-side | Client → LB → instances; LB knows the registry | AWS ALB, K8s Service + kube-proxy |
| Client-side | Client queries registry, balances itself | Consul, Eureka, gRPC xDS |
| Service mesh | Sidecar proxy handles discovery, retries, mTLS | Istio, Linkerd |

- Registry needs health checks and deregistration on shutdown.
- DNS caching causes stale endpoints; keep TTLs low.

## Trade-offs

| Choice | Gain | Cost |
|---|---|---|
| Gateway | One place for auth, limits, observability | Extra hop (~1–5 ms), a critical component to scale and keep up |
| Business logic in gateway | Faster to ship | Becomes a distributed monolith |
| Service mesh | Uniform retries, mTLS, metrics | Operational complexity, sidecar overhead |
| Many microservices | Independent deploys/teams | Network failures, distributed transactions |

## Gotchas

- Gateway timeouts must exceed service timeouts, or the gateway gives up while work continues.
- Do not retry non-idempotent requests at the gateway.
- Propagate request id / trace context (`traceparent`) through gateway, services and queue messages.
- Internal calls still need auth (mTLS or service tokens); "inside the VPC" is not a security model.
- Multi-tenant: resolve tenant at the gateway (subdomain, header, token claim) and pass it down explicitly.

> **Interview tip:** Draw the gateway, list 4–5 responsibilities, then say which calls are sync and which are async events. That shows you know where coupling lives.
