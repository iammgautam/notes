---
title: DNS
section: networking
order: 7
summary: How DNS resolution and caching work, which record types you actually use, and how TTLs shape deploys, migrations and failover.
---

DNS turns names into addresses, and it is a globally distributed cache you only partly control. That is why "we changed the record, but half the users still hit the old server" happens, why a misconfigured TTL can stretch an outage from minutes to hours, and why "it's always DNS" is a running joke among on-call engineers.

## How it works

### Resolution

```text
app -> stub resolver (OS, /etc/resolv.conf)
         -> recursive resolver (ISP, 1.1.1.1, VPC resolver)
              -> root servers        "ask .com servers"
              -> .com TLD servers    "ask ns1.provider.net"
              -> authoritative NS    "api.example.com A 203.0.113.10, TTL 60"
         <- answer cached by the recursive resolver for TTL seconds
<- answer cached by OS / runtime / browser
```

- The **recursive resolver** does the walking and caches every step (TLD and NS records are cached for long times, often days).
- The **authoritative server** (Route 53, Cloudflare, Cloud DNS) holds your zone and is the source of truth.
- **Negative answers** (NXDOMAIN) are cached too, for the zone's SOA minimum TTL. Querying a name before creating it can make it "not exist" for a while.
- Queries are UDP port 53; large responses and zone transfers use TCP.

### Record types you use

| Type | Maps | Example |
|---|---|---|
| A | name to IPv4 | `api.example.com. 60 A 203.0.113.10` |
| AAAA | name to IPv6 | `api.example.com. 60 AAAA 2001:db8::10` |
| CNAME | name to another name | `www 300 CNAME example-app.herokudns.com.` |
| ALIAS / ANAME | apex to name (provider feature) | `example.com ALIAS my-lb.elb.amazonaws.com` |
| MX | mail servers for a domain | `example.com MX 10 mx1.mail.com.` |
| TXT | arbitrary text | SPF, DKIM, DMARC, domain verification |
| NS | delegate a zone | `example.com NS ns-123.awsdns.com.` |
| SOA | zone metadata, negative TTL | one per zone |
| SRV | service host and port | Used by some service discovery |
| CAA | which CAs may issue certs | `example.com CAA 0 issue "letsencrypt.org"` |

- A **CNAME** can't sit at the zone apex (`example.com`) and can't coexist with other records on the same name. That is why providers offer ALIAS/ANAME or "CNAME flattening".
- Load balancers on cloud providers change IPs; always point at their hostname (CNAME or ALIAS), never hard-code the IPs.

### TTL

TTL is how long resolvers may cache the answer. It is a ceiling, not a guarantee: some resolvers and clients cache longer, and long-running processes may cache forever.

| TTL | Use for |
|---|---|
| 30–60 s | Records you may need to change fast (failover, migrations) |
| 300 s | Typical default for app endpoints |
| 3600–86400 s | Stable records (MX, TXT verification, NS) |

Low TTLs cost more queries and slightly more latency on cold lookups; high TTLs make changes slow.

### Who caches

Browser (about 1 minute in Chrome), OS (`systemd-resolved`, mDNSResponder on macOS), the recursive resolver, and your runtime. The JVM historically cached forever. Python doesn't cache itself, but connection pools hold connections to the old IP until they're closed, which has the same effect.

## In practice

### Inspect

```bash
dig api.example.com                   # answer + remaining TTL
dig +short api.example.com
dig api.example.com @1.1.1.1          # ask a specific resolver
dig api.example.com @ns-123.awsdns-15.com   # ask the authoritative server
dig +trace api.example.com            # walk from the root yourself
dig -x 203.0.113.10                   # reverse lookup (PTR)
dig example.com TXT
```

Comparing the authoritative answer with a public resolver's answer tells you whether you have a config problem or a caching problem.

### A zero-downtime endpoint migration

1. **Days before:** lower the TTL on the record from 3600 to 60. Wait at least the **old** TTL so every cache picks up the short one.
2. **Cutover:** change the record to the new target. Most traffic moves within a minute or two.
3. Keep the old target serving for a while: some clients ignore TTLs, and pooled connections stay on the old IP until recycled.
4. **After:** watch old-target traffic drain to zero, then raise the TTL back.

### DNS-based failover and routing

- **Health-checked failover** (Route 53 failover records): if the primary's health check fails, answer with the secondary. Recovery time is roughly health check interval × failure threshold + TTL.
- **Weighted records**: send 10% of resolutions to a new stack. Coarse canary, since caching resolvers make the split uneven.
- **Latency / geo routing**: answer with the closest region.

DNS failover is slow and approximate. Inside a region, rely on load balancer health checks, which react in seconds.

### DNS inside your infrastructure

- **Docker Compose**: service names resolve via Docker's embedded DNS at `127.0.0.11`. `DATABASE_HOST=db` works because of this.
- **Kubernetes**: `my-svc.my-namespace.svc.cluster.local` resolves to the Service's ClusterIP. The default `ndots:5` makes lookups of external names like `api.stripe.com` first try several cluster suffixes, multiplying query volume. Use a trailing dot (`api.stripe.com.`) or lower `ndots` for heavy outbound callers.
- **Managed databases**: failover of RDS or Cloud SQL usually works by flipping a DNS record. Your app recovers only if it reconnects and re-resolves; set pool max-lifetime (`CONN_MAX_AGE` in Django, `server_lifetime` in PgBouncer) so connections don't pin to the dead primary.

### Email deliverability records

Transactional email (password resets, invites) lands in spam without these TXT records:

```text
example.com.                 TXT "v=spf1 include:sendgrid.net -all"
s1._domainkey.example.com.   CNAME s1.domainkey.u123.wl.sendgrid.net.
_dmarc.example.com.          TXT "v=DMARC1; p=quarantine; rua=mailto:dmarc@example.com"
```

## Pitfalls

- **Changing a record with a 24 h TTL** and expecting fast cutover. Lower TTLs ahead of time.
- **Clients caching beyond TTL**: long-lived connection pools, JVM caches, mobile OSes. Keep the old endpoint alive.
- **Hard-coded LB IPs** that the cloud provider later changes.
- **Dangling CNAMEs** to deleted cloud resources (an S3 bucket, a Heroku app): an attacker claims the resource name and serves content on your subdomain. Clean up records when you delete infrastructure.
- **Resolver as a single point of failure**: if the VPC resolver or CoreDNS is overloaded, everything that calls external APIs fails with `Name or service not known`. Watch DNS error rates and query volume.
- **Negative caching** after a typo or querying a record before creating it.
- **Forgetting the registrar**: an expired domain takes everything down. Enable auto-renew and registrar lock.

## Rules of thumb

- Point at hostnames (CNAME/ALIAS), not IPs, for anything managed.
- 300 s TTL by default; 60 s for records you plan to move; lower it a full old-TTL before any change.
- Use LB health checks for fast failover, DNS for regional failover.
- Recycle pooled connections so DNS changes take effect.
- Add CAA, SPF, DKIM and DMARC; audit for dangling records.

## Interview angle

- **"What happens during DNS resolution?"** Stub resolver to recursive resolver, which walks root, TLD and authoritative servers, caching each answer for its TTL.
- **"You switched DNS to the new server but some users still hit the old one. Why?"** Caches honoring the old TTL, resolvers or clients that ignore TTL, and long-lived connections that never re-resolve.
- **"CNAME vs A record?"** A maps a name to an IP; CNAME aliases one name to another and can't be used at the zone apex, where providers offer ALIAS-style records instead.
