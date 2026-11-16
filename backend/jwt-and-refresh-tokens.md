---
title: JWT and refresh token rotation
section: backend
order: 49
phase: "Phase 7 · Security"
summary: How JWTs are built, signed and validated, the attacks on naive verifiers, why revocation is hard, and refresh token rotation with reuse detection.
---

A JWT is a signed, self-contained statement: "the issuer says this subject has these claims until this time". Any service with the right key can verify it without calling anyone. The price: a valid token stays valid until it expires, it is easy to verify incorrectly, and browsers have no great place to keep it. The standard answer is a short-lived JWT access token plus a long-lived, opaque, rotating refresh token. Server-side sessions, the main alternative, are in [Authentication, authorization and sessions]({{ '/backend/authentication-and-sessions/' | relative_url }}); the protocols that issue these tokens are in [OAuth 2.0 and OIDC]({{ '/backend/oauth2-and-oidc/' | relative_url }}).

## JWT authentication

### Structure

A JWT is three base64url segments (no padding) joined by dots: `header.payload.signature`. The signature covers the first two segments exactly as encoded. A header looks like `{"alg": "RS256", "typ": "JWT", "kid": "2026-11"}`; a payload like this:

```json
{
  "iss": "https://auth.example.com",
  "sub": "user_8412",
  "aud": "https://api.example.com",
  "iat": 1794829500,
  "exp": 1794830400,
  "jti": "9f1c2e4a-7b1d-4c55-9a51-0d3b6e2f8a10",
  "tenant_id": "t_193",
  "scope": "invoices:read invoices:write"
}
```

Signed (JWS) is not encrypted: anyone holding the token can decode the payload, so keep secrets and sensitive personal data out of it. Encrypted JWTs (JWE) exist but are rare for access tokens.

```text
 client             auth server                      API service
   | -- login -------> | verify credentials             |
   | <-- access JWT ---| signed with private key        |
   |     (15 min)      |                                |
   | <-- refresh token | opaque, stored hashed          |
   |     (rotating)    |                                |
   | -- GET /invoices, Authorization: Bearer <jwt> ---> |
   |                   | <-- JWKS fetch (cached) -------|
   |                   |    verify sig, exp, iss, aud   |
   | <------------------------------------------- 200 --|
```

### Signing: HS256 vs RS256 vs ES256

| Alg | Primitive | Who can mint | Signature size | Use when |
|---|---|---|---|---|
| HS256 | HMAC-SHA256, shared secret | Anyone who can verify | 32 bytes | Issuer and verifiers in one trust boundary |
| RS256 | RSA PKCS#1 v1.5 + SHA-256 | Private key holder only | 256 bytes (2048-bit key) | Many verifiers; widest support |
| ES256 | ECDSA P-256 + SHA-256 | Private key holder | 64 bytes | Smaller tokens, fast signing |

PS256 (RSA-PSS) and EdDSA (Ed25519) are good choices where libraries support them. The deciding question is who verifies. With HS256 every verifier holds the signing secret, so one compromised service can mint tokens for all the others. With asymmetric algorithms the identity provider keeps the private key and publishes public keys as a JWKS document (`/.well-known/jwks.json`). HS256 keys must be at least 256 random bits (RFC 7518); a human-chosen secret can be brute-forced offline from one captured token.

Key rotation uses the `kid` header: publish the new key in the JWKS next to the old one, wait for verifier caches to pick it up, sign with the new one, and remove the old key after the longest token lifetime. Verifiers cache the JWKS and refetch on an unknown `kid`, rate-limited.

### Claims

Registered claims (RFC 7519): `iss` (issuer), `sub` (stable subject id), `aud` (intended recipient), `exp`, `nbf` (not before), `iat` (issued at), `jti` (unique token id). Common additions: `scope`, `tenant_id`, `auth_time`, `amr`. RFC 9068 access tokens also carry `client_id` and set `typ` to `at+jwt`. Keep tokens small (they ride on every request, and proxies often cap headers near 8 KB) and leave fast-changing permission lists out; resolve those server-side (see [RBAC and ABAC]({{ '/backend/rbac-and-abac/' | relative_url }})).

### Validation checklist

