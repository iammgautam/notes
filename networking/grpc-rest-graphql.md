---
title: gRPC vs REST vs GraphQL
section: networking
order: 9
summary: How REST, gRPC and GraphQL differ on contract, transport, performance, caching and tooling, and which to choose for public, internal and frontend APIs.
---

These are three ways to define the contract between a client and a server. None wins everywhere. REST is the default for public and CRUD-heavy APIs, gRPC shines between internal services, and GraphQL helps when many different frontends need different slices of a rich graph. Picking one is mostly about who the clients are and who controls them.

## How it works

### REST

Resources at URLs, HTTP methods for verbs, JSON bodies, HTTP status codes for outcomes. Contract usually described with OpenAPI. Every intermediary (browser, CDN, proxy, curl) understands it. See [REST API design]({{ '/networking/rest-api-design/' | relative_url }}).

### gRPC

You define services and messages in Protocol Buffers; code generators produce typed clients and servers in each language.

```text
syntax = "proto3";

service Billing {
  rpc GetInvoice (GetInvoiceRequest) returns (Invoice);
  rpc StreamUsage (UsageQuery) returns (stream UsageEvent);
}

message GetInvoiceRequest { string tenant_id = 1; string invoice_id = 2; }
message Invoice {
  string id = 1;
  int64 amount_minor = 2;
  string currency = 3;
  repeated LineItem items = 4;
}
```

- Runs over **HTTP/2**: multiplexed streams on one connection, binary framing.
- **Protobuf** encoding is compact and fast to parse (often 3–10× smaller than JSON).
- Four call types: unary, server streaming, client streaming, bidirectional streaming.
- First-class **deadlines** propagated across hops, plus cancellation and status codes (`DEADLINE_EXCEEDED`, `UNAVAILABLE`, `NOT_FOUND`).
- Field numbers are the wire contract: you can add fields freely, never reuse or renumber them.

Browsers can't speak native gRPC (they don't expose HTTP/2 trailers), so browser clients need gRPC-Web or Connect through a proxy.

### GraphQL

One endpoint (`POST /graphql`), a typed schema, and clients ask for exactly the fields they need:

```text
query ProjectPage($id: ID!) {
  project(id: $id) {
    name
    owner { name avatarUrl }
    tasks(first: 20, status: OPEN) {
      edges { node { id title assignee { name } } }
      pageInfo { endCursor hasNextPage }
    }
  }
}
```

The server executes a **resolver** per field. That solves over-fetching and under-fetching for frontends, and moves complexity to the server: N+1 resolver queries, query cost limits, authorization per field, and caching.

### Comparison

| | REST | gRPC | GraphQL |
|---|---|---|---|
| Contract | OpenAPI (optional) | `.proto` (required) | Schema (required) |
| Transport | HTTP/1.1 or 2 | HTTP/2 | HTTP, usually POST |
| Payload | JSON | Protobuf binary | JSON |
| Browser support | native | via gRPC-Web / proxy | native |
| HTTP caching / CDN | excellent | none | poor (POST, one URL) |
| Streaming | SSE / WebSocket | built in | subscriptions (WebSocket) |
| Code generation | optional | core workflow | common for clients |
| Debuggability | curl, browser | grpcurl, needs reflection | GraphiQL, but one URL in logs |
| Errors | HTTP status | gRPC status codes | 200 with `errors` array |
| Best for | public APIs, CRUD | internal service-to-service | many frontends over a rich graph |

## In practice

### Choosing

- **Public or partner API**: REST. Universal tooling, cacheable, easy auth, every customer can call it with curl.
- **Internal, high-volume, polyglot microservices**: gRPC. Typed contracts, generated clients, deadlines, streaming, less CPU on serialization.
- **Web and mobile apps with varied, nested data needs** and a team willing to own the server complexity: GraphQL, often as a gateway in front of REST or gRPC services.
- **A Django monolith with a Vue frontend**: REST with DRF is almost always enough. Add fields and dedicated endpoints for heavy screens before reaching for GraphQL.

### gRPC in Python

```python
import grpc
from billing_pb2 import GetInvoiceRequest
from billing_pb2_grpc import BillingStub

channel = grpc.insecure_channel("billing:50051")   # reuse; channels are pooled
stub = BillingStub(channel)

try:
    inv = stub.GetInvoice(
        GetInvoiceRequest(tenant_id="t_7", invoice_id="inv_88"),
        timeout=0.5,                                  # deadline, seconds
        metadata=[("x-request-id", request_id)],
    )
except grpc.RpcError as e:
    if e.code() == grpc.StatusCode.NOT_FOUND:
        ...
```

```bash
python -m grpc_tools.protoc -I proto --python_out=. --grpc_python_out=. proto/billing.proto
grpcurl -plaintext billing:50051 list
grpcurl -plaintext -d '{"tenant_id":"t_7","invoice_id":"inv_88"}' billing:50051 billing.Billing/GetInvoice
```

Use TLS (`grpc.secure_channel`) or a service mesh with mTLS outside a trusted network.

### GraphQL in Django (Strawberry or Graphene)

The two things you must add on day one:

1. **DataLoader** batching so `tasks { assignee { name } }` issues one `WHERE id IN (...)` instead of one query per task.
2. **Query limits**: max depth, max complexity or cost, and pagination caps, or a single query can ask for `projects { tasks { comments { author { projects ... } } } }` and melt the database.

Also: disable introspection in production if the API is private, and use **persisted queries** (clients send a hash, server only runs known queries) for both security and CDN caching via GET.

### Load balancing gRPC

gRPC keeps one long-lived HTTP/2 connection and multiplexes everything over it. An L4 load balancer balances **connections**, not requests, so one client sends all traffic to one backend. Use an L7 proxy that understands HTTP/2 (Envoy, Linkerd, ALB with gRPC target groups) or client-side load balancing with periodic re-resolution.

## Pitfalls

- **GraphQL N+1**: naive resolvers produce hundreds of queries per request. DataLoaders are mandatory.
- **GraphQL errors with 200 OK**: monitoring that only watches status codes sees no failures. Parse `errors` and emit metrics per operation name.
- **GraphQL authorization**: checking permissions only on the top-level query leaks data through nested fields. Authorize at the object or resolver level.
- **gRPC without deadlines**: a slow dependency holds requests forever. Always set a timeout and propagate it.
- **Proto evolution mistakes**: changing a field's type or reusing a number silently corrupts data for old clients. Mark removed fields `reserved`.
- **gRPC and L4 load balancers**: uneven load, one hot pod.
- **Choosing by hype**: introducing gRPC or GraphQL for a small team and one frontend adds build tooling and operational cost for little gain.

## Rules of thumb

- REST by default, especially at the edge and for third parties.
- gRPC inside the platform when you have many services, many languages, or streaming needs.
- GraphQL when frontend data needs are diverse and changing, and you can afford DataLoaders, cost limits and per-field auth.
- Whatever you pick: typed contract, generated docs, contract checks in CI, deadlines on every call.

## Interview angle

- **"Why is gRPC faster than REST?"** Binary Protobuf instead of JSON, HTTP/2 multiplexing over persistent connections, header compression, and generated code that avoids reflection-based parsing. The gain matters at high request volumes, less for a few calls per page.
- **"What problem does GraphQL solve, and what does it cost?"** Clients fetch exactly what they need in one round trip. The cost is server complexity: N+1 resolvers, query cost control, harder caching and per-field authorization.
- **"Which would you use for a public API?"** REST with OpenAPI: universally consumable, cacheable, simple auth and debugging.
