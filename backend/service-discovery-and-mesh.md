---
title: Service discovery and service mesh
section: backend
order: 7
phase: "Phase 1 · Backend architecture"
summary: How services find each other (client vs server-side, DNS, Consul, etcd, Kubernetes Services) and what a mesh adds with sidecars, mTLS and traffic policy.
---

In a static world, `orders` lives at `10.0.3.17:8000` and you put that in a config file. In a real backend, instances come and go every few minutes: autoscaling adds pods, deploys replace them, nodes die, and each new instance gets a new IP. Service discovery is the system that answers "where are the healthy instances of `orders` right now", and a service mesh is the layer that then manages how calls between those instances happen: encryption, identity, retries, timeouts and traffic splitting, without each service implementing them.

## Service discovery

Every discovery system has three parts:

1. **Registration**: instances (or the platform) record "service X, address Y, healthy".
2. **Health tracking**: dead or unready instances are removed, through heartbeats, leases or active health checks.
3. **Lookup**: callers resolve a name to a set of addresses, and something picks one.

### Client-side discovery

The caller queries the registry, caches the instance list, and load balances itself.

```text
 client-side                         server-side
 +--------+  1. lookup  +--------+   +--------+       +---------------+
 | caller |-----------> |registry|   | caller |------>| load balancer |
 | (LB in |<----------- |        |   +--------+       | or kube-proxy |
 |  lib)  |  instances  +--------+                    +---------------+
 +--------+                ^  register                  |  | lookup
     | 2. call one         |  + heartbeat               v  v
     v                  +----------+                 +----------+
 +----------+           | orders x3|                 | orders x3|
 | orders #2|           +----------+                 +----------+
 +----------+
```

- Pros: no extra hop, smart per-request balancing (least outstanding requests, power of two choices, zone awareness), works well with long-lived HTTP/2 and gRPC connections.
- Cons: a discovery library in every language you use; caching and failure handling bugs are per client.
- Examples: Netflix Eureka with the Ribbon client (publicly described, now largely superseded), gRPC's built-in resolvers and load balancing policies, Consul or etcd with a client library.

### Server-side discovery

The caller hits a stable address (a load balancer or virtual IP); the infrastructure resolves and balances.

- Pros: callers stay dumb, any language works, one place to change behavior.
- Cons: extra hop (or kernel-level NAT), balancing is per connection unless the LB is L7, and the LB must be highly available.
- Examples: AWS ALB or NLB with target groups, Kubernetes Services via kube-proxy, nginx or HAProxy fed by a registry.

| | Client-side | Server-side |
|---|---|---|
| Extra hop | No | Usually yes (or kernel NAT) |
| Language coupling | Library per language | None |
| Balancing quality | Per request, latency-aware | Per connection unless L7 |
| Failure surface | Every client's cache | The LB tier |
| Typical in | gRPC, Eureka-era JVM stacks | Kubernetes, cloud LBs |

### DNS-based discovery

DNS is the universal client-side registry: every language can resolve a name.

- **A/AAAA records** return instance IPs; **SRV records** also return ports, priority and weight.
- Consul serves DNS (`orders.service.consul`, port 8600 by default); Kubernetes serves `orders.shop.svc.cluster.local` through CoreDNS.

The weakness is caching. TTLs are honored loosely: some runtimes cache longer than the TTL (older JVMs cached forever unless `networkaddress.cache.ttl` was set), HTTP clients keep pooled connections to old IPs regardless of DNS, and negative answers are cached too. DNS also has no notion of load or health beyond what the server decides to return. Use short TTLs (seconds), and treat DNS as "eventually correct", not as real-time membership.

### Registries: Consul, etcd, ZooKeeper, Eureka

| Registry | Consistency | Membership and health | Notes |
|---|---|---|---|
| Consul | Raft among servers (CP for the catalog) | Local agents run health checks; gossip for node liveness | DNS and HTTP APIs, KV store, built-in mesh (Consul service mesh) |
| etcd | Raft (CP) | Leases with TTL; instance keeps the lease alive | Watch on key prefixes; Kubernetes stores all cluster state in it |
| ZooKeeper | ZAB (CP) | Ephemeral znodes disappear when the session dies | Older Hadoop and Kafka ecosystems |
| Eureka | AP, peer replication | Client heartbeats; "self-preservation" stops expiry if too many heartbeats vanish at once | Favors stale data over no data |

