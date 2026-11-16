---
title: API gateway and backend for frontend
section: backend
order: 6
phase: "Phase 1 · Backend architecture"
summary: What an API gateway does at the edge, how routing, auth offload, rate limits and aggregation work, and when a per-client BFF beats one shared API.
---

Once a backend splits into services, every client has the same problems: which host serves `/orders`, who checks the token, who enforces the rate limit, and how a mobile home screen avoids making nine round trips over a 4G link. Putting those answers in every service duplicates security-critical code; putting them in the client leaks your internal topology and makes every refactor a breaking change. An API gateway centralizes the cross-cutting edge concerns, and a backend for frontend (BFF) adds a thin, client-specific layer that shapes data for one user experience.

```text
  web app        iOS / Android      partner API clients
     |                 |                    |
     v                 v                    v
+----------------------------------------------------------+
| API gateway: TLS, authn, rate limits, routing, request id |
+----------------------------------------------------------+
     |                 |                    |
     v                 v                    |
 +---------+      +------------+            |
 | web BFF |      | mobile BFF |            |   (public API goes
 +---------+      +------------+            |    straight through)
   |  |  |          |   |   |               |
   v  v  v          v   v   v               v
 +--------+   +--------+   +--------+   +---------+
 | users  |   | orders |   | search |   | billing |
 +--------+   +--------+   +--------+   +---------+
```

## API gateway pattern

The pattern is a single entry point (or a small number of them) that sits between external clients and internal services. Clients see one hostname and one API surface; the gateway maps each request to an upstream and applies policy on the way through. Chris Richardson's microservices pattern catalog and Microsoft's Azure architecture guidance split it into three sub-patterns that are useful vocabulary in an interview:

- **Gateway routing**: one public address, many internal services, routed by path, host, header or method.
- **Gateway offloading**: move shared concerns (TLS, authentication, rate limiting, compression, CORS) out of services.
- **Gateway aggregation**: one client request fans out to several services and returns a combined response.

### Where it sits and what it is not

The gateway handles **north-south** traffic (clients into your system). Service-to-service **east-west** traffic is the job of service discovery and, optionally, a mesh; see [Service discovery and service mesh]({{ '/backend/service-discovery-and-mesh/' | relative_url }}).

| Component | Layer | Knows about | Typical job |
|---|---|---|---|
| L4 load balancer | TCP/UDP | IPs, ports | Spread connections across nodes |
| Reverse proxy (nginx) | HTTP | Hosts, paths | TLS, static files, proxying |
| API gateway | HTTP/gRPC | Routes, consumers, API keys, plans | Policy per API and per consumer |
| BFF | Application | One client's screens | Compose and shape data |
| Service mesh | L4/L7 inside cluster | Service identities | mTLS, retries, east-west routing |

The line between a reverse proxy and a gateway is blurry (nginx plus some Lua is a gateway). What matters is that a gateway models **API consumers** (keys, plans, quotas) and **API products** (routes, versions), not just upstream servers.

### Common implementations

- Managed: AWS API Gateway, Azure API Management, Google Cloud API Gateway and Apigee. Quick to adopt, billed per request, with hard limits on payload size and integration timeouts that you need to check before committing (they change, so read the current quotas page).
- Self-hosted: Kong (built on nginx/OpenResty), Envoy-based gateways (Envoy Gateway, Emissary, Gloo), Traefik, Tyk, Spring Cloud Gateway, and plain nginx or HAProxy.
- Kubernetes: the Gateway API (`Gateway`, `HTTPRoute`, `GRPCRoute`) is the successor to `Ingress`, with many implementations behind one spec.
- Netflix publicly described Zuul, its edge gateway, built around routing, auth and resiliency filters.

### When to use it, when not

Use a gateway when you have more than a couple of services behind a public API, third-party consumers who need keys and quotas, or several client types. Skip it (use a plain load balancer or reverse proxy) for a monolith or a modular monolith with one API, where it only adds a hop and a deploy target; see [Monolith and modular monolith]({{ '/backend/monolith-and-modular-monolith/' | relative_url }}).

## API gateway as an API design component

Treat the gateway as part of the API contract, not just infrastructure. Each capability below changes what clients see.

### Routing

