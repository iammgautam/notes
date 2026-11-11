---
title: HTTP essentials
section: networking
order: 2
summary: Methods, safety and idempotency, the status codes that matter, caching headers, keep-alive, and what HTTP/2 and HTTP/3 actually change.
---

HTTP semantics are a contract between your API, clients, proxies, CDNs and retry logic. Get them right and caches, retries and browsers do the correct thing for free. Get them wrong and a retrying proxy double-charges a customer, or a CDN serves one tenant's data to another.

## How it works

### Methods, safety and idempotency

- **Safe**: no server-side state change intended. Clients and crawlers may call freely.
- **Idempotent**: calling N times leaves the same server state as calling once. Retries are safe.

| Method | Safe | Idempotent | Typical use |
|---|---|---|---|
| GET | yes | yes | Read a resource or collection |
| HEAD | yes | yes | Headers only (existence, size) |
| OPTIONS | yes | yes | CORS preflight, capabilities |
| PUT | no | yes | Replace a resource at a known URL |
| DELETE | no | yes | Remove a resource |
| PATCH | no | not guaranteed | Partial update |
| POST | no | no | Create, trigger an action |

Idempotency is about **state**, not the response: a second `DELETE` may return `404` instead of `204`, and that is fine. `PATCH {"op": "increment"}` is not idempotent; `PATCH {"status": "archived"}` is. For POST, you make retries safe with an `Idempotency-Key` header (see [Race conditions in web apps]({{ '/os-concurrency/race-conditions-in-web-apps/' | relative_url }})).

### Status codes that matter

| Code | Meaning | Use it when |
|---|---|---|
| 200 | OK | Successful read or update with body |
| 201 | Created | POST created something; add `Location` |
| 202 | Accepted | Work queued (Celery job), not done yet |
| 204 | No Content | Success, no body (DELETE) |
| 301 / 308 | Permanent redirect | 308 keeps method and body |
| 302 / 307 | Temporary redirect | 307 keeps method and body |
| 304 | Not Modified | Conditional GET matched the cache |
| 400 | Bad Request | Malformed or invalid input |
| 401 | Unauthorized | Not authenticated (missing or bad credentials) |
| 403 | Forbidden | Authenticated, not allowed |
| 404 | Not Found | Missing, or hidden from this tenant |
| 409 | Conflict | State conflict, duplicate, version mismatch |
| 412 | Precondition Failed | `If-Match` ETag did not match |
| 422 | Unprocessable Content | Valid syntax, failed validation |
| 429 | Too Many Requests | Rate limited; send `Retry-After` |
| 500 | Internal Server Error | Your bug |
| 502 | Bad Gateway | Proxy got a bad response from upstream |
| 503 | Service Unavailable | Overloaded or in maintenance |
| 504 | Gateway Timeout | Upstream did not answer in time |

> **Gotcha:** 502 vs 504 tells you where to look. 502 usually means the app process crashed or closed the connection (OOM-killed worker, gunicorn timeout killing a worker). 504 means the app was alive but too slow for the proxy timeout.

### Caching headers

```http
HTTP/1.1 200 OK
Cache-Control: private, max-age=60
ETag: "v42-9f1c"
Last-Modified: Tue, 10 Nov 2026 08:00:00 GMT
Vary: Accept-Encoding, Authorization
```

- `Cache-Control: max-age=N`: fresh for N seconds. `s-maxage` applies to shared caches (CDN).
- `private`: only the browser may cache. `public`: shared caches may too.
- `no-cache`: cache it but revalidate every time. `no-store`: never write it down. Use `no-store` for anything sensitive.
- `ETag` + `If-None-Match`: client revalidates, server answers `304` with no body if unchanged.
- `ETag` + `If-Match` on writes: optimistic concurrency, server answers `412` if someone else changed it.
- `Vary`: which request headers change the response. Forget `Vary: Authorization` (or `Cookie`) on a cacheable per-user response and a shared cache can serve user A's data to user B.
- Static assets with hashed filenames: `Cache-Control: public, max-age=31536000, immutable`.

### Connections and keep-alive

HTTP/1.1 keeps TCP connections open by default (`Connection: keep-alive`), but each connection carries one request at a time. Browsers open ~6 connections per origin to get parallelism. A slow response blocks the ones queued behind it on that connection (head-of-line blocking).