The CP versus AP choice matters for discovery. A strongly consistent registry can refuse writes during a partition, so new instances can't register; an AP registry may hand out dead instances. Most designs accept staleness and compensate on the client with timeouts, retries to another instance and outlier ejection, because a slightly stale list is far better than no list. Read [CAP and consistency models]({{ '/backend/cap-and-consistency-models/' | relative_url }}) for the general argument.

**Self-registration vs third-party registration.** In self-registration, the service registers itself and heartbeats (Eureka clients, etcd leases). In third-party registration, the platform does it from the outside: Kubernetes adds pod IPs to EndpointSlices when readiness probes pass; tools like Registrator watched Docker events. Third-party registration is more reliable because a hung process can't keep heartbeating from a healthy thread while its request threads are deadlocked, as long as the health check exercises real work.

### Kubernetes Services

Kubernetes is the discovery system most backends use today, often without noticing:

1. A `Service` selects pods by label and gets a stable virtual IP (ClusterIP) and DNS name.
2. The EndpointSlice controller tracks which selected pods are **ready** (readiness probe passing) and lists their IPs.
3. kube-proxy on each node programs iptables, IPVS or nftables rules so packets to the ClusterIP are NATed to a ready pod. eBPF dataplanes such as Cilium can replace kube-proxy entirely.
4. CoreDNS answers `orders.shop.svc.cluster.local` with the ClusterIP.

Variants:

- **Headless Service** (`clusterIP: None`): DNS returns the pod IPs directly. Used for StatefulSets (`db-0.db.shop.svc.cluster.local`) and for client-side load balancing.
- **ExternalName**: a CNAME to something outside the cluster.
- **Topology-aware routing**: prefer endpoints in the same zone to cut cross-zone latency and cost.

> **Gotcha:** kube-proxy balances **connections**, not requests. gRPC and other HTTP/2 clients open one long-lived connection and multiplex everything over it, so all traffic from a client pod lands on one server pod while new pods sit idle. Fix it with a headless Service plus gRPC client-side `round_robin`, a server-side max connection age so clients reconnect, or an L7 mesh proxy that balances per request.

## Service mesh

Discovery tells you where instances are. Production calls also need encryption, workload identity, timeouts, retries, circuit breaking, traffic splitting and per-call metrics. Libraries can do all of that (Netflix publicly described doing it with Hystrix and Ribbon), but only for one language, and upgrading means redeploying every service. A service mesh moves this into the network layer.

### Architecture

```text
            control plane (istiod / linkerd-destination)
       config, certs, endpoints pushed via xDS or gRPC
        |                                       |
        v                                       v
 +-------------------------+          +-------------------------+
 | pod: checkout           |          | pod: orders             |
 |  +-----+    +---------+ |  mTLS    | +---------+    +-----+  |
 |  | app |--->| sidecar |------------->| sidecar |--->| app |  |
 |  +-----+    +---------+ |          | +---------+    +-----+  |
 +-------------------------+          +-------------------------+
       localhost, plain HTTP                localhost, plain HTTP
```

- **Data plane**: a proxy next to every workload intercepts inbound and outbound traffic (iptables redirect or eBPF). The app talks plain HTTP to `orders`; the proxy resolves endpoints, opens mTLS to the remote proxy, applies retries and timeouts, and emits metrics and trace spans.
- **Control plane**: watches Kubernetes for services and endpoints, issues certificates, and pushes routing and policy config to the proxies.

### Envoy

Envoy is the most common mesh proxy: a C++ L4/L7 proxy started at Lyft and now a CNCF graduated project. Its key design point is the **xDS APIs** (Listener, Route, Cluster, Endpoint and Secret discovery services): every piece of config can be streamed from a control plane at runtime, without restarts. Istio, Consul's mesh and several gateways are control planes that speak xDS to Envoy. gRPC can also consume xDS directly ("proxyless" mesh).

### Istio and Linkerd

| | Istio | Linkerd |
|---|---|---|
| Data plane | Envoy sidecars, or ambient mode | linkerd2-proxy, a small Rust proxy |
| Scope | Very broad: L7 routing, policies, gateways, VM support, multi-cluster | Narrower: mTLS, reliability, observability, with less configuration |
| Operational weight | Higher | Lower |
| Config | VirtualService, DestinationRule, PeerAuthentication, AuthorizationPolicy (plus Gateway API) | Mostly annotations, ServiceProfiles, Gateway API routes |