Routes match on host, path prefix, method, headers or query parameters and pick an upstream cluster. Rules are usually longest-prefix-first, and prefix matching must respect path segments, so `/api/orders` must not match `/api/ordersX`. Routing is also where traffic management lives:

- **Weighted routing** for canaries (90% to v1, 10% to v2) and blue-green cutovers.
- **Header routing** for internal testing (`x-canary: true`) or per-tenant pinning.
- **Version routing** by path (`/v2/...`) or header; see [API versioning and documentation]({{ '/backend/api-versioning-and-documentation/' | relative_url }}).
- **Path rewriting** so the public path (`/api/orders/42`) maps to the internal one (`/internal/orders/42`) without leaking it.

### Authentication offload

The gateway validates credentials once so services do not each reimplement it:

- **JWTs**: verify signature against the issuer's JWKS (cached), `exp`, `nbf`, `iss` and `aud`. Pass verified claims downstream as headers (`x-user-id`, `x-tenant-id`, `x-scopes`) or forward the token itself. See [JWT and refresh tokens]({{ '/backend/jwt-and-refresh-tokens/' | relative_url }}).
- **Opaque tokens and sessions**: introspect against the auth server or session store, then cache the result for seconds, not hours, so revocation still works.
- **API keys**: look up the consumer, its plan and its quotas. Store hashes, not raw keys.
- **mTLS** for B2B partners: the client certificate is the identity.

The gateway should do **authentication** and coarse **authorization** (is this scope allowed on this route). Fine-grained authorization (can user 17 edit invoice 9001) depends on domain data and belongs in the owning service; see [RBAC and ABAC]({{ '/backend/rbac-and-abac/' | relative_url }}).

> **Gotcha:** if services trust `x-user-id`, the gateway must strip any incoming `x-user-*` headers from the client before setting its own. Otherwise anyone can send `x-user-id: admin`. Services should also only be reachable through the gateway (network policy or mTLS), or the header is meaningless.

### Rate limiting and quotas

Limits at the edge protect everything behind it. Common keys are API key, user, tenant, IP and route. Two kinds:

- **Rate limits** (requests per second, burst) protect capacity. Return `429` with `Retry-After` and, by convention, `RateLimit-*` or `X-RateLimit-*` headers.
- **Quotas** (requests per day or month per plan) enforce the business model.

Each gateway node can limit locally (fast, but the effective limit is `N × limit` across N nodes) or call a shared counter in Redis (accurate, one extra round trip). Envoy supports both styles: a local rate limit filter and a global rate limit service. Algorithms and trade-offs are in [Rate limiting]({{ '/backend/rate-limiting/' | relative_url }}).

### Request aggregation

One client request triggers several upstream calls run **in parallel**, combined into one response. This is the main win for mobile clients, where each round trip costs tens to hundreds of milliseconds. Rules that keep it safe:

- Per-dependency timeouts below the overall deadline, and a total budget for the whole request.
- Degrade, don't fail: if recommendations time out, return the page without them and mark the response partial.
- Keep it to composition. Once aggregation needs business rules, it belongs in a BFF or a service, not in gateway config.

GraphQL is a generalized form of aggregation: the client describes the shape and the server resolves it across services. See [GraphQL]({{ '/backend/graphql/' | relative_url }}).

### Transformation

