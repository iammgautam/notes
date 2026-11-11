---
title: CDN and object storage
section: hld
order: 13
summary: Store blobs in object storage, upload and download with presigned URLs, and serve them through a CDN with the right cache headers.
---

## What it is

- **Object storage** (S3, GCS, Azure Blob, MinIO): flat namespace of `bucket/key → bytes + metadata`. Practically unlimited, very durable (S3: 11 nines), cheap, ~10–100 ms latency.
- **CDN** (CloudFront, Cloudflare, Fastly, Akamai): edge caches near users. Serves static and cacheable content with low latency and offloads the origin.

## When to use

| Use object storage for | Not for |
|---|---|
| Images, video, PDFs, user uploads | Small, frequently updated records |
| Backups, logs, data lake files | Low-latency random writes |
| Model artifacts, RAG source documents | Anything needing transactions or queries |

| Use a CDN for | Notes |
|---|---|
| Static assets (JS, CSS, images) | Long TTL + fingerprinted filenames |
| Media downloads and video segments | Huge bandwidth savings |
| Public cacheable API responses | Short TTL or `stale-while-revalidate` |
| DDoS absorption, TLS at the edge | Even for uncacheable traffic |

## Upload and download flow

Never stream large files through your app servers. Use **presigned URLs**: a time-limited, signed URL that lets the client talk to object storage directly.

```text
1. client -> app:  POST /uploads {filename, size, type}
2. app: authz, create DB row (status=pending), sign PUT URL (5 min)
3. client -> S3:   PUT <presigned url>  (bytes go direct)
4. S3 -> event (or client -> app: POST /uploads/{id}/complete)
5. worker: verify, scan, thumbnail, mark status=ready
6. download: app returns presigned GET URL or CDN URL
```

```python
url = s3.generate_presigned_url(
    "put_object",
    Params={"Bucket": "uploads", "Key": f"{tenant_id}/{uuid4()}", "ContentType": ctype},
    ExpiresIn=300,
)
```

- Generate the key server-side (include tenant id); never trust client filenames as keys.
- Enforce size/type: presigned POST policies support `content-length-range`.
- Large files: multipart upload (parallel parts, resumable).
- Private content via CDN: CloudFront signed URLs/cookies, with the bucket only readable by the CDN (origin access control).

## Cache headers

| Header | Purpose |
|---|---|
| `Cache-Control: public, max-age=31536000, immutable` | Fingerprinted static assets (`app.3f9a2c.js`) |
| `Cache-Control: no-cache` | May store, must revalidate every time |
| `Cache-Control: no-store` | Never cache (sensitive data) |
| `Cache-Control: private` | Browser only, not CDN/shared caches |
| `s-maxage=60` | TTL for shared caches (CDN) only |
| `stale-while-revalidate=30` | Serve stale while refreshing in background |
| `ETag` / `If-None-Match` | Revalidate cheaply, `304 Not Modified` |
| `Vary: Accept-Encoding` | Cache key varies by header; avoid `Vary: Cookie` on CDN |

> **Rule of thumb:** Version URLs instead of purging. A new deploy produces new filenames; old ones can stay cached forever.

## Push vs pull CDN

| | Pull (origin pull) | Push |
|---|---|---|
| How | CDN fetches from origin on first miss | You upload content to the CDN |
| Pros | Zero management | Control, no cold miss |
| Cons | First request slow, origin load on purge | Must manage uploads |
| Default | Most web apps | Large, rarely changing files |

## Trade-offs

| Choice | Gain | Cost |
|---|---|---|
| Long TTL | Hit rate, low origin load | Hard to change content (needs versioned URLs or purge) |
| Presigned URLs | App servers skip bulk bytes | Need async verification, URL leakage risk until expiry |
| Storage classes (IA, Glacier) | 50–90% cheaper storage | Retrieval fees and latency |
| CDN for API | Latency, origin offload | Staleness, risk of caching private data |

## Gotchas

- Caching a response with `Set-Cookie` or user data at the CDN leaks it to other users. Mark personalized responses `private` or `no-store`.
- Cache key must include everything that changes the response (query params, `Accept-Language`) and nothing else (tracking params kill hit rate).
- Object storage is not a filesystem: no atomic rename, listing is slow and paginated. Keep the index in your DB.
- Avoid public buckets; block public access and serve through the CDN or presigned GETs.
- Egress costs dominate media-heavy bills; a CDN usually lowers them.

> **Interview tip:** For any design with uploads, say "presigned PUT direct to S3, async processing worker, metadata in Postgres, serve via CDN with long TTL and versioned keys".