1. Size limit; exactly three segments of valid base64url and JSON objects.
2. `alg` is in a server-side allowlist tied to the key type. Never let the header choose the algorithm; reject `none`.
3. Key selected by `kid` from a preconfigured set (your issuer's JWKS). Ignore `jku`, `x5u` and embedded `jwk` headers.
4. Signature verifies (constant-time comparison for HMAC).
5. `exp` in the future, `nbf` in the past, with 30–60 s leeway for clock skew.
6. `iss` equals the expected issuer exactly.
7. `aud` contains this service; otherwise a token for one service replays against another.
8. `typ` matches the profile, so an OIDC ID token cannot pass as an access token.
9. Required claims present; then authorize on `sub`, `scope`, `tenant_id`.
10. For revocation: `jti` not in a denylist, `iat` after the user's "tokens valid after" time.

### Attacks on naive verifiers

- **`alg: none`.** Early libraries accepted an unsigned token whose header said no algorithm. Fixed by allowlisting.
- **Algorithm confusion (RS256 to HS256).** The server expects RS256 and calls a generic `verify(token, key)`. The attacker sends `alg: HS256`, HMAC-signed with the server's *public* key. A library that trusts the header computes HMAC with that public key and the signature matches. Fixed by pinning algorithms per key.
- **`kid` injection.** If `kid` is used in a file path or SQL query, `../../dev/null` (an empty key) or SQL injection follows. Treat `kid` as a lookup key into a fixed map.
- **`jku` / `x5u` spoofing.** The token points at an attacker's key set. Fetch keys only from configured URLs.
- **Library bugs.** CVE-2022-21449 let some Java versions accept all-zero ECDSA signatures. Patch crypto libraries.

### The revocation problem

Stateless verification never asks whether a token is still good, so logout, password change, suspension and role removal leave access tokens valid until `exp`.

| Approach | Time to revoke | Cost |
|---|---|---|
| Short access-token TTL (5–15 min) + refresh | Up to the TTL | Refresh traffic; the default |
| `jti` denylist in Redis, TTL = token's remaining life | Immediate | One lookup per request; set stays small |
| Per-user "valid after" timestamp; reject `iat` older | Immediate | Per-user lookup, cacheable for seconds |
| Opaque token + introspection (RFC 7662) | Immediate | Lookup per request: sessions again |
| Gateway session, internal JWT minted at the edge | Immediate at the edge | Gateway lookup; services stay stateless |

A per-request lookup rebuilds sessions, and that is fine: it can be a tiny cached check while the JWT still carries identity across internal hops. The last row (sometimes called the phantom token pattern) puts the lookup at the gateway, which swaps opaque client tokens for short-lived internal JWTs (see [API gateway and BFF]({{ '/backend/api-gateway-and-bff/' | relative_url }})).

## Refresh token rotation

### Why refresh tokens exist

Short access tokens limit theft, but nobody wants to log in every 15 minutes. A refresh token is a long-lived credential exchanged only at the auth server's token endpoint. It is opaque (random, not a JWT), stored hashed server-side so it is revocable, and it travels to one endpoint instead of every API.

### Rotation

With rotation, every refresh returns a new refresh token and invalidates the presented one. A stolen refresh token is then useful only until the legitimate client refreshes next. The OAuth 2.0 Security Best Current Practice (RFC 9700, 2025) requires refresh tokens issued to public clients (SPAs, mobile apps) to be either rotated or sender-constrained (DPoP, RFC 9449, or mutual TLS).

Set two lifetimes: idle per token (unused for 14 days, it expires) and absolute for the chain (60 days after login, re-authenticate regardless).

### Reuse detection and token families

All refresh tokens descended from one login form a *family*. If an already-exchanged token is presented again, two parties hold copies of the chain, the legitimate client and a thief, and the server cannot tell which is calling. So it revokes the entire family and both must log in again.

```text
 login -> RT1 --refresh--> RT2 --refresh--> RT3      family F (RT3 live)
                            ^
            attacker replays RT2 (already used)
            => reuse detected: revoke family F, RT3 dies too
            => user signs in again; alert on the event
```

The false positive is a race: two tabs refresh with the same token at once and the second looks like reuse. Make client refresh single-flight (shared across tabs with the Web Locks API), or allow a reuse interval of a few seconds during which the previous token still works; some identity providers expose this as a setting. Revoke all of a user's families on logout-everywhere, password change and suspension.

### Storage on web and mobile

| Client | Access token | Refresh token | Notes |
|---|---|---|---|
| SPA, recommended | Not in the browser | Not in the browser | Backend-for-frontend holds tokens; browser has a `__Host-` session cookie |
| SPA, token-mediating | In memory (JS variable) | `HttpOnly; Secure; SameSite=Strict` cookie, `Path=/auth/refresh` | XSS can use but not steal the refresh cookie; check `Origin` on refresh |
| iOS | Memory | Keychain | Optionally gated by biometrics |
| Android | Memory | Android Keystore-backed encrypted storage | Never plain `SharedPreferences` |

Avoid `localStorage`: any XSS exfiltrates a long-lived credential. The IETF draft "OAuth 2.0 for Browser-Based Applications" recommends the backend-for-frontend pattern as the most secure option for SPAs.

### Data model

```sql
CREATE TABLE refresh_tokens (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  family_id         uuid        NOT NULL,
  user_id           bigint      NOT NULL REFERENCES users (id),
  token_hash        bytea       NOT NULL UNIQUE,   -- sha256(token); never the raw token
  parent_id         bigint      REFERENCES refresh_tokens (id),
  issued_at         timestamptz NOT NULL DEFAULT now(),
  expires_at        timestamptz NOT NULL,          -- idle limit
  family_expires_at timestamptz NOT NULL,          -- absolute limit
  used_at           timestamptz,
  revoked_at        timestamptz
);
CREATE INDEX ON refresh_tokens (family_id);
CREATE INDEX ON refresh_tokens (user_id) WHERE revoked_at IS NULL;

-- Rotate: claim atomically. 0 rows and used_at set => reuse: revoke family.
UPDATE refresh_tokens SET used_at = now()
 WHERE token_hash = $1 AND used_at IS NULL AND revoked_at IS NULL
   AND expires_at > now()
RETURNING id, family_id, user_id, family_expires_at;

UPDATE refresh_tokens SET revoked_at = now()
 WHERE family_id = $2 AND revoked_at IS NULL;      -- on reuse
```

SHA-256 is right here, unlike for passwords: a 256-bit random token cannot be brute-forced, and a deterministic hash allows the indexed lookup.

## In practice

### Runnable: HS256 sign and verify with `hmac`

The verifier follows the checklist. The first assert reproduces jwt.io's example token, proving byte-compatibility with real libraries.

```python
import base64
import hashlib
import hmac
import json
import time


class InvalidToken(Exception):
    pass


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _segment(obj) -> str:
    return b64url(json.dumps(obj, separators=(",", ":")).encode())


def sign(claims: dict, key: bytes, kid=None) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    if kid:
        header["kid"] = kid
    signing_input = f"{_segment(header)}.{_segment(claims)}"
    sig = hmac.new(key, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{b64url(sig)}"


def verify(token, keys, *, issuer, audience, now=None, leeway=30, max_len=8192):
    """keys: kid -> secret. Every check below is on the checklist in the text."""
    if len(token) > max_len or token.count(".") != 2:
        raise InvalidToken("malformed")
    h64, p64, s64 = token.split(".")
    try:
        header, claims = json.loads(b64url_decode(h64)), json.loads(b64url_decode(p64))
        sig = b64url_decode(s64)
    except ValueError as exc:                     # bad base64, JSON or UTF-8
        raise InvalidToken("malformed") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise InvalidToken("malformed")
    if header.get("alg") != "HS256":              # allowlist; "none" and RS256 die here
        raise InvalidToken("algorithm not allowed")
    key = keys.get(header.get("kid"))             # lookup in a fixed map only
    if key is None:
        raise InvalidToken("unknown key id")
    expected = hmac.new(key, f"{h64}.{p64}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):    # constant-time compare
        raise InvalidToken("bad signature")
    now = time.time() if now is None else now
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or now > exp + leeway:
        raise InvalidToken("expired")
    if isinstance(claims.get("nbf"), (int, float)) and now + leeway < claims["nbf"]:
        raise InvalidToken("not yet valid")
    if claims.get("iss") != issuer:
        raise InvalidToken("wrong issuer")
    aud = claims.get("aud")
    if audience not in (aud if isinstance(aud, list) else [aud]):
        raise InvalidToken("wrong audience")
    return claims


def rejects(token, keys, reason, **kw):
    try:
        verify(token, keys, issuer="https://auth.example.com",
               audience="billing-api", **kw)
    except InvalidToken as exc:
        return str(exc) == reason
    return False


if __name__ == "__main__":
    # Interop check against the well-known example token from jwt.io.
    known = sign({"sub": "1234567890", "name": "John Doe", "iat": 1516239022},
                 b"your-256-bit-secret")
    assert known.endswith(".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")

    keys = {"2026-11": b"k" * 32, "2026-10": b"o" * 32}  # current + previous key
    now = 1_794_829_500
    claims = {"iss": "https://auth.example.com", "sub": "user_8412",
              "aud": "billing-api", "iat": now, "exp": now + 900,
              "tenant_id": "t_193", "scope": "invoices:read"}
    token = sign(claims, keys["2026-11"], kid="2026-11")
    ok = dict(issuer="https://auth.example.com", audience="billing-api", now=now)
    assert verify(token, keys, **ok)["sub"] == "user_8412"
    old = sign(claims, keys["2026-10"], kid="2026-10")    # rotation overlap works
    assert verify(old, keys, **ok)["tenant_id"] == "t_193"

    h64, p64, s64 = token.split(".")
    forged = _segment(dict(claims, scope="admin"))
    assert rejects(f"{h64}.{forged}.{s64}", keys, "bad signature", now=now)
    none_hdr = _segment({"alg": "none", "typ": "JWT", "kid": "2026-11"})
    assert rejects(f"{none_hdr}.{p64}.", keys, "algorithm not allowed", now=now)
    rs_hdr = _segment({"alg": "RS256", "kid": "2026-11"})  # alg confusion attempt
    assert rejects(f"{rs_hdr}.{p64}.{s64}", keys, "algorithm not allowed", now=now)
    assert rejects(sign(claims, b"x" * 32, kid="../../dev/null"), keys,
                   "unknown key id", now=now)
    assert rejects(token, keys, "expired", now=now + 900 + 31)
    assert verify(token, keys, **dict(ok, now=now + 900 + 29))  # inside leeway
    other_aud = sign(dict(claims, aud="search-api"), keys["2026-11"], kid="2026-11")
    assert rejects(other_aud, keys, "wrong audience", now=now)
    assert rejects("a.b", keys, "malformed", now=now)
    assert rejects("!!.@@.##", keys, "malformed", now=now)
    print("ok", token[:40] + "...")
```

### Runnable: refresh token rotation with reuse detection

```python
import hashlib
import secrets
import uuid

DAY = 86400


class InvalidGrant(Exception):
    """Maps to HTTP 400 {"error": "invalid_grant"} on the token endpoint."""


class RefreshTokens:
    def __init__(self, clock, idle_ttl=14 * DAY, absolute_ttl=60 * DAY):
        self.clock, self.idle_ttl, self.absolute_ttl = clock, idle_ttl, absolute_ttl
        self.rows = {}            # sha256(token) -> row; raw tokens are never stored
        self.revoked_families = set()

    @staticmethod
    def _h(token):
        return hashlib.sha256(token.encode()).hexdigest()  # high entropy: fast hash is fine

    def _issue(self, user_id, family_id, family_expires_at):
        token = secrets.token_urlsafe(32)
        now = self.clock()
        self.rows[self._h(token)] = {
            "user_id": user_id, "family_id": family_id, "used": False,
            "expires_at": min(now + self.idle_ttl, family_expires_at),
            "family_expires_at": family_expires_at,
        }
        return token

    def login(self, user_id):
        """Start a new family (one per device/login)."""
        return self._issue(user_id, str(uuid.uuid4()), self.clock() + self.absolute_ttl)

    def rotate(self, token):
        row = self.rows.get(self._h(token))
        if row is None or row["family_id"] in self.revoked_families:
            raise InvalidGrant("unknown or revoked")
        if row["used"]:
            # Someone presented a token that was already exchanged: the
            # legitimate client or a thief. We can't tell which, so kill the family.
            self.revoked_families.add(row["family_id"])
            raise InvalidGrant("reuse detected; family revoked")
        if self.clock() > row["expires_at"]:
            raise InvalidGrant("expired")
        row["used"] = True        # in SQL: UPDATE ... WHERE used_at IS NULL RETURNING
        return row["user_id"], self._issue(row["user_id"], row["family_id"],
                                           row["family_expires_at"])


def fails(store, token, reason):
    try:
        store.rotate(token)
    except InvalidGrant as exc:
        return str(exc) == reason
    return False


if __name__ == "__main__":
    now = [0.0]
    store = RefreshTokens(clock=lambda: now[0])

    rt1 = store.login(user_id=42)
    _, rt2 = store.rotate(rt1)
    user, rt3 = store.rotate(rt2)
    assert user == 42 and len({rt1, rt2, rt3}) == 3
    assert not any(t in store.rows for t in (rt1, rt2, rt3))   # only hashes stored

    laptop = store.login(user_id=42)                  # separate family
    assert fails(store, rt2, "reuse detected; family revoked")  # thief replays rt2
    assert fails(store, rt3, "unknown or revoked")    # legit successor dies too
    _, laptop2 = store.rotate(laptop)                 # other device unaffected

    now[0] += 15 * DAY                                # idle longer than 14 days
    assert fails(store, laptop2, "expired")

    phone = store.login(user_id=7)
    for _ in range(6):                                # active every 10 days ...
        now[0] += 10 * DAY
        _, phone = store.rotate(phone)
    now[0] += 1 * DAY
    assert fails(store, phone, "expired")             # ... absolute 60-day cap
    print("ok")
```

### Verifying RS256 with PyJWT (illustrative)

```python
import jwt  # PyJWT 2.x

jwks = jwt.PyJWKClient("https://auth.example.com/.well-known/jwks.json")  # caches keys


def authenticate(token: str) -> dict:
    signing_key = jwks.get_signing_key_from_jwt(token)   # by kid, from your issuer only
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],                 # pinned: never read from the header
        audience="https://api.example.com",
        issuer="https://auth.example.com",
        leeway=30,
        options={"require": ["exp", "iat", "iss", "aud", "sub"]},
    )
```

## Pitfalls

- **No revocation plan, or day-long access tokens.** Logout does nothing server-side and a fired employee keeps access for hours. Minutes-long access tokens plus revocable refresh tokens.
- **Algorithm from the header, or no `aud`/`iss` check.** See the checklist; PyJWT 2.x requires `algorithms` for this reason.
- **Fat tokens.** Symptom: sporadic 431 or 400 responses from the load balancer once headers pass its limit.
- **Refresh tokens stored raw.** A database leak becomes a mass account takeover. Store hashes.
- **Rotation without race handling.** Users with several tabs get logged out randomly. Single-flight refresh or a short reuse interval.
- **No family revocation on password change.** The attacker who caused the reset keeps a working refresh token.
- **JWKS fetched per request, or never refreshed.** Latency and an outage dependency, or total failure after key rotation. Cache with a TTL; refetch on unknown `kid`.

## Interview questions

**JWT or server-side sessions?** Sessions for first-party web apps: instant revocation, small cookies, simple. JWTs when many services or third parties must verify identity without a shared store. Many systems use both: a session at the edge, JWTs between internal services.

**HS256 or RS256?** HS256 when the issuer and all verifiers are the same service or trust boundary, since every verifier can also mint. RS256 or ES256 when multiple services verify, so only the identity provider can sign and keys can be published via JWKS.

**How do you log a user out when using JWTs?** Revoke their refresh token families so no new access tokens are issued; the current access token lives until its short `exp`. For immediate effect, check a `jti` denylist or a per-user "valid after" timestamp per request.

**What is algorithm confusion?** A verifier that trusts the header's `alg` accepts an HS256 token HMAC-signed with the (public) RSA key, so anyone can forge tokens. Pin the algorithm per key.

**Explain refresh token rotation and reuse detection.** Each refresh issues a new refresh token and invalidates the old one. If an already-used token from a login's family is presented, someone holds a copy, so the whole family is revoked.

**Where should an SPA store tokens?** Ideally nowhere: a backend-for-frontend keeps them server-side behind an `HttpOnly` session cookie. Otherwise the access token in memory and the refresh token in an `HttpOnly`, `Secure`, `SameSite=Strict` cookie scoped to the refresh path. Never `localStorage`.

## Further reading

- RFC 7519 (JSON Web Token), RFC 7515 (JSON Web Signature) and RFC 7518 (JSON Web Algorithms)
- RFC 8725, "JSON Web Token Best Current Practices"
- RFC 9700, "Best Current Practice for OAuth 2.0 Security" (refresh token rotation and sender-constraining)
- RFC 9068, "JSON Web Token (JWT) Profile for OAuth 2.0 Access Tokens"
- IETF OAuth working group draft, "OAuth 2.0 for Browser-Based Applications"
