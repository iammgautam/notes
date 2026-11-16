---
title: GraphQL
section: backend
order: 19
phase: "Phase 3 · API design"
summary: Schema, operations and resolvers, N+1 and DataLoader, cost limits, persisted queries, caching, federation and field-level auth, and when to use GraphQL.
---

GraphQL moves the shape of the response from the server to the client. One endpoint, one typed schema, and each client asks for exactly the fields it needs. That fixes over-fetching and the "five REST calls per screen" problem for mobile and web apps, and it gives frontends a typed contract. It also moves problems onto the backend that REST handles for free: query cost is now client-controlled, HTTP caching mostly stops working, and a naive resolver tree issues hundreds of SQL queries per request.

## Schema and type system

The schema is the contract, written in SDL. Every field has a type; `!` means non-null; arguments live on fields; `input` types carry mutation arguments.

```graphql
type Query {
  order(id: ID!): Order
  orders(first: Int = 20, after: String, status: OrderStatus): OrderConnection!
}

type Mutation {
  cancelOrder(input: CancelOrderInput!): CancelOrderPayload!
}

type Subscription {
  orderStatusChanged(orderId: ID!): Order!
}

type Order {
  id: ID!
  status: OrderStatus!
  totalCents: Int!
  customer: Customer!
  lineItems(first: Int = 50): [LineItem!]!
}

enum OrderStatus { PENDING PAID SHIPPED CANCELED }

input CancelOrderInput {
  orderId: ID!
  reason: String
}

type CancelOrderPayload {
  order: Order
  userErrors: [UserError!]!
}
```

Conventions worth adopting: Relay-style connections (`edges`, `node`, `pageInfo`, opaque cursors) for lists ([Pagination]({{ '/backend/pagination/' | relative_url }})); one `input` object per mutation and a payload type with `userErrors` for expected business failures; global object IDs if clients use a normalized cache. Clients discover the schema through introspection, which powers codegen and IDE tooling.

## Queries, mutations and subscriptions

**Queries** read. The client selects fields, and the response mirrors the selection:

```graphql
query OrderPage($id: ID!) {
  order(id: $id) {
    id
    status
    customer {
      name
    }
    lineItems(first: 10) {
      sku
      quantity
    }
  }
}
```

**Mutations** write. Top-level mutation fields execute serially in document order; query fields may execute in parallel. Return the changed object so the client cache updates without a refetch.

**Subscriptions** push events over a long-lived connection, usually the `graphql-transport-ws` protocol over WebSocket (the older `subscriptions-transport-ws` protocol is deprecated) or Server-Sent Events. The server keeps a connection per client, listens to an event source (Redis pub/sub, Kafka, Postgres `LISTEN/NOTIFY`), and runs the subscription's selection set once per event per subscriber. Filter events before resolving, keep payloads small, and plan for sticky connections and reconnect storms like any other [real-time system]({{ '/backend/real-time-communication/' | relative_url }}).

Transport details: requests are usually `POST /graphql` with `{"query", "variables", "operationName"}`. The response is `{"data": ..., "errors": [...]}`, and partial success is normal: a failed field becomes `null` with an entry in `errors` while the rest of the data still returns, typically with HTTP `200`.

```json
{
  "data": {
    "order": {
      "id": "ord_123",
      "marginCents": null
    }
  },
  "errors": [
    {
      "message": "Not allowed to view revenue",
      "path": ["order", "marginCents"],
      "extensions": { "code": "FORBIDDEN" }
    }
  ]
}
```

## Resolvers and execution

A resolver is a function `(parent, args, context, info) -> value` attached to a field. Execution walks the query tree top-down: resolve `order`, then for the returned object resolve each selected child field, and so on. Fields without a custom resolver default to reading the attribute of the same name on the parent object.

Consequences:

- Every field is an independent call site, so cross-cutting concerns (auth, tenancy, logging) need a consistent home, usually the `context` built once per request.
- The cost of a query is the sum of all resolver calls, multiplied by list sizes. The server does not see a "query" in the SQL sense; it sees a tree of function calls.
- Non-null fields propagate errors upward: if a `String!` resolver fails, its parent becomes `null`, and so on up to the nearest nullable field. Making everything non-null makes one failing field blank a whole screen.

