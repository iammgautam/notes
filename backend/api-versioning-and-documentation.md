---
title: API versioning and documentation
section: backend
order: 21
phase: "Phase 3 · API design"
summary: Compatibility rules, URL/header/media-type/date versioning, Deprecation and Sunset headers, contract tests, and OpenAPI with drf-spectacular and CI checks.
---

Every API change either keeps existing clients working or breaks them. Versioning is how you ship breaking changes without breaking anyone on deploy day; documentation is how clients learn what the contract is. Both are really about one thing, the contract, and both decay unless machines check them: a version nobody measures can never be retired, and docs nobody tests drift from the code within a quarter.

## Compatibility rules

Most changes should not need a new version at all. Evolve additively and reserve versions for real breaks.

| Usually safe (additive) | Breaking |
|---|---|
| New endpoint or method | Removing or renaming a field or endpoint |
| New optional request field or query param | Making an optional field required |
| New response field | Changing a field's type, format or units |
| Relaxing validation (longer max length) | Tightening validation |
| New error detail inside an existing error code | Changing status codes or error codes |
| New enum value, if clients were told enums are open | New enum value for clients with closed enums |
| | Changing defaults, sort order, pagination shape, auth |

Two laws shape this table:

- **Hyrum's law**: with enough users, every observable behavior is depended on by somebody, including field order, error message text and response timing. "Not in the docs" does not make a change safe.
- **Tolerant reader**: clients must ignore unknown fields and unknown enum values. Say so in the docs from day one; it is what makes additive evolution possible.

The same rules apply to webhook payloads and events, which teams often forget to version at all.

## URL path versioning

`/v1/orders`, `/v2/orders`. The most common approach (Google Cloud APIs and many public APIs use a major version in the path).

- Visible in logs, links and browser tests; trivial to route at a gateway or load balancer.
- Caches key on the URL, so versions never collide.
- Coarse: the whole API moves together, so a breaking change to one resource forces a new version of everything, or an awkward mix of `v1` and `v2` paths.
- Resource URLs change between versions, which breaks stored links and hypermedia.

Use it for **major** versions that change rarely (years apart), combined with additive evolution inside each.

## Header versioning

A request header selects the version: `Api-Version: 2`, or GitHub's date-valued `X-GitHub-Api-Version: 2022-11-28`. A query parameter is the close cousin; Azure uses `?api-version=2024-01-01`.

