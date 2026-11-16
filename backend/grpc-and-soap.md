---
title: gRPC and SOAP
section: backend
order: 20
phase: "Phase 3 · API design"
summary: gRPC's protobuf, HTTP/2, streaming, deadlines, status codes and load balancing; SOAP's WSDL and WS-Security; and REST vs GraphQL vs gRPC vs SOAP.
---

REST and GraphQL are document-oriented: you exchange representations of resources. gRPC and SOAP are RPC-oriented: you call typed operations defined in a contract, and code generators give you client and server stubs. gRPC is the modern default for internal service-to-service traffic: binary, multiplexed, streaming, strongly typed. SOAP is its XML-era predecessor that still runs a large share of banking, government and enterprise integrations, so a backend engineer meets it whenever a partner says "here is our WSDL".

## gRPC overview

gRPC is an RPC framework open-sourced by Google in 2015, now a CNCF project. You define services and messages in a `.proto` file, generate code for each language, and call remote methods like local ones. Underneath: Protocol Buffers for serialization, HTTP/2 for transport, and a small protocol that maps each call to one HTTP/2 stream.

## Protocol Buffers

Protobuf is a schema-first binary format. Each field has a name, a type and a **field number**; only the number goes on the wire.

```protobuf
syntax = "proto3";

package shop.orders.v1;

import "google/protobuf/timestamp.proto";

service OrderService {
  rpc GetOrder(GetOrderRequest) returns (Order);
  rpc WatchOrders(WatchOrdersRequest) returns (stream OrderEvent);
  rpc UploadLineItems(stream LineItem) returns (UploadSummary);
  rpc SyncCart(stream CartOp) returns (stream CartState);
}

message GetOrderRequest {
  string id = 1;
}

message Order {
  string id = 1;
  int64 total_cents = 2;
  OrderStatus status = 3;
  google.protobuf.Timestamp created_at = 4;
  optional string note = 6;     // explicit presence: unset vs ""
  reserved 5;                   // was coupon_code; never reuse
  reserved "coupon_code";
}

enum OrderStatus {
  ORDER_STATUS_UNSPECIFIED = 0; // proto3 default; never a real state
  ORDER_STATUS_PENDING = 1;
  ORDER_STATUS_PAID = 2;
}
```

Each field is encoded as a key (`field_number << 3 | wire_type`) followed by the value. Integers are varints (7 bits per byte, high bit means "more"), strings and nested messages are length-prefixed. Readers skip fields they do not know, which is the basis of protobuf's compatibility:

| Change | Safe? |
|---|---|
| Add a field with a new number | Yes; old readers skip it |
| Remove a field and `reserved` its number and name | Yes |
| Rename a field (same number and type) | Wire-safe; breaks JSON mapping and generated code |
| Reuse an old field number | No; old data decodes into the wrong field |
| Change `int32` to `int64` | Wire-compatible for varints; truncation risk in old readers |
| Change `int32` to `string` | No |
| Add an enum value | Yes, but old clients see an unknown value |

Proto3 scalars have no presence by default: `0`, `""` and "not set" look identical. Use `optional` (stable since protobuf 3.15) or wrapper types when the difference matters, such as a PATCH-style update. Enforce the rules in CI with a breaking-change checker such as `buf breaking`.

## HTTP/2 transport

gRPC rides on HTTP/2: one long-lived TCP connection carries many concurrent **streams**, each RPC is one stream, and headers are compressed with HPACK. A unary call looks like this on the wire:

```text
client                                                    server
  | HEADERS  :method POST                                    |
  |          :path /shop.orders.v1.OrderService/GetOrder     |
  |          content-type: application/grpc                  |
  |          grpc-timeout: 300m                              |
  | DATA     00 | 00 00 00 09 | <9 bytes GetOrderRequest>    |
  |--------------------------------------------------------->|
  |                  HEADERS  :status 200                    |
  |                  DATA     00 | 00 00 00 2a | <42 bytes>  |
  |                  HEADERS  (trailers) grpc-status: 0      |
  |<---------------------------------------------------------|
   other RPCs run at the same time as other streams on the
   same connection; no head-of-line blocking between them
   at the HTTP layer (TCP-level loss still stalls all)
```