Both are CNCF graduated projects. Cilium offers a sidecarless mesh built on eBPF with a per-node Envoy for L7.

### mTLS and workload identity

The mesh gives every workload an identity, usually a SPIFFE ID derived from its Kubernetes service account (`spiffe://cluster.local/ns/shop/sa/orders`), and a short-lived certificate that the control plane rotates automatically (hours, not years). Both sides of every connection authenticate. That enables:

- **Encryption in transit** inside the cluster without touching app code.
- **Authorization by identity**, not by IP: "only `checkout` may call `POST /payments`". IPs are reused by other pods minutes later; identities are not.
- A real **zero-trust** posture: a compromised pod without the right identity can't call sensitive services.

Start in permissive mode (accept both plaintext and mTLS) while migrating, then switch to strict.

### Traffic policies

- **Timeouts and retries** per route, with retry budgets so retries can't exceed a fraction of traffic.
- **Circuit breaking**: connection and pending-request limits per upstream.
- **Outlier detection**: eject an instance after N consecutive 5xx and send traffic elsewhere; see [Fault tolerance patterns]({{ '/backend/fault-tolerance-patterns/' | relative_url }}).
- **Traffic splitting**: weighted canaries, header-based routing, mirroring (send a copy of live traffic to a new version and discard its responses); see [Deployment strategies]({{ '/backend/deployment-strategies/' | relative_url }}).
- **Fault injection**: add delays or errors to test resilience.
- **Locality-aware balancing**: prefer the same zone, fail over to others.

### Ambient mode

Sidecars have real costs: one proxy per pod (memory and CPU multiplied by pod count), upgrades that require restarting every workload, and startup and shutdown ordering bugs. Istio's ambient mode (generally available since late 2024) splits the data plane in two:

- **ztunnel**: a per-node L4 proxy (written in Rust) that provides mTLS, identity and L4 authorization for all pods on the node.
- **Waypoint proxies**: optional Envoy deployments per namespace or service account, used only when you need L7 features such as HTTP routing, retries or header-based policy.

You get mTLS everywhere for little cost and pay for L7 only where you use it. The trade-off is a newer, less battle-tested model and shared per-node components, which change the blast radius compared with per-pod sidecars.

### When a mesh is worth it

| Adopt a mesh when | Skip it when |
|---|---|
| Dozens of services in several languages | A handful of services, one language |
| Compliance needs mTLS everywhere | Cloud LB plus TLS at the edge suffices |
| You need identity-based authorization between services | Network policies by namespace are enough |
| Uniform golden metrics without app changes | You already have OpenTelemetry in every service |
| A platform team can own it | No one can operate another control plane |

A mesh is infrastructure you have to run: control plane upgrades, certificate authority health, proxy CPU, and a new layer to debug when "the network" misbehaves. Expect added latency per hop on the order of a millisecond or less, and measure it in your environment.

## In practice

### Registry with leases and client-side round robin

A runnable stdlib model: instances register with a TTL lease, heartbeats renew it, a hung instance expires, and the client refreshes its cached list when the registry's membership version changes.

