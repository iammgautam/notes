---
title: TLS and HTTPS
section: networking
order: 6
summary: What the TLS 1.3 handshake does, how certificate chains are validated, where to terminate TLS behind a load balancer, and how HSTS locks it in.
---

TLS gives you three things: **confidentiality** (nobody on the path can read the bytes), **integrity** (nobody can change them unnoticed) and **server authentication** (you are talking to the real `api.example.com`). In production, most TLS work is operational: certificates that expire, chains that are incomplete, proxies that lose the original scheme, and internal hops that quietly run in plaintext.

## How it works

### The TLS 1.3 handshake

```text
Client                                         Server
  | ClientHello: versions, ciphers, SNI,           |
  |   key_share (ECDHE public key), ALPN (h2)      |
  |----------------------------------------------->|
  |        ServerHello: chosen cipher, key_share   |
  |        {EncryptedExtensions, Certificate,      |
  |         CertificateVerify, Finished}           |
  |<-----------------------------------------------|
  | {Finished}  + first HTTP request (encrypted)   |
  |----------------------------------------------->|
```

1. Both sides send ephemeral Diffie-Hellman public keys (ECDHE, usually X25519) and derive the same shared secret. Nothing secret crosses the wire.
2. The server proves it owns the certificate's private key by signing the handshake transcript (`CertificateVerify`).
3. The client validates the certificate chain and hostname.
4. Both derive symmetric keys; bulk data uses AES-GCM or ChaCha20-Poly1305.

One round trip (TLS 1.2 needed two). **Forward secrecy**: because keys are ephemeral, stealing the server's private key later doesn't decrypt recorded traffic. Resumed sessions can use 0-RTT early data, which is replayable, so only safe for idempotent requests.

**SNI** (Server Name Indication) tells the server which hostname you want, so one IP can serve many certificates. **ALPN** negotiates HTTP/2 vs HTTP/1.1 inside the handshake.

### Certificates and the chain of trust

```text
Root CA (in OS/browser trust store)
  └─ Intermediate CA  (sent by your server)
       └─ Leaf: CN/SAN = api.example.com  (sent by your server)
```

The client checks: signatures up to a trusted root, validity dates, the hostname is in the **SAN** list, and the cert is not revoked. Your server must send leaf **plus intermediates** (the "fullchain"). Browsers sometimes paper over a missing intermediate; `curl`, Python `requests` and mobile clients fail.

Certificate types by validation: DV (domain control proven via ACME HTTP-01 or DNS-01 challenge) covers almost all APIs. Wildcards (`*.example.com`) need DNS-01 and cover one label only.

### Where TLS terminates

```text
Option A: terminate at the edge
client ==TLS==> LB/CDN --HTTP--> nginx --HTTP--> gunicorn

Option B: re-encrypt
client ==TLS==> LB ==TLS==> app        (zero-trust, compliance)

Option C: passthrough
client ==TLS=====================> app  (LB sees TCP only, no L7 routing)
```

Edge termination is the common default: one place to manage certificates (ACM, cert-manager), the LB can do HTTP routing, and the app stays simple. The trade-off is plaintext inside your network. Re-encrypt (or mTLS in a service mesh) when compliance or threat model requires it.

Once the LB terminates TLS, the app sees plain HTTP. It learns the original scheme from `X-Forwarded-Proto: https`.

### HSTS

```http
Strict-Transport-Security: max-age=31536000; includeSubDomains; preload
```

After seeing this once over HTTPS, the browser refuses plain HTTP to that host for `max-age` seconds and won't let users click through certificate errors. It closes the "first request over HTTP gets intercepted" gap (SSL stripping). `preload` gets you into browsers' built-in list, which is very hard to undo.

## In practice

### Django behind a TLS-terminating LB

```python
# settings.py
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000       # start with 300, raise after verifying
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = False          # opt in deliberately
```

`SECURE_PROXY_SSL_HEADER` is only safe if the proxy **overwrites** the header from clients. Otherwise anyone can claim HTTPS. Without it, `request.is_secure()` is false, `SECURE_SSL_REDIRECT` loops forever, and absolute URLs come out as `http://`.

`python manage.py check --deploy` flags missing settings.

### nginx termination

```text
server {
    listen 443 ssl;
    http2 on;
    server_name api.example.com;
    ssl_certificate     /etc/letsencrypt/live/api.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.example.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:SSL:10m;

    location / {
        proxy_pass http://app:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
server { listen 80; return 301 https://$host$request_uri; }
```

### Debugging commands

```bash
# Show the chain the server actually sends, and verify it
openssl s_client -connect api.example.com:443 -servername api.example.com -showcerts </dev/null

# Expiry dates and SANs of the leaf
openssl s_client -connect api.example.com:443 -servername api.example.com </dev/null 2>/dev/null \
  | openssl x509 -noout -dates -ext subjectAltName

# Handshake details through curl
curl -vI https://api.example.com 2>&1 | grep -E 'SSL|TLS|subject|issuer|expire'
```

### Outbound TLS from Python

```python
requests.get(url, timeout=5)                         # verifies by default
requests.get(url, verify="/etc/ssl/internal-ca.pem")  # private CA
# requests.get(url, verify=False)  # never in production
```

In slim Docker images, make sure `ca-certificates` is installed, or every outbound HTTPS call fails with `CERTIFICATE_VERIFY_FAILED`. `certifi` ships its own bundle and ignores the OS store, which matters when you add a corporate CA.

## Pitfalls

- **Expired certificates.** Still a top cause of outages. Automate renewal (ACME, ACM, cert-manager) and alert at 21 and 7 days before expiry.
- **Missing intermediate.** Works in Chrome, fails in `curl` and SDKs with "unable to get local issuer certificate". Serve `fullchain.pem`.
- **Redirect loops** behind an LB because the app doesn't trust `X-Forwarded-Proto`.
- **Mixed content.** An HTTPS page loading `http://` scripts gets blocked by the browser.
- **`verify=False`** copied from a Stack Overflow answer into production, enabling man-in-the-middle.
- **HSTS too early.** A long `max-age` with `includeSubDomains` before every subdomain supports HTTPS breaks those subdomains for a year.
- **Clock skew** in containers makes valid certificates look "not yet valid".

## Checklist

- TLS 1.2 minimum, TLS 1.3 preferred; let your LB's modern policy pick ciphers.
- Automated issuance and renewal, plus expiry alerts.
- Serve the full chain; test with `openssl s_client`.
- Terminate at the LB, set and trust `X-Forwarded-Proto` correctly, redirect HTTP to HTTPS.
- Secure cookies, HSTS rolled out with a short `max-age` first.
- Encrypt internal hops when the data or compliance needs it.

## Interview angle

- **"Walk through the TLS handshake."** ClientHello with key share, SNI and ALPN; ServerHello with its key share; server sends certificate and a signature over the transcript; both derive symmetric keys via ECDHE; one RTT in TLS 1.3.
- **"How does the client know the certificate is legit?"** It builds a chain from the leaf through intermediates to a root in its trust store, checks signatures, dates and that the hostname is in the SAN.
- **"Where would you terminate TLS?"** Usually at the LB for simplicity and L7 routing; re-encrypt or use mTLS internally when the threat model or compliance requires it.
- **"What does HSTS protect against?"** SSL stripping on the first plain-HTTP request; the browser upgrades to HTTPS itself.