Every message is framed with a 1-byte compressed flag and a 4-byte big-endian length. The HTTP status is almost always `200`; the real outcome travels in **trailers** (`grpc-status`, `grpc-message`), which is exactly what browsers cannot read and why gRPC-Web exists. HTTP/2 flow-control windows give per-stream backpressure for free ([Backpressure]({{ '/backend/backpressure/' | relative_url }})).

## Unary and streaming RPCs

| Kind | Shape | Typical use |
|---|---|---|
| Unary | 1 request, 1 response | Most calls: `GetOrder`, `CreatePayment` |
| Server streaming | 1 request, N responses | Change feeds, large result sets in chunks, LLM token streams |
| Client streaming | N requests, 1 response | Uploads, telemetry batches, bulk import |
| Bidirectional | N and N, independent | Chat, collaborative sync, interactive sessions |

Streams are cheap to open but long-lived ones have costs: they pin a connection to one backend, a deploy must drain them (servers send `GOAWAY`), the deadline covers the whole stream rather than each message, and each open stream holds a worker thread in synchronous Python servers. Use `grpc.aio` for many concurrent streams, and send application-level heartbeats or rely on HTTP/2 keepalive pings to detect dead peers.

## Deadlines and cancellation

A **deadline** is an absolute point in time by which the whole call must finish; a timeout is the client-side way to express it. The client sends it as the `grpc-timeout` header, the server sees the remaining time, and when it expires both sides get `DEADLINE_EXCEEDED` and the server's context is canceled.

Why it matters more than in REST:

- Many gRPC clients have **no default deadline**. A slow dependency then holds threads, connections and memory indefinitely, and the outage spreads upward.
- **Propagate** deadlines: if the edge gave a request 300 ms and 120 ms are spent, downstream calls get what is left (minus a margin), not their own fresh 1 s timeout. Go and Java do this when you pass the incoming context; in Python you pass `timeout=context.time_remaining()` yourself.
- Servers should stop work when the client gives up: check `context.is_active()` in loops and streams.
- Retries spend the same budget. Configure retries with backoff in the service config, only for idempotent methods and retryable codes ([Retries and backoff]({{ '/backend/retries-and-backoff/' | relative_url }})).

## Status codes

gRPC has 17 canonical codes. They map loosely to HTTP and decide retry behavior:

| Code | Name | Typical cause | HTTP analog | Retry? |
|---|---|---|---|---|
| 0 | `OK` | Success | 200 | n/a |
| 1 | `CANCELLED` | Caller canceled | 499 | No |
| 2 | `UNKNOWN` | Unmapped server exception | 500 | Rarely |
| 3 | `INVALID_ARGUMENT` | Bad input regardless of state | 400 | No |
| 4 | `DEADLINE_EXCEEDED` | Ran out of time | 504 | Only idempotent, within budget |
| 5 | `NOT_FOUND` | No such entity | 404 | No |
| 6 | `ALREADY_EXISTS` | Duplicate create | 409 | No |
| 7 | `PERMISSION_DENIED` | Authenticated, not allowed | 403 | No |
| 8 | `RESOURCE_EXHAUSTED` | Quota, rate limit, message too large | 429 | With backoff |
| 9 | `FAILED_PRECONDITION` | System not in required state | 400 | No; fix state first |
| 10 | `ABORTED` | Concurrency conflict | 409 | Yes, redo read-modify-write |
| 11 | `OUT_OF_RANGE` | Past end of valid range | 400 | No |
| 12 | `UNIMPLEMENTED` | Method not supported | 501 | No |
| 13 | `INTERNAL` | Broken invariant | 500 | Rarely |
| 14 | `UNAVAILABLE` | Transient: server down, connection reset | 503 | Yes, with backoff |
| 15 | `DATA_LOSS` | Unrecoverable corruption | 500 | No |
| 16 | `UNAUTHENTICATED` | Missing or bad credentials | 401 | After refreshing credentials |