- **Protocol translation**: REST/JSON at the edge to gRPC inside (Envoy's gRPC-JSON transcoder, grpc-gateway), or HTTP to an async queue for fire-and-forget endpoints.
- **Header manipulation**: add `x-request-id` and `traceparent`, remove internal headers from responses (`server`, `x-powered-by`, debug headers).
- **Payload shaping**: rename fields or drop internal ones for a legacy contract. Keep this minimal: heavy transformation in gateway config is business logic in the hardest place to test.

### Other edge concerns

TLS termination, CORS, body size limits, compression, caching public GETs, IP allow and deny lists, WAF rules, and observability. The gateway sees every request, so it is the natural place for the request ID and the first trace span; see [Distributed tracing and OpenTelemetry]({{ '/backend/distributed-tracing-and-opentelemetry/' | relative_url }}).

## Backend for frontend (BFF)

A BFF is a backend service dedicated to one client experience: one for the web app, one for mobile, one for a smart TV app. Sam Newman popularized the name in a 2015 article, describing how SoundCloud moved away from a single general-purpose API. The idea is that different clients need different things:

| Concern | Web | Mobile |
|---|---|---|
| Payload size | Larger pages fine | Small, battery and data aware |
| Round trips | Cheap on broadband | Expensive, aggregate aggressively |
| Auth | Cookie session, CSRF | Tokens, refresh flow |
| Release cadence | Deploy anytime | App store review, old versions live for years |
| Push | WebSockets, SSE | APNs, FCM |

A single general-purpose API ends up serving the union of all clients' needs, with flags like `?include=...&compact=true`, and every client team queues behind the API team. A BFF lets the **frontend team own** the backend that serves it: they can add a field for a new screen the same day, shape responses for their views, and handle client-specific auth.

### How a BFF works

- Sits behind the gateway (which still does TLS, authn, global limits).
- Calls domain services over internal APIs, in parallel, and composes the response for a screen (`GET /mobile/home`), not for a resource.
- Holds no domain data of its own. It may cache, and may keep session state for web clients (the "token handler" variant keeps OAuth tokens server-side and gives the browser only an HTTP-only cookie).
- Old mobile versions keep calling old BFF endpoints for years, so a mobile BFF is where you keep compatibility shims without polluting domain services.

### When to use it, when not

| Use a BFF when | Avoid it when |
|---|---|
| Clients differ materially in data shape or auth | One web client, or clients nearly identical |
| Separate frontend teams want autonomy | One small team owns everything |
| Mobile needs aggressive aggregation | A GraphQL layer already gives clients flexible shapes |
| Old app versions need long-lived compatibility | You can't staff another deployable service |

Granularity: one BFF per **experience**, not per platform. iOS and Android usually share a mobile BFF because their screens are the same.

### BFF risks

- **Duplication**: the web and mobile BFFs both implement "order total including discounts". When logic appears in two BFFs, push it down into a domain service.
- **Fan-out latency**: a BFF that calls services sequentially is slower than the client calling them in parallel. Always fan out concurrently with deadlines.
- **Becoming a monolith**: a BFF that writes to databases and owns rules is a new god service. It reads and composes; services decide.

## In practice

### A gateway in miniature

A runnable, stdlib-only sketch of the core gateway pipeline: verify a token, rate limit per user, longest-prefix route, strip spoofed identity headers, and aggregate with per-call timeouts and partial results.

```python
import asyncio
import hashlib
import hmac
import time

SECRET = b"demo-gateway-key"


def issue_token(user_id: str) -> str:
    mac = hmac.new(SECRET, user_id.encode(), hashlib.sha256).hexdigest()
    return f"{user_id}.{mac}"


def verify_token(token: str):
    user_id, _, mac = token.rpartition(".")
    expected = hmac.new(SECRET, user_id.encode(), hashlib.sha256).hexdigest()
    return user_id if user_id and hmac.compare_digest(mac, expected) else None


class TokenBucket:
    def __init__(self, rate: float, burst: int, clock=time.monotonic):
        self.rate, self.burst, self.clock = rate, burst, clock
        self.tokens, self.updated = float(burst), clock()

    def allow(self) -> bool:
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


# Prefix -> upstream service. Longest matching prefix wins.
ROUTES = {"/api": "legacy", "/api/orders": "orders", "/api/users": "users"}


def match_route(path: str):
    hits = [p for p in ROUTES if path == p or path.startswith(p + "/")]
    return ROUTES[max(hits, key=len)] if hits else None


class Gateway:
    def __init__(self, upstreams, rate=5.0, burst=5, clock=time.monotonic):
        self.upstreams, self.rate, self.burst, self.clock = upstreams, rate, burst, clock
        self.buckets = {}

    async def handle(self, path: str, headers: dict):
        token = headers.get("authorization", "").removeprefix("Bearer ")
        user_id = verify_token(token)
        if user_id is None:
            return 401, {"error": "unauthenticated"}
        bucket = self.buckets.setdefault(
            user_id, TokenBucket(self.rate, self.burst, self.clock))
        if not bucket.allow():
            return 429, {"error": "rate_limited"}
        service = match_route(path)
        if service is None:
            return 404, {"error": "no_route"}
        # Never forward identity headers the client sent; set them ourselves.
        clean = {k: v for k, v in headers.items()
                 if not k.startswith("x-user") and k != "authorization"}
        clean["x-user-id"] = user_id
        return 200, await self.upstreams[service](path, clean)


async def aggregate(calls: dict, timeout: float):
    """Fan out concurrently; a slow or failing dependency degrades, not fails."""
    async def guarded(coro):
        try:
            return await asyncio.wait_for(coro, timeout)
        except Exception:  # timeout or upstream error
            return None

    names = list(calls)
    results = await asyncio.gather(*(guarded(calls[n]) for n in names))
    body = dict(zip(names, results))
    body["partial"] = any(r is None for r in results)
    return body


def fake_service(name):
    async def call(path, headers):
        return {"service": name, "path": path, "user": headers["x-user-id"]}
    return call


async def main():
    now = [0.0]
    upstreams = {n: fake_service(n) for n in ("legacy", "orders", "users")}
    gw = Gateway(upstreams, rate=1.0, burst=2, clock=lambda: now[0])
    alice = {"authorization": "Bearer " + issue_token("alice"), "x-user-id": "admin"}

    status, body = await gw.handle("/api/orders/42", alice)
    assert status == 200 and body["service"] == "orders"
    assert body["user"] == "alice"                      # spoofed header dropped
    assert match_route("/api/ordersX") == "legacy"      # prefix is segment-aware
    assert match_route("/static/app.js") is None

    assert (await gw.handle("/api/users/me", alice))[0] == 200
    assert (await gw.handle("/api/users/me", alice))[0] == 429   # burst used
    now[0] += 1.0                                                # 1 token back
    assert (await gw.handle("/api/users/me", alice))[0] == 200

    forged = {"authorization": "Bearer alice.deadbeef"}
    assert (await gw.handle("/api/orders", forged))[0] == 401

    async def slow():
        await asyncio.sleep(1)
        return ["rec-1"]

    async def profile():
        return {"name": "Alice"}

    async def broken():
        raise ConnectionError("orders down")

    home = await aggregate(
        {"profile": profile(), "recs": slow(), "orders": broken()}, timeout=0.05)
    assert home == {"profile": {"name": "Alice"}, "recs": None,
                    "orders": None, "partial": True}
    print("gateway demo ok")


if __name__ == "__main__":
    asyncio.run(main())
```

### nginx as a small gateway

Illustrative config: per-IP limit, auth delegated to an internal endpoint with `auth_request`, the verified user ID injected as a header, and upstream keepalive.

```nginx
limit_req_zone $binary_remote_addr zone=per_ip:10m rate=20r/s;

upstream orders_svc {
    server orders.internal:8000;
    keepalive 32;
}

server {
    listen 443 ssl;
    server_name api.example.com;

    location /api/orders/ {
        limit_req zone=per_ip burst=40 nodelay;
        auth_request /_auth;
        auth_request_set $user_id $upstream_http_x_user_id;

        proxy_set_header X-User-Id $user_id;      # overwrites any client value
        proxy_set_header X-Request-Id $request_id;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_connect_timeout 1s;
        proxy_read_timeout 5s;
        proxy_pass http://orders_svc/internal/orders/;
    }

    location = /_auth {
        internal;
        proxy_pass http://auth.internal/verify;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header X-Original-URI $request_uri;
    }
}
```

### Kubernetes Gateway API canary route

Illustrative `HTTPRoute` sending 10% of `/api/orders` traffic to a new version:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: orders
  namespace: shop
spec:
  parentRefs:
    - name: public-gateway
      namespace: infra
  hostnames: ["api.example.com"]
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /api/orders
      backendRefs:
        - name: orders-v1
          port: 8000
          weight: 90
        - name: orders-v2
          port: 8000
          weight: 10
```

### A Django BFF endpoint

Illustrative async Django view for a mobile home screen. The identity comes from the gateway, calls run in parallel, and recommendations are optional.

```python
import asyncio
import httpx
from django.http import JsonResponse

client = httpx.AsyncClient(timeout=httpx.Timeout(0.3, connect=0.1))  # shared pool


async def fetch(url, **params):
    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError:
        return None


async def mobile_home(request):
    user_id = request.headers["X-User-Id"]  # set by the gateway only
    profile, orders, recs = await asyncio.gather(
        fetch(f"http://users.internal/users/{user_id}"),
        fetch("http://orders.internal/orders", user_id=user_id, limit=3),
        fetch(f"http://recs.internal/recs/{user_id}"),
    )
    if profile is None:
        return JsonResponse({"error": "unavailable"}, status=503)
    return JsonResponse({
        "greeting": profile["first_name"],
        "recent_orders": [{"id": o["id"], "status": o["status"]}
                          for o in (orders or [])],
        "recommendations": recs or [],
        "partial": orders is None or recs is None,
    })
```

## Pitfalls

- **Business logic in the gateway.** Pricing rules in Lua or a 400-line transformation template: untestable, owned by nobody, and every change needs a platform deploy. Keep the gateway generic; move logic to services or a BFF.
- **Single point of failure.** Run several gateway replicas across zones behind an L4 load balancer, and keep config changes canaried like code. A bad route config takes down every API at once.
- **Timeout mismatch.** If the gateway times out at 5 s and the service at 30 s, the service keeps working on requests nobody is waiting for. Set timeouts that shrink inward: client > gateway > BFF > service > database.
- **Retry amplification.** Client, gateway, BFF and service each retrying 3 times turns one failure into 81 calls. Retry at one layer, only idempotent requests, with budgets; see [Retries and backoff]({{ '/backend/retries-and-backoff/' | relative_url }}).
- **Header spoofing.** Not stripping client-supplied identity headers, or services reachable directly, bypassing the gateway.
- **Local rate limits that don't add up.** Ten gateway pods each allowing 100 req/s means 1,000 req/s total. Use a shared counter or divide the limit by replica count.
- **Sequential fan-out in a BFF.** Five 50 ms calls in series is 250 ms; in parallel it's about 50 ms. Symptom: BFF latency equals the sum of its dependencies.
- **Large payloads and streaming.** Buffering gateways break file uploads and server-sent events. Configure streaming and body limits per route.

## Interview questions

**Why put an API gateway in front of microservices?** One stable public entry point that hides internal topology, and one place for TLS, authentication, rate limiting, request IDs and routing, so services don't reimplement security-critical code and clients aren't coupled to service boundaries.

**What should never live in the gateway?** Domain logic and fine-grained authorization that needs domain data. The gateway decides "is this caller authenticated and allowed on this route"; the service decides "may this user edit this invoice".

**Gateway vs BFF?** A gateway is generic, shared, owned by platform, and handles cross-cutting policy. A BFF is client-specific, owned by the frontend team, and composes domain data for screens. Common setups have both: gateway in front, BFFs behind it.

**How do you avoid the gateway becoming a bottleneck?** Stateless horizontally scaled replicas across zones, connection pooling and keepalive to upstreams, async I/O proxies (Envoy, nginx), caching JWKS and auth decisions briefly, and keeping heavy work out of the request path.

**How do services know who the user is after auth offload?** The gateway forwards verified claims in headers or forwards the token. Services trust those headers only because the network guarantees traffic came through the gateway (network policy, mTLS), and the gateway overwrites any client-sent copies.

**How would you do a canary release at the gateway?** Weighted routing between two upstream versions (for example 95/5), sticky by user or tenant ID if behavior must be consistent per user, watch error rate and latency per version, then shift weights or roll back.

**When is GraphQL a better choice than a BFF?** When many clients need different shapes of the same data and you want clients to choose fields without a backend change. It brings its own costs (query cost limits, N+1 in resolvers, caching). Some teams run GraphQL as the BFF.

**What happens if one dependency of an aggregated endpoint is slow?** With per-call timeouts and parallel fan-out, the response returns within the budget without that section, flagged partial. Without them, the slowest dependency sets the latency for everything.

## Further reading

- Sam Newman, *Building Microservices*, 2nd edition (O'Reilly), chapters on the user interface and API gateways, and his article "Pattern: Backends For Frontends".
- Chris Richardson, *Microservices Patterns* (Manning), the API gateway and BFF patterns.
- Microsoft Azure Architecture Center: Gateway Routing, Gateway Offloading, Gateway Aggregation and Backends for Frontends patterns.
- Envoy proxy documentation: HTTP routing, rate limiting and the gRPC-JSON transcoder.
- Kubernetes Gateway API documentation (gateway-api.sigs.k8s.io).