```python
import itertools
import time


class Registry:
    """Instances hold a lease; a missed heartbeat past the TTL removes them."""

    def __init__(self, ttl: float, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self.leases = {}          # (service, address) -> expiry
        self.version = 0          # bumps on membership change, for cheap polling

    def register(self, service: str, address: str):
        self.leases[(service, address)] = self.clock() + self.ttl
        self.version += 1

    def heartbeat(self, service: str, address: str):
        key, now = (service, address), self.clock()
        if self.leases.get(key, now) <= now:
            raise KeyError("lease expired; re-register")
        self.leases[key] = now + self.ttl

    def deregister(self, service: str, address: str):
        if self.leases.pop((service, address), None) is not None:
            self.version += 1

    def lookup(self, service: str):
        now = self.clock()
        expired = [k for k, exp in self.leases.items() if exp <= now]
        for key in expired:
            del self.leases[key]
        if expired:
            self.version += 1
        return sorted(addr for (svc, addr) in self.leases if svc == service)


class DiscoveryClient:
    """Client-side discovery: cache the instance list, refresh on a version change."""

    def __init__(self, registry: Registry, service: str):
        self.registry, self.service = registry, service
        self.seen_version, self.instances, self.rr = None, [], iter(())

    def pick(self) -> str:
        current = self.registry.lookup(self.service)
        if self.registry.version != self.seen_version:
            self.seen_version, self.instances = self.registry.version, current
            self.rr = itertools.cycle(self.instances)
        if not self.instances:
            raise LookupError(f"no healthy instances of {self.service}")
        return next(self.rr)


if __name__ == "__main__":
    now = [0.0]
    reg = Registry(ttl=10, clock=lambda: now[0])
    for addr in ("10.0.0.1:8000", "10.0.0.2:8000", "10.0.0.3:8000"):
        reg.register("orders", addr)
    client = DiscoveryClient(reg, "orders")

    picks = [client.pick() for _ in range(6)]
    assert picks[:3] == ["10.0.0.1:8000", "10.0.0.2:8000", "10.0.0.3:8000"]
    assert picks[3:] == picks[:3]                       # round robin

    now[0] = 8
    reg.heartbeat("orders", "10.0.0.1:8000")
    reg.heartbeat("orders", "10.0.0.3:8000")            # .2 is hung, no heartbeat
    now[0] = 12                                         # .2's lease ran out at t=10
    assert {client.pick() for _ in range(4)} == {"10.0.0.1:8000", "10.0.0.3:8000"}

    reg.deregister("orders", "10.0.0.1:8000")           # graceful shutdown
    assert client.pick() == "10.0.0.3:8000"
    now[0] = 30                                         # everything expired
    try:
        reg.heartbeat("orders", "10.0.0.3:8000")        # too late to renew
        raise AssertionError("expected KeyError")
    except KeyError:
        pass
    try:
        client.pick()
        raise AssertionError("expected LookupError")
    except LookupError:
        pass
    print("discovery demo ok")
```

### Kubernetes Service and headless Service

Illustrative manifests. Pods only join the endpoints once their readiness probe passes.

```yaml
apiVersion: v1
kind: Service
metadata:
  name: orders
  namespace: shop
spec:
  selector:
    app: orders
  ports:
    - name: http
      port: 80
      targetPort: 8000
---
apiVersion: v1
kind: Service
metadata:
  name: orders-grpc
  namespace: shop
spec:
  clusterIP: None          # headless: DNS returns pod IPs for client-side LB
  selector:
    app: orders
  ports:
    - name: grpc
      port: 50051
```

Inspect what discovery actually returns:

```bash
kubectl -n shop get endpointslices -l kubernetes.io/service-name=orders
kubectl -n shop run dns --rm -it --image=busybox:1.36 --restart=Never -- \
  nslookup orders-grpc.shop.svc.cluster.local
dig @127.0.0.1 -p 8600 orders.service.consul SRV   # Consul DNS, with ports
```

### Istio policies

Illustrative Istio config: strict mTLS in a namespace, a timeout and bounded retries, connection limits and outlier ejection, and identity-based authorization.

```yaml
apiVersion: security.istio.io/v1
kind: PeerAuthentication
metadata:
  name: default
  namespace: shop
spec:
  mtls:
    mode: STRICT
---
apiVersion: networking.istio.io/v1
kind: VirtualService
metadata:
  name: orders
  namespace: shop
spec:
  hosts: ["orders"]
  http:
    - timeout: 2s
      retries:
        attempts: 2
        perTryTimeout: 500ms
        retryOn: connect-failure,refused-stream,unavailable
      route:
        - destination:
            host: orders
            subset: v1
          weight: 95
        - destination:
            host: orders
            subset: v2
          weight: 5
---
apiVersion: networking.istio.io/v1
kind: DestinationRule
metadata:
  name: orders
  namespace: shop
spec:
  host: orders
  subsets:
    - name: v1
      labels: {version: v1}
    - name: v2
      labels: {version: v2}
  trafficPolicy:
    connectionPool:
      http:
        http1MaxPendingRequests: 100
        http2MaxRequests: 1000
    outlierDetection:
      consecutive5xxErrors: 5
      interval: 10s
      baseEjectionTime: 30s
      maxEjectionPercent: 50
---
apiVersion: security.istio.io/v1
kind: AuthorizationPolicy
metadata:
  name: payments-only-from-checkout
  namespace: shop
spec:
  selector:
    matchLabels:
      app: payments
  action: ALLOW
  rules:
    - from:
        - source:
            principals: ["cluster.local/ns/shop/sa/checkout"]
      to:
        - operation:
            methods: ["POST"]
            paths: ["/payments*"]
```

