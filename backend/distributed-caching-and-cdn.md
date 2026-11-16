---
title: Distributed caching and CDNs
section: backend
order: 31
phase: "Phase 4 · Caching"
summary: Sharding and replicating caches, consistent hashing, hot keys, L1 plus L2 tiers, and CDNs with origin shields, cache keys, purges and signed URLs.
---

One cache node runs out of memory, network bandwidth, or fault tolerance long before a busy product runs out of data to cache. A distributed cache spreads keys across many nodes so capacity grows with the cluster, and a CDN pushes the cache all the way out to the edge of the network so a hit never reaches your data center. Both raise the same questions: how keys map to nodes, what happens when a node disappears, what to do with a key that is too popular for any single node, and how to invalidate copies you no longer directly control.

## Distributed caching

### Sharding

Each key lives on one shard. Three places can make that decision:

| Where | Examples | Pros | Cons |
|---|---|---|---|
| Client library | Memcached clients (ketama, rendezvous) | no extra hop; servers stay simple | every client must agree on the server list and algorithm |
| Proxy | mcrouter, twemproxy, Envoy Redis proxy | central config; connection pooling; routing rules | extra hop; proxy fleet to run |
| Server cluster | Redis Cluster (16,384 hash slots) | servers own topology; online resharding | smart clients; cross-slot limits |

Client-side sharding has a classic failure: a deploy rolls out a new server list to half the fleet, so the two halves map keys differently, each writes and invalidates different nodes, and stale reads appear that no single request can explain. Keep the server list in one source of truth (service discovery) and change it atomically.

### Replication

Replication buys availability and read capacity at the cost of memory and a little staleness, since cache replication is asynchronous. For a pure cache it is optional. Without replicas, losing a node turns its share of keys into misses; with 20 nodes that is 5% of keys, which is fine if the database can absorb the extra misses and dangerous if those misses include the hottest keys. Replicate when a miss storm would hurt more than the extra memory costs.

How a cluster reacts to a dead node matters as much as replication. Rehashing a dead node's keys onto its ring neighbors can overload them in turn. Facebook's memcache paper publicly describes a **gutter pool**: a small set of otherwise idle servers that temporarily take over a failed server's keys with short TTLs, so failures do not cascade to healthy servers or flood the database.

### Client-side consistent hashing

`hash(key) % N` remaps almost everything when `N` changes: going from 4 to 5 nodes moves about 80% of keys, which empties the cache in effect. Consistent hashing places nodes at many points on a hash ring (virtual nodes) and assigns each key to the next point clockwise. Adding a node moves only the keys that now fall on its points, about `1/(N+1)` of the total, and virtual nodes keep the load even. Rendezvous (highest random weight) hashing gets the same property without a ring by scoring each node per key; jump consistent hash does it with no memory for numbered shards. Details in [Consistent hashing]({{ '/backend/consistent-hashing/' | relative_url }}).

### Hot keys

Sharding spreads keys, not requests for one key. If a single key receives 100k reads per second with a 10 KB value, its node must push about 1 GB/s, roughly 8 Gbit/s, which saturates a typical cache node's NIC no matter how many other nodes the cluster has. Typical culprits: a celebrity profile, a viral post, a global config blob, a feature flag read on every request.

The standard mitigations: a short-TTL in-process L1 cache in front of the cluster, splitting the key into N copies on different shards and reading a random copy, and reading from replicas. Detection, write-hot keys and the full set of fixes are in [Cache stampede and hot keys]({{ '/backend/cache-stampede/' | relative_url }}).

### Local plus remote: two-tier caches

A two-tier cache puts a small in-process L1 (seconds of TTL, bounded size) in front of a shared L2 (Redis or Memcached, minutes of TTL). L1 hits cost nanoseconds and spare the network; L2 keeps the hit ratio high across processes.

```text
  pod 1                          pod 2
 +-------------------------+    +-------------------------+
 | app --> L1 dict, TTL 5s |    | app --> L1 dict, TTL 5s |
 +-----------+-------------+    +-----------+-------------+
             | miss                         | miss
             v                              v
      +-------------------------------------------+
      | L2: Redis Cluster, TTL 10 min             |
      +---------------------+---------------------+
                            | miss
                            v
                        PostgreSQL

 on write: DEL in L2, then PUBLISH inval <key>; every L1 drops it
```

The cost is consistency: each process can serve its own stale copy until its L1 TTL expires. Ways to keep L1 honest:

