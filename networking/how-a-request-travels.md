---
title: How a request travels
section: networking
order: 1
summary: Follow one API call from DNS to TCP, TLS, HTTP, load balancer, app and database and back, and know where the milliseconds go.
---

When an API call "is slow", the time went somewhere specific: a DNS lookup, a TCP handshake to a far region, a TLS negotiation, a queue in front of your workers, a pool wait, a sequential scan. Knowing the full path lets you attribute latency instead of guessing, and it is the backbone answer to "what happens when you type a URL".

## How it works

```text
client                                                   server side
  |  1. DNS: api.example.com -> 203.0.113.10
  |  2. TCP: SYN / SYN-ACK / ACK            (1 RTT)
  |  3. TLS 1.3: ClientHello / ServerHello  (1 RTT)
  |  4. HTTP request  ------------------>  LB / reverse proxy (TLS ends)
  |                                          |  5. pick backend, forward
  |                                          v
  |                                       nginx -> gunicorn worker
  |                                          |  6. Django middleware,
  |                                          |     auth, view, serializer
  |                                          v
  |                                       PgBouncer -> PostgreSQL
  |  <------------------ 7. response bytes back the same path
```

### 1. DNS

The client asks its stub resolver, which checks the OS cache, then a recursive resolver (ISP, 8.8.8.8, VPC resolver), which walks root → TLD → authoritative servers if it has nothing cached. Cached answers cost ~0 ms; a cold recursive lookup costs 20–200 ms. TTLs decide how long answers live. See [DNS]({{ '/networking/dns/' | relative_url }}).

### 2. TCP handshake

SYN, SYN-ACK, ACK: one round trip before any data. RTT is set by physics: about 1 ms inside a datacenter, 60–80 ms across the US, 150–250 ms India to US-East. New connections also start in TCP slow start, so the first response bytes trickle until the congestion window grows.

### 3. TLS handshake

TLS 1.3 adds one more RTT (TLS 1.2 adds two). The server sends its certificate chain, both sides derive session keys. Session resumption and 0-RTT shave this on repeat visits. See [TLS and HTTPS]({{ '/networking/tls-and-https/' | relative_url }}).

### 4. HTTP request

The request line, headers and body go out. With HTTP/1.1 keep-alive or HTTP/2, steps 2–3 are paid once per connection, not per request. That is the single biggest client-side latency win.

### 5. Load balancer and reverse proxy

A cloud LB (ALB, GCP LB) or nginx terminates TLS, applies routing rules, picks a healthy backend (round robin, least connections) and opens or reuses a connection to it. It adds `X-Forwarded-For` and `X-Forwarded-Proto` so the app knows the real client IP and scheme.

### 6. Application

nginx buffers the request and hands it to a gunicorn worker. If every worker is busy, the request **waits in the listen backlog**: this queue time is invisible in your Django timings. Then middleware (sessions, auth, tenant resolution), the view, the ORM, serialization.

### 7. Database

The ORM borrows a connection (from Django's persistent connection or PgBouncer), sends the query, Postgres plans and executes it, rows stream back. Each query is at least one network round trip, typically 0.3–1 ms in-region. 50 queries in an N+1 loop is 50 round trips before any execution time.

Then everything unwinds: JSON rendered, bytes written to the socket, the LB forwards them, and the client parses the response.

## In practice

### Measure the client-side phases with curl

```bash
curl -s -o /dev/null https://api.example.com/v1/health -w '
dns:        %{time_namelookup}s
tcp:        %{time_connect}s
tls:        %{time_appconnect}s
ttfb:       %{time_starttransfer}s
total:      %{time_total}s
'
```

The values are cumulative. `ttfb - tls` is roughly "server think time plus one RTT". If `tcp` is already 200 ms, no amount of query tuning will fix the user experience; you need a closer region or a CDN.

### Measure the server-side phases

```text
# nginx log_format with upstream timing
log_format timed '$remote_addr "$request" $status '
                 'rt=$request_time urt=$upstream_response_time '
                 'uct=$upstream_connect_time';
```

- `request_time` large but `upstream_response_time` small: slow client or slow upload.
- `upstream_connect_time` large: workers saturated, backlog full.
- `upstream_response_time` large: look inside the app (APM spans, `pg_stat_statements`).

In Django, a Sentry or OpenTelemetry trace breaks the app time into middleware, view, and each SQL span. That is where N+1 queries and slow serializers show up.

### A rough latency budget

| Phase | Same region | Cross-continent |
|---|---|---|
| DNS (cached / cold) | 0 / 20–100 ms | 0 / 100–200 ms |
| TCP handshake | ~1 ms | 150–250 ms |
| TLS 1.3 | ~1 ms | 150–250 ms |
| LB + proxy hop | 0.5–2 ms | 0.5–2 ms |
| Django view, no DB | 5–30 ms | 5–30 ms |
| Each SQL round trip | 0.3–1 ms | 0.3–1 ms |

On a cold connection from far away, the handshakes alone can cost more than the whole server side.

## Pitfalls

- **Queue time is invisible.** Your APM says the view took 40 ms, users see 2 s. Requests waited for a free gunicorn worker. Watch nginx `upstream_connect_time`, set `X-Request-Start` at the proxy and compute queue time, and alert on worker saturation.
- **No connection reuse in service-to-service calls.** Calling `requests.get()` per request opens a fresh TCP+TLS connection each time. Use a module-level `requests.Session()` or `httpx.Client()` so the pool keeps connections alive.
- **DB connection churn.** With `CONN_MAX_AGE=0`, Django opens a new Postgres connection per request (several ms plus backend process fork). Use `CONN_MAX_AGE` or PgBouncer.
- **Wrong client IP.** Without trusting `X-Forwarded-For` from the LB only, rate limits and audit logs see the LB's IP. Configure the trusted proxy count, never trust the header from arbitrary clients.
- **Timeouts that don't line up.** LB idle timeout 60 s, gunicorn `--timeout 30`, client timeout 120 s: the client waits for a response the server already gave up on. Make timeouts shrink as you go deeper (client > LB > app > DB `statement_timeout`).

## Checklist

- Reuse connections everywhere: keep-alive at the client, pooled HTTP clients between services, persistent DB connections.
- Log `request_time`, `upstream_connect_time` and `upstream_response_time` at the proxy.
- Trace inside the app with spans for every SQL query and outbound HTTP call.
- Put users close to the server (region choice, CDN for static and cacheable responses).
- Align timeouts from the outside in, each layer shorter than the one in front of it.
- Alert on worker saturation, not just CPU.

## Interview angle

- **"What happens when you type a URL and press Enter?"** DNS resolution, TCP handshake, TLS handshake, HTTP request, LB routing, app processing, DB queries, response, browser parsing and sub-resource fetches. Mention caches at each step and where latency goes.
- **"An endpoint is slow only for some users. How do you debug?"** Split client-side (DNS, connect, TLS via `curl -w` or browser timing) from server-side (proxy and APM). Region distance and cold connections are classic causes.
- **"Why does HTTP keep-alive matter?"** It removes the TCP and TLS round trips from every request after the first, which dominates latency on high-RTT links.