## N+1 and DataLoader

Resolve 50 orders, and the `customer` resolver runs 50 times, one query each. That is the N+1 problem from ORMs ([Query optimization and N+1]({{ '/backend/query-optimization-and-n-plus-one/' | relative_url }})), except the client controls the shape, so you cannot fix it with one well-placed `select_related`.

```text
query { orders(first: 50) { id customer { name } } }

without DataLoader               with DataLoader
orders resolver -> 1 query       orders resolver -> 1 query
customer x 50   -> 50 queries    customer x 50   -> load(id) x 50
                                 end of tick     -> 1 query
                                   WHERE id = ANY(...)
total: 51 queries                total: 2 queries
```

DataLoader fixes it with two tricks. **Batching**: `load(key)` returns a future instead of querying; after the current tick of the event loop, all keys collected so far go to one batch function (`WHERE id = ANY(%s)`). **Memoization**: the same key within a request returns the same future, so customer 7 is fetched once even if it appears 20 times. The loader must be created per request: a shared loader caches across users and leaks data between tenants.

The batch function has a strict contract: return exactly one value per key, in key order, with `None` (or an error) for missing keys. Each level of nesting adds one batch, so query count scales with query depth, not with the number of rows.

A runnable model of the mechanism with asyncio:

```python
import asyncio


class DataLoader:
    """Request-scoped batching and memoization (the graphql/dataloader idea)."""

    def __init__(self, batch_fn):
        self.batch_fn = batch_fn     # async fn(keys) -> values in key order
        self.cache = {}              # key -> future, lives for one request
        self.pending = []            # keys waiting for the next dispatch
        self.task = None

    def load(self, key):
        if key in self.cache:
            return self.cache[key]
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.cache[key] = fut
        self.pending.append(key)
        if len(self.pending) == 1:
            # The dispatch task runs after every resolver already scheduled
            # has reached its first await, so their keys share one batch.
            self.task = loop.create_task(self._dispatch())
        return fut

    async def _dispatch(self):
        keys, self.pending = self.pending, []
        try:
            values = await self.batch_fn(keys)
            if len(values) != len(keys):
                raise ValueError("batch_fn must return one value per key")
        except Exception as exc:
            for key in keys:
                self.cache.pop(key).set_exception(exc)
            return
        for key, value in zip(keys, values):
            self.cache[key].set_result(value)


class FakeDB:
    def __init__(self):
        self.queries = []
        self.users = {i: dict(id=i, name=f"user{i}") for i in range(1, 11)}
        self.posts = [dict(id=p, author_id=p % 5 + 1, editor_id=p % 5 + 4)
                      for p in range(1, 51)]

    async def user_by_id(self, user_id):
        self.queries.append([user_id])
        await asyncio.sleep(0)                   # pretend network I/O
        return self.users.get(user_id)

    async def users_by_ids(self, ids):           # WHERE id = ANY(%s)
        self.queries.append(sorted(ids))
        await asyncio.sleep(0)
        return [self.users.get(i) for i in ids]


async def resolve_feed(db, get_user):
    async def resolve_post(post):                # one resolver per post
        author = await get_user(post["author_id"])
        editor = await get_user(post["editor_id"])   # second "level"
        return dict(id=post["id"], author=author["name"],
                    editor=editor["name"])
    return await asyncio.gather(*(resolve_post(p) for p in db.posts))


async def main():
    naive_db = FakeDB()
    naive = await resolve_feed(naive_db, naive_db.user_by_id)
    assert len(naive_db.queries) == 100          # 2 lookups x 50 posts

    db = FakeDB()
    users = DataLoader(db.users_by_ids)          # new loader per request
    batched = await resolve_feed(db, users.load)
    assert batched == naive
    assert db.queries == [[1, 2, 3, 4, 5], [6, 7, 8]]  # 4, 5 were cached
    print("100 queries without DataLoader,", len(db.queries), "with it")


if __name__ == "__main__":
    asyncio.run(main())
```