- URLs stay stable; the version can be chosen per request.
- Invisible in a plain link or browser address bar, so you must log it explicitly.
- Shared caches must include it: send `Vary: Api-Version`, or a CDN will serve one version's body to another version's client.
- Define the default when the header is missing (latest, oldest, or the account's pinned version) and document it.

## Media type versioning

Content negotiation on a vendor media type:

```http
GET /orders/ord_123 HTTP/1.1
Accept: application/vnd.acme.order.v2+json

HTTP/1.1 200 OK
Content-Type: application/vnd.acme.order.v2+json
Vary: Accept
```

Unsupported versions get `406 Not Acceptable`. GitHub's REST API historically used `application/vnd.github.v3+json`. This is the most "RESTful" option because the version describes the representation, not the resource, and it allows per-resource versions. In practice it is clumsy for clients, awkward in generated SDKs, and easy to break with a missing `Vary: Accept`.

| | URL path | Header / query | Media type | Date-based |
|---|---|---|---|---|
| Visibility | High | Medium | Low | Medium |
| Granularity | Whole API | Whole API or per request | Per resource | Per change |
| Caching | Natural | Needs `Vary` | Needs `Vary: Accept` | Needs `Vary` |
| Gateway routing | Trivial | Header rules | Header rules | Usually one backend |
| Client effort | Lowest | Low | Highest | Low (pinned by default) |
| Typical users | Google Cloud, most APIs | GitHub, Azure | Older GitHub API | Stripe |

## Date-based versions

Stripe has publicly described the model: every account is pinned to the API version current at its first request, a `Stripe-Version` header overrides it per request, and each breaking change ships as a new dated version (newer versions also carry a release name). Internally, handlers only implement the latest behavior; a chain of version change modules transforms responses back for clients pinned to older dates. Webhook events are rendered with the receiving endpoint's version too.

```text
request (Stripe-Version header, else account's pinned version)
   |
   v
[resolve version] -> [upgrade old request shape] -> handler (latest only)
                                                          |
                                                    latest response
                                                          |
client <- [undo 2025-03-01 change] <- [undo 2025-09-01 change]
          (applied only if the client's version is older)
```

Why it works: clients never get broken by surprise, they upgrade on their own schedule one changelog entry at a time, and the server keeps a single code path. Costs: transforms accumulate forever unless old versions are retired, requests need upgrade transforms as well as responses, and changes in **behavior** (not shape), such as a different refund rule, cannot be expressed as a transform and need version checks inside business logic.

A runnable model of the response side:

```python
import copy
from datetime import date

# Latest representation. Older pinned versions are produced by undoing
# each change made after them, newest first (version change modules).


def undo_amount_cents(body):        # 2025-09-01: amount float -> int cents
    body["amount"] = body.pop("amount_cents") / 100


def undo_expanded_customer(body):   # 2025-03-01: customer_id -> object
    body["customer_id"] = body.pop("customer")["id"]


def undo_status_rename(body):       # 2024-06-01: "state" renamed "status"
    body["state"] = body.pop("status")


VERSION_CHANGES = [                 # (introduced in, downgrade), newest first
    ("2025-09-01", undo_amount_cents),
    ("2025-03-01", undo_expanded_customer),
    ("2024-06-01", undo_status_rename),
]
FIRST_VERSION = "2024-01-01"


def resolve_version(header_value, account_pinned):
    """Header wins; otherwise the version the account was pinned to."""
    version = header_value or account_pinned
    date.fromisoformat(version)     # ValueError -> respond 400
    if version < FIRST_VERSION:
        raise ValueError(f"unknown API version {version}")
    return version


def render(latest_body, version):
    body = copy.deepcopy(latest_body)
    for introduced, downgrade in VERSION_CHANGES:
        if version < introduced:
            downgrade(body)
    return body


if __name__ == "__main__":
    latest = {
        "id": "ord_1",
        "status": "paid",
        "amount_cents": 1999,
        "customer": dict(id="cus_9", name="Asha"),
    }
    assert render(latest, "2025-09-01") == latest
    assert render(latest, "2025-05-20") == {
        "id": "ord_1", "status": "paid", "amount": 19.99,
        "customer": dict(id="cus_9", name="Asha"),
    }
    oldest = render(latest, resolve_version(None, "2024-01-01"))
    assert oldest == {"id": "ord_1", "state": "paid", "amount": 19.99,
                      "customer_id": "cus_9"}
    # Any date between releases behaves like the release before it.
    assert render(latest, "2024-12-31") == render(latest, "2024-06-01")
    assert resolve_version("2025-03-01", "2024-01-01") == "2025-03-01"
    for bad in ("v2", "2023-12-31"):
        try:
            resolve_version(bad, None)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    assert latest["amount_cents"] == 1999          # source not mutated
    print("date-based version rendering checks passed")
```

## Deprecation and Sunset headers

Two standard response headers tell clients, and their monitoring, that something is going away:

```http
HTTP/1.1 200 OK
Deprecation: @1796083200
Sunset: Wed, 30 Jun 2027 00:00:00 GMT
Link: <https://developer.example.com/migrate/v2>; rel="deprecation"
```

- `Deprecation` (RFC 9745, 2025): when the resource is or will be deprecated, as a structured-field date in Unix seconds (`@1796083200` is 2026-12-01 UTC). Earlier drafts used `Deprecation: true`, which you will still see in the wild.
- `Sunset` (RFC 8594, 2019): the HTTP-date after which the resource is expected to stop responding.
- `Link` with `rel="deprecation"` (RFC 9745) or `rel="sunset"` (RFC 8594) points to migration docs.

Headers alone do nothing if nobody reads them. A retirement that actually finishes:

1. Announce with a date, changelog entry, and migration guide.
2. Emit the headers and log every call to the deprecated surface with client ID and version.
3. Contact the top consumers by traffic directly; usage data tells you who they are.
4. Run brownouts: scheduled short windows where the old endpoint fails, so forgotten integrations surface while there is still time. GitHub has publicly used brownouts before removing deprecated features.
5. At sunset, return `410 Gone` with a pointer to the replacement rather than `404`.

## Consumer-driven contracts

Schema checks tell you whether the provider's contract changed. They cannot tell you whether anyone depends on what changed. Consumer-driven contract testing (Pact is the best-known tool) inverts that:

```text
consumer CI                     broker                  provider CI
tests run against a     --->   stores each      --->   replays every
mock provider; records         consumer's              interaction against
the requests it makes          contract by             the real provider,
and fields it reads            version                 with provider states
                                  ^                         |
deploy gate: can-i-deploy? -------+----- verification ------+
```

A contract is a list of interactions the consumer actually exercises:

```json
{
  "consumer": { "name": "billing-ui" },
  "provider": { "name": "orders-api" },
  "interactions": [
    {
      "description": "a paid order",
      "providerState": "order ord_123 exists and is paid",
      "request": { "method": "GET", "path": "/v2/orders/ord_123" },
      "response": {
        "status": 200,
        "body": { "id": "ord_123", "status": "paid" }
      }
    }
  ]
}
```

The provider's CI fails if a change breaks any registered consumer, before deploy rather than in production. It also tells you which fields nobody uses, so they can be removed safely. It works for internal services and known partners; it cannot cover anonymous public clients, and provider states take real maintenance.

## OpenAPI spec structure

OpenAPI (formerly Swagger; "Swagger" now names SmartBear's tools such as Swagger UI) is the standard machine-readable description of HTTP APIs. Version 3.0 came in 2017; 3.1 (2021) aligned schemas with JSON Schema 2020-12 and added top-level `webhooks`.

```yaml
openapi: 3.1.0
info:
  title: Orders API
  version: 2.4.0            # version of this document, not the URL
servers:
  - url: https://api.example.com/v2
paths:
  /orders/{order_id}:
    get:
      operationId: getOrder  # becomes the method name in generated SDKs
      tags: [orders]
      parameters:
        - name: order_id
          in: path
          required: true
          schema: { type: string, pattern: "^ord_[A-Za-z0-9]+$" }
      responses:
        "200":
          description: The order
          headers:
            ETag: { schema: { type: string } }
          content:
            application/json:
              schema: { $ref: "#/components/schemas/Order" }
        "404":
          $ref: "#/components/responses/NotFound"
components:
  schemas:
    Order:
      type: object
      required: [id, status, amount_cents]
      properties:
        id: { type: string }
        status: { type: string, enum: [pending, paid, refunded] }
        amount_cents: { type: integer, minimum: 0 }
  responses:
    NotFound:
      description: No such order in this tenant
      content:
        application/problem+json:
          schema: { $ref: "#/components/schemas/Problem" }
  securitySchemes:
    bearerAuth: { type: http, scheme: bearer, bearerFormat: JWT }
security:
  - bearerAuth: []
```

The top-level keys: `openapi`, `info`, `servers`, `paths` (operations with parameters, request bodies, responses), `components` (reusable `schemas`, `parameters`, `responses`, `requestBodies`, `securitySchemes`, `examples`), `security`, `tags`, and in 3.1 `webhooks`.

## Code-first vs design-first

| | Design-first | Code-first |
|---|---|---|
| Source of truth | Hand-written spec in the repo | Code; spec generated from it |
| Review | API reviewed as a spec PR before code | Reviewed through code and generated diff |
| Parallel work | Frontend builds against a mock server from day one | Frontend waits for an endpoint or a stub |
| Drift risk | Code can diverge from spec unless validated | Spec matches code, but only as precise as annotations |
| Speed | Slower start | Fast for teams already writing serializers |
| Best for | Public APIs, cross-team contracts | Internal APIs in DRF, FastAPI and similar |

Either way, commit the generated or hand-written spec, review its diff in every PR, and validate the running service against it. The workflow matters more than the direction.

## drf-spectacular

The standard OpenAPI 3 generator for Django REST framework. It introspects views, serializers, filters, pagination and DRF's versioning classes. Illustrative setup:

```python
# settings.py
INSTALLED_APPS += ["drf_spectacular"]
REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.URLPathVersioning",
    "ALLOWED_VERSIONS": ["v1", "v2"],
    "DEFAULT_VERSION": "v2",
}
SPECTACULAR_SETTINGS = {
    "TITLE": "Orders API",
    "VERSION": "2.4.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,   # separate request/response schemas
}

# urls.py
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema")),
    re_path(r"^api/(?P<version>v1|v2)/", include(router.urls)),
]
```

Annotate what introspection cannot see: custom headers, non-serializer responses, `SerializerMethodField` return types.

```python
from drf_spectacular.utils import (
    OpenApiParameter, OpenApiResponse, extend_schema)


class ExportViewSet(viewsets.ViewSet):
    @extend_schema(
        summary="Start an orders export",
        request=ExportRequestSerializer,
        responses={202: OperationSerializer,
                   422: OpenApiResponse(description="Invalid filter")},
        parameters=[OpenApiParameter("Idempotency-Key", str,
                                     OpenApiParameter.HEADER, required=True)],
        tags=["exports"],
    )
    def create(self, request, version=None):
        ...
```

## Generated clients

Once the spec is trustworthy, clients come for free: openapi-generator (dozens of languages), openapi-typescript or orval for TypeScript frontends, oapi-codegen for Go, Kiota for several languages. Practices that keep them useful:

- Generate in CI whenever the spec changes and publish a versioned package; consumers upgrade like any dependency.
- Pin the generator version; its output changes between releases.
- Keep `operationId` stable. drf-spectacular derives it from the path and method by default; renaming a route renames the SDK method and breaks every caller.
- Configure enums as open (unknown values tolerated) or new enum values become breaking changes for generated clients.

## Docs as tests

A spec is only worth publishing if CI proves it matches the service.

- **Spec validity**: `manage.py spectacular --validate --fail-on-warn` fails on schema errors and unresolved warnings.
- **Drift**: regenerate and `git diff --exit-code`; an uncommitted spec change means a contract change nobody reviewed.
- **Breaking-change detection**: diff the PR's spec against `main` with a tool such as oasdiff and fail on breaking changes unless the PR is labeled as a versioned break.
- **Property-based API testing**: Schemathesis generates requests from the spec and checks that responses match the documented schemas and status codes, and that nothing returns `500`.
- **Response validation in unit tests**: validate responses from the test client against the spec, so a serializer change that drifts from the docs fails a test.

## In practice

Illustrative CI step (tool flags are version-sensitive):

```bash
python manage.py spectacular --file openapi.yaml --validate --fail-on-warn
git diff --exit-code openapi.yaml          # spec changed but not committed
git show origin/main:openapi.yaml > /tmp/base-openapi.yaml
oasdiff breaking /tmp/base-openapi.yaml openapi.yaml --fail-on ERR
python manage.py runserver 0.0.0.0:8000 &
schemathesis run http://localhost:8000/api/schema/
```

Version-specific serializers with DRF's `URLPathVersioning` keep one view and one queryset:

```python
class OrderViewSet(viewsets.ModelViewSet):
    def get_queryset(self):
        return Order.objects.filter(tenant=self.request.tenant)

    def get_serializer_class(self):
        if self.request.version == "v1":
            return OrderSerializerV1      # flat customer_id, float amount
        return OrderSerializerV2
```

Deprecation headers for the whole `v1` surface, plus usage logging so you know who still calls it:

```python
import logging

log = logging.getLogger("api.deprecated")


class DeprecationHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith("/api/v1/"):
            response["Deprecation"] = "@1796083200"      # 2026-12-01
            response["Sunset"] = "Wed, 30 Jun 2027 00:00:00 GMT"
            response["Link"] = ('<https://developer.example.com/migrate/v2>;'
                                ' rel="deprecation"')
            log.info("v1 call", extra={"client_id": getattr(
                request, "client_id", None), "path": request.path})
        return response
```

## Pitfalls

- **Versioning too eagerly.** Five live major versions means five code paths to patch for every security fix. Evolve additively; version only real breaks.
- **Forking code per version.** Copy-pasted `v1` and `v2` views drift; fixes land in one. Share the domain layer and vary only serializers or transforms at the edge.
- **"Minor" changes that break.** Making a field required, tightening validation, adding an enum value for closed-enum clients, changing default sort order.
- **No per-client usage data.** You cannot sunset what you cannot measure. Log version and client ID on every request.
- **Missing `Vary`.** A CDN caches the `v2` response under the shared URL and serves it to `v1` clients.
- **Unversioned webhooks.** Payload changes break consumers' handlers with no header to negotiate. Version event payloads like responses.
- **Spec drift and `operationId` churn.** Docs promise one thing, code does another; SDK method names change on refactors. Generate, commit, diff and test the spec in CI.

## Interview questions

**Which versioning strategy would you choose for a public API?** A major version in the URL for rare, large breaks, additive evolution within it, and optionally date-based minor versions pinned per account for finer-grained changes. URL versions are easiest for clients, gateways and caches.

**What counts as a breaking change?** Anything an existing correct client can observe failing: removed or renamed fields, type or unit changes, new required inputs, tighter validation, changed status or error codes, changed defaults or ordering, and new enum values if clients treat enums as closed.

**How do you retire an endpoint used by 300 partners?** Announce with a date and migration guide, emit `Deprecation` and `Sunset` headers, log usage per client, contact the heaviest users, run brownouts, and return `410 Gone` after the sunset date.

**How does Stripe-style date versioning work?** Accounts are pinned to the version at first use; a header can override it. Handlers implement only the latest behavior; ordered transforms convert requests up and responses down for older versions. Behavior changes need explicit version checks.

**Consumer-driven contracts vs OpenAPI diffing?** OpenAPI diffs detect structural changes to the contract; consumer-driven contracts detect whether a change breaks an actual consumer's usage. Diffing works for public APIs; contracts work for known consumers and also reveal unused fields.

**Code-first or design-first?** Design-first when the contract is a cross-team or public deliverable that should be reviewed before implementation; code-first for internal APIs where the framework already describes the shape. Either way, commit the spec and test the service against it.

**How do you keep documentation accurate?** Generate or validate the spec in CI, fail on drift and on breaking diffs, run schema-driven tests such as Schemathesis, and validate test responses against the spec.

## Further reading

- *OpenAPI Specification* v3.1.0 (OpenAPI Initiative).
- RFC 8594, *The Sunset HTTP Header Field*, and RFC 9745, *The Deprecation HTTP Response Header Field*.
- Brandur Leach, *APIs as infrastructure: future-proofing Stripe with versioning* (Stripe engineering blog, 2017).
- Pact documentation (docs.pact.io): consumer-driven contract testing.
- Google AIP-180 (backwards compatibility) and AIP-185 (API versioning); drf-spectacular documentation.