- **Short TTL only.** Simple, and staleness is bounded by the TTL. Enough for flags, config and catalog data.
- **Pub/sub invalidation.** After a write, publish the key; every process subscribes and drops it. Messages are lost if a subscriber is disconnected, so keep the TTL as a backstop.
- **Redis server-assisted client-side caching** (Redis 6+, `CLIENT TRACKING`). Redis remembers which keys each connection read and pushes invalidation messages when they change. Some clients, including recent redis-py releases, implement it for you.

## CDN caching

A CDN is a globally distributed shared HTTP cache. It obeys the HTTP caching rules from [Caching fundamentals]({{ '/backend/caching-fundamentals/' | relative_url }}) plus provider-specific controls, and it is the only cache that removes both your origin's work and the long-distance network round trip.

### Edge caches

Users reach a nearby point of presence (PoP) through anycast routing or DNS-based steering. Each PoP holds its own cache. Static assets, images, video segments and public API responses hit at the edge; the long tail misses and goes back toward the origin. Even uncacheable requests benefit: TLS terminates near the user, and the CDN keeps warm, long-lived connections to the origin over better routes.

### Origin shield

With hundreds of PoPs, a newly published object can produce hundreds of separate misses to the origin. An **origin shield** (CloudFront Origin Shield, Fastly shielding, Cloudflare tiered cache) is a designated mid-tier cache, usually near the origin, through which all PoPs fetch:

```text
  users (Mumbai)    users (Frankfurt)    users (Virginia)
        |                  |                    |
   +----v-----+       +----v-----+         +----v-----+
   | edge PoP |       | edge PoP |         | edge PoP |  many PoPs
   +----+-----+       +----+-----+         +----+-----+
        | miss             | miss               | miss
        +------------------+--------------------+
                           |
                   +-------v-------+
                   | origin shield |  one tier near the origin
                   +-------+-------+
                           | one request per object
                   +-------v-------+
                   |    origin     |  LB -> app -> Redis -> DB
                   +---------------+
```

The shield collapses those misses into roughly one origin request per object, raises the overall hit ratio for long-tail content, and protects the origin during purges. The cost is an extra hop on shield misses and another tier's fees. Edge servers and shields also collapse concurrent requests for the same uncached object into one fetch.

### Cache keys

The cache key decides which requests share an entry. Defaults usually include host, path and the full query string, which is rarely what you want:

- **Normalize the query string.** Sort parameters and drop tracking ones (`utm_*`, `fbclid`), or better, allow-list the parameters that change the response.
- **Include only headers that change the response.** Normalize `Accept-Encoding` to a few values; use a device class rather than the raw `User-Agent`.
- **Keep cookies out.** A cookie in the key makes every user's entry unique. Strip cookies on cacheable paths.
- **Never leave a header unkeyed if the origin reflects it.** Caching a response built from `X-Forwarded-Host` without keying on it is the basis of web cache poisoning.

An attacker can also bypass the cache by adding random query parameters; allow-listing keys makes those requests hit.

### Purge and invalidation

You can delete from a CDN, unlike a browser cache, but it is slower and coarser than a Redis `DEL`.

- **Purge by URL**: precise, but you must know every URL variant.
- **Purge by tag**: the origin labels responses with tags (`Surrogate-Key` on Fastly, `Cache-Tag` on Cloudflare, cache tags on Akamai); one call purges everything tagged `product-9001`. This is the CDN version of a generation counter.
- **Soft purge**: mark entries stale instead of removing them, so the edge can serve stale while it revalidates. It avoids a miss storm on the origin.
- **Purge all**: an emergency tool. Every PoP misses at once, which is a self-inflicted stampede.

Propagation takes from under a second to minutes depending on the provider, and some charge per invalidation path beyond a free quota. For assets, avoid purges entirely with content-hashed URLs.

### Stale-while-revalidate at the edge

`stale-while-revalidate` (RFC 5861) lets a cache serve a stale response immediately while it fetches a fresh one in the background, so users never wait on an expiry. `stale-if-error` lets it keep serving stale content while the origin is failing, which turns an origin outage into a freshness problem instead of an availability problem.

Split policies between the CDN and browsers. You can purge the CDN but not browsers, so give the CDN the long TTL. `CDN-Cache-Control` (RFC 9213) or the older `Surrogate-Control` targets CDNs only and leaves `Cache-Control` for browsers:

```http
HTTP/1.1 200 OK
Cache-Control: public, max-age=0, must-revalidate
CDN-Cache-Control: max-age=300, stale-while-revalidate=600, stale-if-error=86400
Surrogate-Key: catalog tenant-acme product-9001
ETag: "c41d9e"
```

### Signed URLs and cookies

Private content (paid videos, invoice PDFs, user uploads) should still be served from the edge. A **signed URL** carries an expiry and a signature over the path and expiry (sometimes also a client IP or path prefix), made with a key the CDN can verify: an HMAC shared secret on many CDNs, or a public/private key pair with CloudFront. The edge checks the signature and expiry, then serves the cached object. **Signed cookies** do the same for many files at once, such as every segment of an HLS stream.

Two rules: exclude the signature parameters from the cache key, or every viewer gets a private cache entry and the hit ratio drops to zero; and keep expiries short, because anyone holding a valid URL can share it until it expires.

### Dynamic content at the edge

"Dynamic" does not mean "uncacheable":

- **Micro-caching.** Cache public dynamic responses for 1–10 s. At 2,000 requests per second for one page, a 1 s TTL means the origin sees about one request per second per shield instead of 2,000.
- **Split the page.** Serve a cacheable shell and fetch personalized parts from an API marked `private, no-store`.
- **Edge Side Includes (ESI).** The edge assembles a page from fragments with different TTLs; supported by Varnish and some CDNs.
- **Edge compute.** Cloudflare Workers, Fastly Compute and Lambda@Edge run code at the PoP: validate a JWT, assign an A/B bucket, normalize cache keys, or render from an edge key-value store.

Authenticated responses stay `private` unless the key includes the identity, which rarely pays off in a shared cache.

## In practice

The first runnable demo builds a consistent hash ring with virtual nodes and compares how many keys move when a fifth node joins, against `hash % N`:

```python
import bisect
import hashlib


def h(s):
    return int.from_bytes(hashlib.md5(s.encode()).digest()[:8], "big")


class Ring:
    def __init__(self, nodes, vnodes=160):
        self.vnodes, self.points, self.owner = vnodes, [], {}
        for node in nodes:
            self.add(node)

    def add(self, node):
        for i in range(self.vnodes):
            point = h(f"{node}#{i}")
            bisect.insort(self.points, point)
            self.owner[point] = node

    def node_for(self, key):
        i = bisect.bisect(self.points, h(key)) % len(self.points)
        return self.owner[self.points[i]]


if __name__ == "__main__":
    keys = [f"user:{i}" for i in range(100_000)]
    ring = Ring(["c1", "c2", "c3", "c4"])
    before = {k: ring.node_for(k) for k in keys}
    ring.add("c5")
    moved = sum(before[k] != ring.node_for(k) for k in keys) / len(keys)

    mod_moved = sum(h(k) % 4 != h(k) % 5 for k in keys) / len(keys)
    print(f"ring moved {moved * 100:.1f}%, mod-N moved {mod_moved * 100:.1f}%")
    assert 0.15 < moved < 0.25  # about 1/5 of keys
    assert mod_moved > 0.75     # about 4/5 of keys

    load = {}
    for k in keys:
        n = ring.node_for(k)
        load[n] = load.get(n, 0) + 1
    assert max(load.values()) / (len(keys) / 5) < 1.2  # vnodes keep it even
```

The second demo signs and verifies CDN-style URLs with HMAC and builds a cache key that ignores the signature and tracking parameters:

```python
import hashlib
import hmac
from urllib.parse import parse_qsl, urlencode, urlsplit

SECRET = b"shared-with-the-cdn"


def _sig(path, exp):
    msg = f"{path}|{exp}".encode()
    return hmac.new(SECRET, msg, hashlib.sha256).hexdigest()


def sign(path, exp):
    query = urlencode([("exp", exp), ("sig", _sig(path, exp))])
    return f"{path}?{query}"


def verify(url, now):
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query))
    try:
        exp = int(q["exp"])
    except (KeyError, ValueError):
        return False
    ok = hmac.compare_digest(q.get("sig", ""), _sig(parts.path, exp))
    return ok and now < exp


def cache_key(url, allowed=("w", "fmt")):
    parts = urlsplit(url)
    kept = sorted((k, v) for k, v in parse_qsl(parts.query) if k in allowed)
    return parts.path + ("?" + urlencode(kept) if kept else "")


if __name__ == "__main__":
    url = sign("/videos/42/seg-001.ts", exp=1_700_000_600)
    assert verify(url, now=1_700_000_000)
    assert not verify(url, now=1_700_000_601)  # expired
    assert not verify(url.replace("/42/", "/43/"), now=1_700_000_000)
    assert not verify("/videos/42/seg-001.ts", now=1_700_000_000)

    other = sign("/videos/42/seg-001.ts", exp=1_700_000_900)
    assert cache_key(url) == cache_key(other) == "/videos/42/seg-001.ts"
    assert cache_key("/img/a.png?utm_source=x&w=200&fmt=webp") == \
        cache_key("/img/a.png?fmt=webp&w=200")
    print("signed URLs verified; cache keys normalized")
```