The second batch contains only users 6–8 because 4 and 5 were already loaded as authors. A resolver that awaits something unrelated before calling `load` lands in a later batch; that is the main way batching quietly degrades.

## Query complexity limits

A public GraphQL endpoint accepts arbitrary programs. Without limits, one request can ask for `orders(first: 100) { lineItems(first: 50) { product { reviews(first: 20) { ... } } } }`, which is up to 100 × 50 × 20 = 100,000 objects. Defenses, layered:

| Control | Stops | Notes |
|---|---|---|
| Max depth | Deeply nested and cyclic queries | Cheap; misses wide queries |
| Mandatory, capped `first`/`last` | Unbounded lists | Reject list fields without a size |
| Cost analysis | Wide and multiplicative queries | Static score before execution: field weights times list sizes |
| Alias and token limits | `a1: order(id: 1) ... a500: order(id: 500)` | Aliases multiply work without depth |
| Batch limits | Arrays of operations in one HTTP request | Cap or disable batching |
| Timeouts and statement timeouts | Anything that slipped through | Per request and per SQL statement |
| Cost-based rate limiting | Many cheap-looking requests | Charge points, not requests |

GitHub's public GraphQL API is a well-documented example: as of 2025 it requires `first` or `last` on connections (1 to 100), caps the total nodes a single call can request, and rate-limits by calculated points per hour rather than requests. Shopify's Admin API similarly charges a calculated query cost against a leaky bucket. Disabling introspection in production does not stop any of this; it only hides the schema from casual browsing.

## Persisted queries

Instead of sending the query text, the client sends a hash of it.

**Automatic persisted queries (APQ)** are a cache: the client sends the SHA-256 hash; on a miss the server answers `PersistedQueryNotFound`, the client resends with the full text, and the server stores it. Requests shrink, and because the hash and variables fit in a URL, queries can go over `GET` and be cached by a CDN.

```text
client                               server / CDN
  | GET /graphql?extensions={hash}      |
  |------------------------------------>|  hash unknown
  | 200 errors: PersistedQueryNotFound  |
  |<------------------------------------|
  | POST /graphql {query, hash}         |
  |------------------------------------>|  check sha256(query) == hash
  | 200 data                            |  store hash -> query
  |<------------------------------------|
  | GET /graphql?extensions={hash}      |  later: small GET,
  |------------------------------------>|  CDN-cacheable by URL
```

**Trusted documents** (an allowlist) go further: the build pipeline extracts every operation from the client code, registers the hashes, and the server rejects anything else. For first-party APIs this is the strongest complexity defense, because arbitrary queries are simply not accepted. It does not work for public APIs where third parties write their own queries.

## Caching challenges

REST gets caching from HTTP: one URL per resource, `GET`, `Cache-Control`, `ETag`. GraphQL breaks most of that:

- One endpoint, usually `POST`, so CDNs and browsers do not cache by default.
- Every query has a different shape, so two clients asking about the same order produce different responses.
- Partial errors arrive with `200`, so caching layers cannot tell success from failure by status.
- A response is only as cacheable as its least cacheable field; one per-user field makes the whole response private.

What works instead:

- **Client-side normalized caches** (Apollo Client, Relay) split responses into objects keyed by `__typename` and `id`, so an update to `Order:ord_123` from a mutation updates every screen showing it. This is why stable IDs matter.
- **Persisted queries over `GET`** plus per-field cache hints (for example Apollo Server's `@cacheControl(maxAge: 60)`, where the response takes the minimum across fields) restore CDN caching for public data.
- **Resolver-level caching**: cache entities in Redis inside the data layer, under the DataLoader. This is usually the simplest server-side win because it is independent of query shape.
- DataLoader's memoization is not a cache in this sense; it lives for one request.

See [Caching fundamentals]({{ '/backend/caching-fundamentals/' | relative_url }}) for the layers themselves.

## Federation

One GraphQL schema over many services. Each team owns a **subgraph** with part of the schema; a **router** composes them into a supergraph, plans each incoming query, and fans out to subgraphs. Netflix has publicly described moving its API to federated GraphQL; Apollo Federation is the most widely used specification, and several open-source and commercial routers implement it.

```text
                 client
                   |
           +-------v-------+     query plan
           |    router     |-------------------------+
           +---+-------+---+                         |
               |       |                             |
     +---------v--+  +-v-----------+      +----------v--+
     |  orders    |  |  customers  |      |  inventory  |
     |  subgraph  |  |  subgraph   |      |  subgraph   |
     +------------+  +-------------+      +-------------+
```

Entities are types that several subgraphs contribute to, identified by a `@key`:

```graphql
# Each subgraph starts with:
# extend schema @link(url: "https://specs.apollo.dev/federation/v2.3",
#                     import: ["@key"])

# customers subgraph owns the core fields
type Customer @key(fields: "id") {
  id: ID!
  name: String!
}

# orders subgraph adds a field to Customer and owns Order
type Customer @key(fields: "id") {
  id: ID!
  orders(first: Int = 20): [Order!]!
}

type Order @key(fields: "id") {
  id: ID!
  totalCents: Int!
  customer: Customer!
}
```

For `order { totalCents customer { name } }` the router asks the orders subgraph for the order and a customer reference (`__typename`, `id`), then calls the customers subgraph's `_entities` field with the list of references to fill `name`. Each subgraph's reference resolver must batch, or the N+1 problem reappears across the network. Federation fits when many teams own parts of one graph; for a single team, one GraphQL service acting as a [BFF]({{ '/backend/api-gateway-and-bff/' | relative_url }}) over REST or gRPC backends is far simpler.

## Authorization at field level

Authentication happens once per request (a session or JWT checked in middleware, the user placed in `context`). Authorization happens per field, because any field can be reached through many paths: `order.customer`, `node(id: ...)`, `customer.orders`, a federated entity lookup.

Rules:

- Put the actual checks in the business layer that resolvers call (`OrderService.get(user, id)`), not only in schema directives. That way every path, including REST endpoints sharing the service, enforces the same rule.
- Scope every data fetch by tenant inside loaders and services. A DataLoader keyed only by ID will happily return another tenant's row through `node(id:)`.
- Field-level permissions (`@auth(requires: ADMIN)`-style directives or permission classes) are fine for coarse rules such as "only finance sees `marginCents`". Unauthorized nullable fields resolve to `null` with an error; decide whether that is preferable to failing the whole query.
- Error messages and validation hints leak schema. Many servers suggest field names ("Did you mean `marginCents`?") even with introspection disabled; turn suggestions off for public endpoints if the schema is sensitive.

## When to choose GraphQL

| Choose GraphQL | Prefer REST or gRPC |
|---|---|
| Many client types with different data needs | Simple CRUD with one client |
| Screens that aggregate many services (BFF) | Public API where HTTP caching matters |
| Fast-moving UI teams wanting a typed contract | Service-to-service, high throughput (gRPC) |
| Deep, connected domain (graph of entities) | File upload and download, binary streams |
| You can invest in cost limits and tooling | Small team, no capacity for the extra ops |

The common working setup: GraphQL at the edge for first-party apps, REST for public and partner APIs, gRPC between internal services. The trade-off across all four styles is tabulated in [gRPC and SOAP]({{ '/backend/grpc-and-soap/' | relative_url }}).

## In practice

Illustrative Strawberry setup on Django (version-sensitive: Strawberry's API moves quickly). Per-request loaders live in the context; a permission class guards a sensitive field; depth is capped.

```python
import strawberry
from strawberry.dataloader import DataLoader
from strawberry.django.views import AsyncGraphQLView
from strawberry.extensions import QueryDepthLimiter
from strawberry.permission import BasePermission
from strawberry.types import Info


async def load_customers(keys: list[int]):
    rows = {c.id: c async for c in CustomerModel.objects.filter(id__in=keys)}
    return [rows.get(k) for k in keys]          # one value per key, in order


class CanSeeRevenue(BasePermission):
    message = "Not allowed to view revenue"

    def has_permission(self, source, info: Info, **kwargs) -> bool:
        return info.context["request"].user.has_perm("billing.view_revenue")


@strawberry.type
class Customer:
    id: strawberry.ID
    name: str


@strawberry.type
class Order:
    id: strawberry.ID
    total_cents: int
    customer_id: strawberry.Private[int]
    margin_cents: int = strawberry.field(permission_classes=[CanSeeRevenue])

    @strawberry.field
    async def customer(self, info: Info) -> Customer:
        return await info.context["customer_loader"].load(self.customer_id)


@strawberry.type
class Query:
    @strawberry.field
    async def orders(self, info: Info, first: int = 20) -> list[Order]:
        tenant = info.context["request"].tenant
        qs = OrderModel.objects.filter(tenant=tenant).order_by("-id")
        return [o async for o in qs[: min(first, 100)]]   # capped list


schema = strawberry.Schema(query=Query,
                           extensions=[QueryDepthLimiter(max_depth=8)])


class GraphQLView(AsyncGraphQLView):
    async def get_context(self, request, response):
        return {
            "request": request,
            "response": response,
            "customer_loader": DataLoader(load_fn=load_customers),
        }
```

`load_customers` should also filter by the request's tenant; it is omitted here only to keep the loader signature short.

## Pitfalls

- **Loader shared across requests.** Cached results from user A served to user B. Create loaders in `get_context`.
- **Batch function returns rows in database order** instead of key order, or skips missing keys. Wrong customers attached to orders, silently. Map by ID and return one entry per key.
- **No list caps.** A single query fetches every order of the largest tenant. Require and cap `first`.
- **Everything non-null.** One flaky downstream field nulls the whole response.
- **Monitoring by HTTP status.** Everything is `200`; error rates look perfect while resolvers fail. Track `errors[]` by `extensions.code` and per-field resolver latency in traces.
- **Auth only in the gateway or only on root fields.** Nested fields and `node(id:)` bypass it.
- **Mutations returning `Boolean`.** Clients must refetch; return the updated object and `userErrors`.
- **Treating introspection-off as security.** It is obscurity; cost limits and trusted documents are the controls.

## Interview questions

**What is the N+1 problem in GraphQL and how does DataLoader fix it?** Resolvers run per object, so a list of N parents triggers N child queries. DataLoader collects `load(key)` calls made in the same event-loop tick, issues one batched query per field per level, and memoizes by key within the request. Query count becomes proportional to depth, not row count.

**How do you protect a public GraphQL API from expensive queries?** Static cost analysis with list-size multipliers, mandatory capped page sizes, depth and alias limits, request and statement timeouts, cost-based rate limits, and trusted documents for first-party clients.

**Why is caching harder than with REST, and what do you do?** Single `POST` endpoint, variable response shapes, partial errors with `200`. Use normalized client caches keyed by type and ID, persisted queries over `GET` with field cache hints for CDN caching, and entity caching in the data layer.

**Where should authorization live?** In the business layer every resolver calls, scoped by tenant, so every path to an object enforces it. Field directives are fine for coarse role checks on top.

**How does federation resolve a field owned by another service?** The router fetches the parent from the owning subgraph, including key fields, then calls the other subgraph's `_entities` with a batch of representations (`__typename` plus key) and merges the results.

**Why does GraphQL return 200 with errors?** Execution is per field; a failure in one field does not invalidate the others, so the response carries partial `data` plus `errors`. Transport-level failures (unparseable request, auth failure at the gateway) can still use 4xx.

**When would you not use GraphQL?** Simple CRUD, public APIs that benefit from HTTP caching, high-throughput service-to-service calls, and file transfer.

## Further reading

- *GraphQL Specification*, October 2021 edition (graphql.org).
- The `graphql/dataloader` repository README, which defines the batching and caching contract.
- Apollo Federation documentation: entities, `@key`, query planning.
- *GraphQL Cursor Connections Specification* (Relay).
- Marc-André Giroux, *Production Ready GraphQL* (2020).