For structured errors, the `google.rpc.Status` model attaches typed details (field violations, retry info, error reason) in the `grpc-status-details-bin` trailer.

## Load balancing challenges

HTTP/2's strength is also its operational trap. A client opens one connection and multiplexes every RPC over it for hours. A connection-level (L4) balancer, including a Kubernetes `ClusterIP` Service via kube-proxy, picks a backend once per connection, so every RPC from that client goes to the same pod:

```text
L4 balancing (per connection)          L7 or client-side (per request)

 client pod                             client pod
   | one HTTP/2 connection                | RPC 1 -> pod A
   v                                      | RPC 2 -> pod B
 [kube-proxy / NLB]                       | RPC 3 -> pod C
   |                                      v
   +--> pod A   all RPCs, hot          [Envoy or round_robin policy]
        pod B   idle                     /        |        \
        pod C   idle                  pod A     pod B     pod C
```

New pods added by autoscaling receive nothing until clients reconnect. Fixes:

- **Client-side balancing**: a headless Service so DNS returns every pod IP, plus the `round_robin` policy; the client keeps a subchannel per backend and spreads calls.
- **L7 proxy or service mesh**: Envoy, Linkerd or Istio sidecars balance per request ([Service discovery and mesh]({{ '/backend/service-discovery-and-mesh/' | relative_url }})).
- **Lookaside / xDS**: proxyless gRPC clients receive endpoint lists and policies from a control plane.
- **`max_connection_age`** on servers forces periodic reconnects so clients re-resolve DNS and spread onto new pods.

## gRPC-Web and browsers

Browsers do not let JavaScript control HTTP/2 framing or read trailers, so native gRPC cannot be called from a web page. Options:

- **gRPC-Web**: a variant protocol that carries trailers inside the response body. A proxy (Envoy's `grpc_web` filter is common) or in-process middleware translates to native gRPC. It supports unary and server streaming, not client or bidirectional streaming.
- **Connect protocol** (connectrpc): servers speak gRPC, gRPC-Web and a simple HTTP/JSON-friendly protocol from the same handlers, which also makes `curl` debugging easy.
- **JSON transcoding**: annotate methods with HTTP mappings and let grpc-gateway or Envoy's transcoder expose REST/JSON for public clients while services speak gRPC internally.

```protobuf
import "google/api/annotations.proto";

service OrderService {
  rpc GetOrder(GetOrderRequest) returns (Order) {
    option (google.api.http) = { get: "/v1/orders/{id}" };
  }
}
```

## SOAP: envelopes and WSDL

SOAP (originally Simple Object Access Protocol) is an XML messaging protocol standardized by the W3C (1.1 in 2000, 1.2 in 2003). Every message is an XML **envelope** with an optional **header** (security, addressing, transactions) and a **body** (the operation payload or a **fault**). It is transport-independent in theory and almost always HTTP `POST` in practice. SOAP 1.1 uses `Content-Type: text/xml` plus a `SOAPAction` header; SOAP 1.2 uses `application/soap+xml`.

```xml
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
               xmlns:ord="urn:example:orders">
  <soap:Header>
    <wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
      <wsse:UsernameToken>...</wsse:UsernameToken>
    </wsse:Security>
  </soap:Header>
  <soap:Body>
    <ord:GetOrder>
      <ord:OrderId>ord_123</ord:OrderId>
    </ord:GetOrder>
  </soap:Body>
</soap:Envelope>
```

Errors come back as a `soap:Fault` in the body with a code (`soap:Sender` for client errors, `soap:Receiver` for server errors in 1.2), a reason and optional detail; SOAP 1.1 over HTTP returns every fault with status `500`; SOAP 1.2 uses `400` for `Sender` faults and `500` for the rest.

The contract is a **WSDL** document. In WSDL 1.1, by far the most common version:

| Element | Describes |
|---|---|
| `types` | XML Schema (XSD) definitions of every payload |
| `message` | Request and response messages built from those types |
| `portType` | Abstract operations (`GetOrder`) with input and output |
| `binding` | Concrete protocol: SOAP version, `document/literal` style |
| `service` / `port` | Endpoint address |

Tools generate typed clients from the WSDL (`zeep` in Python, JAX-WS in Java, `dotnet-svcutil` in .NET): the same idea as protobuf codegen, fifteen years earlier and far more verbose.

## WS-Security

SOAP's distinctive feature is **message-level security**. WS-Security (OASIS) defines a `Security` header that can carry:

- Security tokens: `UsernameToken` (username and password or digest), X.509 certificates, SAML assertions, Kerberos tickets.
- XML Signature over selected parts of the message (body, timestamp), proving integrity and origin.
- XML Encryption of selected elements.
- A `Timestamp` with `Created` and `Expires`, used for replay protection.

TLS protects one hop. A signed and encrypted SOAP body stays protected across intermediaries (an ESB, a message queue, an archive) and gives the receiver signed evidence of what was sent, which is why regulated industries value it. The rest of the WS-* stack (WS-Addressing, WS-ReliableMessaging, WS-AtomicTransaction, WS-Policy) adds routing, delivery guarantees and distributed transactions at the cost of considerable complexity.

## Where SOAP still lives

As of 2025–2026, SOAP is still common in:

- Banking and payments: core banking systems, interbank and card-network integrations.
- Government: tax e-filing, customs, social-security and registry interfaces.
- Insurance, telecom provisioning, and some healthcare exchange profiles.
- Travel distribution (airline and hotel booking systems).
- Enterprise platforms: SAP, Microsoft Dynamics, and Salesforce, which still documents a SOAP API alongside REST.

You rarely design a new SOAP API; you integrate with one, generate a client from the WSDL, and isolate it behind an adapter.

## REST vs GraphQL vs gRPC vs SOAP

| | REST | GraphQL | gRPC | SOAP |
|---|---|---|---|---|
| Payload | JSON, usually | JSON | Protobuf (binary) | XML |
| Transport | HTTP/1.1 or 2 | HTTP; WebSocket for subscriptions | HTTP/2 | HTTP mostly |
| Contract | Optional (OpenAPI) | Required (SDL) | Required (`.proto`) | Required (WSDL + XSD) |
| Response shape set by | Server | Client | Server | Server |
| Streaming | Not native (SSE, WebSocket) | Subscriptions | Native, four modes | No |
| Browser support | Native | Native | Via gRPC-Web, Connect | Possible, painful |
| HTTP caching | Excellent | Poor without persisted `GET` | None | None |
| Error model | HTTP status + body | `errors[]`, often with 200 | `grpc-status` trailer | SOAP Fault |
| Security extras | TLS, OAuth | TLS, OAuth, field auth | TLS/mTLS, per-call credentials | WS-Security, message-level |
| Efficiency | Good | Good, resolver overhead | Highest | Lowest |
| Best fit | Public and partner APIs, CRUD | Client-driven UIs, aggregation | Internal services, streaming, polyglot | Legacy enterprise, regulated integrations |

See [REST API design]({{ '/backend/rest-api-design/' | relative_url }}) and [GraphQL]({{ '/backend/graphql/' | relative_url }}) for those two in depth.

## In practice

### Protobuf wire format and gRPC framing, runnable

```python
import struct


def encode_varint(n):
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        if n:
            out.append(byte | 0x80)          # more bytes follow
        else:
            out.append(byte)
            return bytes(out)


def decode_varint(buf, pos):
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def zigzag(n):                               # how sint32/sint64 store n
    return (n << 1) ^ (n >> 63)


def encode_field(number, value):
    if isinstance(value, int):               # wire type 0: varint
        return encode_varint(number << 3 | 0) + encode_varint(value)
    data = value.encode() if isinstance(value, str) else value
    # wire type 2: length-delimited (strings, bytes, nested messages)
    return encode_varint(number << 3 | 2) + encode_varint(len(data)) + data


def decode(buf):
    fields, pos = {}, 0
    while pos < len(buf):
        key, pos = decode_varint(buf, pos)
        number, wire_type = key >> 3, key & 7
        if wire_type == 0:
            value, pos = decode_varint(buf, pos)
        elif wire_type == 2:
            length, pos = decode_varint(buf, pos)
            value, pos = buf[pos:pos + length], pos + length
        else:
            raise ValueError(f"wire type {wire_type} not handled here")
        fields[number] = value
    return fields


def grpc_frame(message, compressed=False):
    # gRPC message on an HTTP/2 DATA stream: flag byte + 4-byte length
    return struct.pack(">BI", int(compressed), len(message)) + message


if __name__ == "__main__":
    # Examples from the protobuf encoding guide.
    assert encode_field(1, 150) == bytes.fromhex("089601")
    assert encode_field(2, "testing") == bytes.fromhex("120774657374696e67")

    # A v2 writer adds field 3; a v1 reader that knows fields 1-2 skips it.
    v2 = encode_field(1, 150) + encode_field(2, "testing") \
        + encode_field(3, 42)
    v1_view = {k: v for k, v in decode(v2).items() if k in (1, 2)}
    assert v1_view == {1: 150, 2: b"testing"}

    # Negative int32/int64 costs 10 bytes; sint32 with zigzag costs 1.
    assert len(encode_varint(-1 & 0xFFFFFFFFFFFFFFFF)) == 10
    assert [zigzag(n) for n in (0, -1, 1, -2)] == [0, 1, 2, 3]
    assert len(encode_varint(zigzag(-1))) == 1

    assert grpc_frame(b"abc") == b"\x00\x00\x00\x00\x03abc"
    print("protobuf wire format checks passed")
```

### Python service with deadlines and client-side balancing

Illustrative (grpcio, generated `orders_pb2` / `pricing_pb2` modules). Generate code and check compatibility in CI:

```bash
python -m grpc_tools.protoc -I proto \
  --python_out=gen --pyi_out=gen --grpc_python_out=gen \
  proto/shop/orders/v1/orders.proto
buf breaking --against '.git#branch=main'   # fail on wire-breaking changes
```

Server: abort with proper codes, pass the remaining deadline downstream, stop streams when the client is gone, and cap connection age so clients rebalance:

```python
from concurrent import futures

import grpc
import orders_pb2
import orders_pb2_grpc
import pricing_pb2


class OrderService(orders_pb2_grpc.OrderServiceServicer):
    def __init__(self, repo, pricing_stub):
        self.repo = repo
        self.pricing = pricing_stub

    def GetOrder(self, request, context):
        order = self.repo.get(request.id)
        if order is None:
            context.abort(grpc.StatusCode.NOT_FOUND,
                          f"order {request.id} not found")
        remaining = context.time_remaining()      # None if no deadline
        quote = self.pricing.Quote(
            pricing_pb2.QuoteRequest(order_id=order.id),
            timeout=0.2 if remaining is None else max(remaining - 0.02, 0.01),
        )
        return orders_pb2.Order(id=order.id, total_cents=quote.total_cents)

    def WatchOrders(self, request, context):
        for event in self.repo.changes(request.customer_id):
            if not context.is_active():           # canceled or timed out
                return
            yield event


def serve(repo, pricing_stub):
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=32),
        options=[("grpc.max_connection_age_ms", 5 * 60 * 1000)],
    )
    orders_pb2_grpc.add_OrderServiceServicer_to_server(
        OrderService(repo, pricing_stub), server)
    server.add_insecure_port("[::]:50051")        # mTLS via a mesh or creds
    server.start()
    server.wait_for_termination()
```

Client: a service config with a default timeout, retries only on `UNAVAILABLE`, and round-robin balancing:

```json
{
  "methodConfig": [
    {
      "name": [{ "service": "shop.orders.v1.OrderService" }],
      "timeout": "0.5s",
      "retryPolicy": {
        "maxAttempts": 3,
        "initialBackoff": "0.1s",
        "maxBackoff": "1s",
        "backoffMultiplier": 2,
        "retryableStatusCodes": ["UNAVAILABLE"]
      }
    }
  ],
  "loadBalancingConfig": [{ "round_robin": {} }]
}
```

Pass it with `options=[("grpc.service_config", config_json)]` to `grpc.insecure_channel("dns:///orders-headless.shop.svc.cluster.local:50051", ...)`. The headless Service makes DNS return every pod, and `round_robin` spreads calls across them. Catch `grpc.RpcError` and branch on `err.code()`.

### Calling a SOAP partner

Illustrative, with `zeep`. Keep it behind an adapter so the rest of the codebase never sees XML:

```python
from zeep import Client
from zeep.wsse.username import UsernameToken

client = Client(
    "https://partner.example.com/OrderService?wsdl",
    wsse=UsernameToken("api_user", password_from_secrets_manager),
)
result = client.service.GetOrder(OrderId="ord_123")   # typed from the WSDL
```

## Pitfalls

- **No deadlines.** One slow dependency exhausts thread pools in every caller. Set a default in the service config and propagate the remainder.
- **One hot pod behind a Kubernetes Service.** Classic L4-plus-HTTP/2 symptom: uneven CPU, new pods idle after scale-up. Use client-side or mesh balancing and `max_connection_age`.
- **Reusing field numbers or changing types.** Silent corruption, not errors. `reserved` old numbers and run `buf breaking` in CI.
- **Treating zero as unset.** A proto3 update with `discount_cents = 0` cannot be told from "not provided". Use `optional` or field masks.
- **Message size limits.** Most implementations cap received messages at 4 MB by default; large responses fail with `RESOURCE_EXHAUSTED`. Paginate or stream.
- **Retrying non-idempotent calls on `DEADLINE_EXCEEDED`.** The first attempt may have succeeded. Retry only idempotent methods or use idempotency keys ([Idempotency]({{ '/backend/idempotency/' | relative_url }})).
- **SOAP clock skew and XML attacks.** WS-Security timestamps fail when clocks drift; XML parsers with entity expansion enabled are open to XXE and billion-laughs attacks. Sync clocks and use hardened parsers (`defusedxml`).

## Interview questions

**Why use gRPC between internal services instead of REST?** Typed contracts with codegen, compact binary payloads, HTTP/2 multiplexing, native streaming, and first-class deadlines and status codes. Costs: weak browser support, no HTTP caching, harder debugging without tools like `grpcurl`.

**Why does gRPC load balancing behind a Kubernetes Service not spread load?** The Service balances per TCP connection, and a gRPC client multiplexes all RPCs over one long-lived HTTP/2 connection. Use per-request balancing: client-side `round_robin` over a headless Service, a mesh sidecar, or xDS, and cap connection age so clients re-resolve.

**What is deadline propagation?** Passing the caller's remaining time budget to downstream calls instead of each hop using its own timeout. It stops work that nobody is waiting for and prevents retries from exceeding the user-facing latency budget.

**How do you evolve a protobuf schema safely?** Add fields with new numbers; never reuse or retype numbers; `reserved` removed numbers and names; keep enum zero as `UNSPECIFIED`; enforce with a breaking-change check in CI.

**Can a browser call a gRPC service?** Not natively, because JavaScript cannot read HTTP/2 trailers. Use gRPC-Web through a proxy (unary and server streaming only), the Connect protocol, or JSON transcoding.

**Which gRPC status codes are safe to retry?** `UNAVAILABLE` with backoff; `RESOURCE_EXHAUSTED` with backoff when it is a quota; `ABORTED` by redoing the whole read-modify-write; `DEADLINE_EXCEEDED` only for idempotent methods within the remaining budget.

**When would you still choose SOAP?** When integrating with a partner or regulator that mandates it, or when message-level signing and encryption across intermediaries is a hard requirement. Otherwise wrap it behind an adapter and expose REST or gRPC.

## Further reading

- *gRPC over HTTP2* protocol specification (`PROTOCOL-HTTP2.md` in the grpc/grpc repository).
- Protocol Buffers documentation: *Language Guide (proto 3)* and *Encoding*.
- *gRPC Load Balancing*, gRPC blog (grpc.io).
- W3C, *SOAP Version 1.2 Part 1: Messaging Framework*.
- OASIS, *Web Services Security: SOAP Message Security 1.1*.