A Django view that gives the CDN a long, purgeable TTL and browsers a revalidating one (illustrative; check which headers your CDN honors):

```python
from django.http import JsonResponse
from django.views.decorators.http import etag


@etag(lambda request, slug: catalog_version(slug))
def public_catalog(request, slug):
    resp = JsonResponse(build_catalog(slug))
    resp["Cache-Control"] = "public, max-age=0, must-revalidate"
    resp["CDN-Cache-Control"] = "max-age=300, stale-while-revalidate=600"
    resp["Surrogate-Key"] = f"catalog tenant-{slug}"
    return resp
```

When the catalog changes, the write path calls the CDN's purge-by-tag API for `tenant-<slug>` after the database commit, alongside the Redis delete.

## Pitfalls

- **Personalized responses in a shared cache.** A response that depends on the session but is marked `public` serves one user's page to others; Steam publicly attributed a December 2015 incident in which users saw other users' account pages to a caching configuration change. Mark authenticated responses `private` and strip `Set-Cookie` from anything cacheable.
- **Signature or tracking parameters in the cache key.** Hit ratio collapses. Allow-list key parameters.
- **Web cache poisoning and deception.** Unkeyed headers reflected into responses, or private pages served under cacheable-looking paths (`/account/settings/x.css`), get cached for everyone. Key on what you reflect and cache only explicit paths.
- **Purge-all as a routine operation.** Origins melt. Purge by tag, use soft purge, keep an origin shield.
- **Server list drift in client-side sharding.** Two views of the ring mean stale reads and missed invalidations. One source of truth, atomic changes.
- **Lost L1 invalidations.** A process missed a pub/sub message and serves stale data. Keep L1 TTLs short so loss is bounded.
- **Node loss cascading to neighbors.** Rehashed keys overload the next node on the ring. Use replicas or a gutter pool.

## Interview questions

**Q: You have 10 cache nodes and need an 11th. What happens to the keys?**
With `hash % N`, about 10/11 of keys move and the cache is effectively cold. With consistent hashing and virtual nodes, about 1/11 move, spread evenly across the old nodes.

**Q: A cache node dies. What does the system do?**
Its keys become misses. With replicas, a replica is promoted. Without, the client or proxy routes those keys elsewhere (ring neighbor or gutter pool) and the database absorbs the misses; size for that, and avoid cascading overload of neighbors.

**Q: How do you handle a hot key in a distributed cache?**
Add a short-TTL in-process L1, replicate the key under several suffixes on different shards and read a random one, or serve it from replicas. Detect it first with per-key metrics or `redis-cli --hotkeys`.

**Q: What is an origin shield and when do you need it?**
A mid-tier CDN cache that all edge PoPs fetch through, so the origin sees about one request per object instead of one per PoP. Use it when you have many PoPs, long-tail content or a fragile origin.

**Q: How do you serve paid videos through a CDN without making them public?**
Signed URLs or signed cookies with short expiry, verified at the edge. Exclude the signature from the cache key so all viewers share cached segments.

**Q: How do you invalidate every cached page that shows product 9001?**
Tag responses with surrogate keys (`product-9001`) and purge by tag after the database commit. For Redis, use a generation counter or CDC-driven deletes.

**Q: How do you cache a page that is mostly public but has a personalized header?**
Cache the public shell at the edge and fetch the personalized part from a `private` API call, or assemble it at the edge with ESI or edge compute.

## Further reading

- Karger et al., "Consistent Hashing and Random Trees", STOC 1997
- Lamping and Veach, "A Fast, Minimal Memory, Consistent Hash Algorithm", 2014
- RFC 9111 (HTTP Caching), RFC 5861 (stale content extensions), RFC 9213 (Targeted HTTP Cache Control)
- Nishtala et al., "Scaling Memcache at Facebook", NSDI 2013
- Redis documentation, "Client-side caching"