### HTTP/1.1 vs HTTP/2 vs HTTP/3

| | HTTP/1.1 | HTTP/2 | HTTP/3 |
|---|---|---|---|
| Transport | TCP | TCP | QUIC over UDP |
| Format | Text | Binary frames | Binary frames |
| Concurrency | One request per connection at a time | Many streams multiplexed on one connection | Multiplexed, independent streams |
| Header compression | None | HPACK | QPACK |
| HOL blocking | HTTP and TCP level | TCP level only (one lost packet stalls all streams) | None across streams |
| Handshake | TCP + TLS | TCP + TLS | Combined, 1 RTT, 0-RTT resume |

HTTP/3 helps most on lossy mobile networks and when users switch networks (QUIC connection IDs survive IP changes). For server-to-server traffic inside a datacenter, HTTP/1.1 with keep-alive or HTTP/2 is usually fine.

## In practice

### Conditional GET in DRF

```python
from django.utils.http import quote_etag
from rest_framework.response import Response
from rest_framework.views import APIView

class ProjectDetail(APIView):
    def get(self, request, pk):
        project = get_project_for_tenant(request.tenant, pk)
        etag = quote_etag(f"{project.pk}-{project.version}")
        if request.headers.get("If-None-Match") == etag:
            return Response(status=304, headers={"ETag": etag})
        data = ProjectSerializer(project).data
        return Response(data, headers={
            "ETag": etag,
            "Cache-Control": "private, max-age=0, must-revalidate",
        })
```

Django also ships `django.views.decorators.http.condition(etag_func=...)` and `ConditionalGetMiddleware` for plain views.

### Inspect headers and protocol

```bash
curl -sI https://api.example.com/v1/projects/42
curl -s -o /dev/null -w '%{http_version}\n' --http2 https://example.com
curl -H 'If-None-Match: "v42-9f1c"' -i https://api.example.com/v1/projects/42
```

### Retry policy

Retry automatically only on idempotent methods (or POSTs with an idempotency key), only on 429, 502, 503, 504 and connection errors, with exponential backoff plus jitter, and honor `Retry-After`.

```python
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
import requests

session = requests.Session()
retry = Retry(total=3, backoff_factor=0.5,
              status_forcelist=[429, 502, 503, 504],
              allowed_methods={"GET", "PUT", "DELETE", "HEAD"},
              respect_retry_after_header=True)
session.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=20))
```

## Pitfalls

- **GET with side effects.** Link prefetchers, crawlers and retries will trigger it. Anything that changes state is POST/PUT/PATCH/DELETE.
- **200 with an error body.** Monitoring, retries and clients all key off the status code. Return real 4xx/5xx.
- **401 vs 403 confusion.** 401 means "who are you?" and should come with `WWW-Authenticate`; 403 means "I know who you are, no".
- **Leaking existence across tenants.** Return 404, not 403, for another tenant's resource so IDs can't be probed.
- **Caching per-user responses in a CDN.** Default to `private` or `no-store` on authenticated endpoints.
- **Retrying non-idempotent POSTs** in a client library or service mesh produces duplicates. Check your mesh and SDK retry defaults.

## Rules of thumb

- Pick the method by semantics; make every write either idempotent or keyed.
- Use a small, consistent set of status codes and document them.
- Authenticated responses: `Cache-Control: private` or `no-store` unless you know better.
- Use ETags for both cheap revalidation and lost-update protection.
- Always reuse connections; enable HTTP/2 at the edge.

## Interview angle

- **"Is PUT idempotent? Is PATCH?"** PUT yes by definition, since it replaces the full state. PATCH depends on the patch: setting fields is idempotent, increments or appends are not.
- **"What is the difference between 502 and 504?"** Both come from a proxy. 502 means the upstream answered badly or dropped the connection; 504 means it did not answer before the proxy timeout.
- **"What does HTTP/2 fix and what does HTTP/3 fix?"** HTTP/2 multiplexes many requests over one TCP connection and compresses headers, removing HTTP-level head-of-line blocking. HTTP/3 moves to QUIC over UDP so packet loss on one stream does not stall the others and handshakes are faster.