Opting a namespace into ambient mode is a label: `kubectl label namespace shop istio.io/dataplane-mode=ambient`.

## Pitfalls

- **Stale endpoints during deploys.** A pod gets SIGTERM and exits while kube-proxy and callers still route to it, causing a burst of 502s. Fail readiness first, keep serving for a few seconds (a `preStop` sleep), then drain. See [Graceful shutdown and health checks]({{ '/backend/graceful-shutdown-and-health-checks/' | relative_url }}).
- **Health checks that lie.** A `/health` that returns 200 while the DB pool is exhausted keeps a broken pod in rotation. Readiness should reflect ability to serve; liveness should only catch deadlocks, and must not depend on downstreams or one slow DB restarts the whole fleet.
- **gRPC imbalance** through ClusterIP Services, as described above.
- **DNS overhead and `ndots:5`.** Pod `resolv.conf` defaults to `ndots:5`, so `api.stripe.com` is first tried with each cluster search suffix, multiplying lookups and adding latency. Use FQDNs with a trailing dot or lower `ndots` via `dnsConfig`, and cache DNS on nodes (NodeLocal DNSCache).
- **Retry storms through the mesh.** Mesh retries on top of app retries multiply load on an already struggling service. Pick one layer, and retry only idempotent requests.
- **Sidecar lifecycle bugs.** The app starts before the proxy is ready and its first calls fail; Jobs never finish because the sidecar keeps running. Use Istio's `holdApplicationUntilProxyStarts` or Kubernetes native sidecar containers (init containers with `restartPolicy: Always`, available in recent Kubernetes versions).
- **Registry as a single point of failure.** If clients can't start when the registry is down, a Consul outage becomes a full outage. Cache the last known list on disk or in memory and keep using it.
- **Mesh without ownership.** Upgrades skipped for a year, expired root CA, no one who can read Envoy stats. A mesh is a product a platform team must run.

## Interview questions

**Client-side vs server-side discovery?** Client-side: the caller queries the registry and picks an instance, with no extra hop and request-level balancing, but needs a library per language. Server-side: the caller hits a load balancer or virtual IP that does lookup and balancing, language-agnostic but with an extra hop and often connection-level balancing.

**How does a Kubernetes Service route traffic?** The Service selector picks pods; EndpointSlices list the ready ones; kube-proxy programs NAT rules on every node mapping the ClusterIP to those pod IPs; CoreDNS resolves the Service name to the ClusterIP.

**Why are gRPC calls unevenly balanced in Kubernetes?** HTTP/2 multiplexes requests on one long-lived connection, and kube-proxy balances only at connection setup. Use client-side balancing with a headless Service, a mesh that balances per request, or force periodic reconnects.

**Should a service registry be CP or AP?** Discovery tolerates staleness better than unavailability, so many designs lean AP or cache aggressively on clients even with a CP store like etcd or Consul. Clients must handle dead instances anyway, through timeouts, retries elsewhere and outlier ejection.

**What does a service mesh give you that a library doesn't?** Language-independent mTLS and workload identity, uniform retries, timeouts and traffic splitting, and consistent golden metrics, all changeable centrally without redeploying apps. The cost is extra hops, resource overhead and a control plane to operate.

**What is ambient mode?** Istio's sidecarless data plane: a per-node ztunnel handles L4 mTLS and identity, and optional per-namespace waypoint proxies handle L7. It cuts per-pod overhead and decouples proxy upgrades from app restarts.

**How does mTLS identity differ from IP allowlists?** Identity is cryptographic and tied to the workload's service account; IPs are reused by other pods. Policies like "only checkout can call payments" stay correct as pods move.

## Further reading

- Kubernetes documentation: "Service", "DNS for Services and Pods" and "Virtual IPs and Service Proxies".
- Consul and etcd documentation on service registration, health checks, leases and watches.
- Envoy documentation: architecture overview and the xDS protocol.
- Istio documentation: traffic management, security, and ambient mode overview.
- Chris Richardson, *Microservices Patterns* (Manning), the client-side discovery, server-side discovery and service registry patterns.
