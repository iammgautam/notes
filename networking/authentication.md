---
title: Authentication and authorization
section: networking
order: 5
summary: Sessions vs JWTs, refresh tokens, OAuth2 flows, OIDC, API keys, and how to enforce RBAC and tenant isolation in a multi-tenant API.
---

Authentication answers "who is calling?". Authorization answers "may they do this, to this object, in this tenant?". Most real breaches in SaaS APIs are authorization bugs: a valid user reading another tenant's invoice by changing an id. Authentication is mostly picking a well-known mechanism and configuring it correctly; authorization is code you write and must test.

## How it works

### Sessions

The server stores session state (Django: `django_session` table, or Redis) and gives the browser an opaque random id in a cookie.

```http
Set-Cookie: sessionid=k3j2...; HttpOnly; Secure; SameSite=Lax; Path=/
```

- Revocation is instant: delete the row.
- Every request does a lookup (cheap with Redis or cached DB sessions).
- Cookies are sent automatically, so you need CSRF protection. See [CORS, CSRF and browser security]({{ '/networking/cors-and-browser-security/' | relative_url }}).

### JWT (JSON Web Token)

A signed, self-contained token: `base64url(header).base64url(payload).signature`.

```json
{
  "sub": "user_812",
  "tid": "tenant_7",
  "roles": ["admin"],
  "iat": 1762851600,
  "exp": 1762852500,
  "iss": "https://auth.example.com",
  "aud": "api"
}
```

- Any service with the public key (RS256/ES256) or shared secret (HS256) can verify it without a DB call. Useful across microservices.
- The payload is **encoded, not encrypted**. Never put secrets or PII in it.
- You cannot revoke a JWT before `exp` without reintroducing state (a denylist by `jti`, or a per-user `token_version`). So keep access tokens short-lived: 5–15 minutes.

| | Server sessions | JWT access tokens |
|---|---|---|
| State | Server-side | In the token |
| Revocation | Instant | Wait for expiry or keep a denylist |
| Cross-service verification | Needs shared session store | Verify signature locally |
| Size per request | ~32 bytes | 500 bytes to 2 KB |
| Best fit | First-party web app | APIs, mobile, service-to-service |

### Refresh tokens

Short access token (15 min) + long refresh token (days to weeks). The refresh token is opaque, stored server-side (hashed), and exchanged for a new access token.

- **Rotation**: each refresh issues a new refresh token and invalidates the old one.
- **Reuse detection**: if an already-used refresh token shows up, assume theft and revoke the whole token family.
- In a browser, keep the refresh token in an `HttpOnly; Secure; SameSite` cookie scoped to the refresh path, and the access token in memory. Not in `localStorage`, which any XSS can read.

### OAuth2

OAuth2 is **delegated authorization**: a user lets an app call an API on their behalf without sharing their password.

| Flow | Who uses it |
|---|---|
| Authorization Code + PKCE | Web apps, SPAs, mobile apps. The default. |
| Client Credentials | Service-to-service, no user involved |
| Device Code | TVs, CLIs without a browser |
| Implicit, Password grant | Deprecated. Don't use. |

Authorization Code + PKCE:

```text
SPA                    Auth server                  API
 | 1. redirect /authorize?client_id&redirect_uri
 |    &code_challenge=SHA256(verifier)&state
 |---------------------->|
 |   user logs in, consents
 |<-- 2. redirect back with ?code=abc&state
 | 3. POST /token code + code_verifier
 |---------------------->|
 |<-- 4. access_token (+ refresh, id_token)
 | 5. GET /v1/projects  Authorization: Bearer ...
 |--------------------------------------------------->|
```

PKCE proves that the party exchanging the code is the one that started the flow, so an intercepted code is useless. `state` stops CSRF on the callback.

### OIDC

OpenID Connect is an identity layer on top of OAuth2. It adds an **ID token** (a JWT about the user: `sub`, `email`, `name`), a `/userinfo` endpoint and discovery (`/.well-known/openid-configuration`). "Sign in with Google" and enterprise SSO (Okta, Azure AD) are OIDC. Use the ID token to log the user in; use the access token to call APIs.

### API keys

For server-to-server integrations by customers. Generate high-entropy random keys with a visible prefix, store only a hash, show the key once.

## In practice

### API key issuance and verification

```python
import hashlib, secrets

def issue_key(tenant):
    raw = "sk_live_" + secrets.token_urlsafe(32)
    ApiKey.objects.create(
        tenant=tenant,
        prefix=raw[:12],
        key_hash=hashlib.sha256(raw.encode()).hexdigest(),
    )
    return raw  # show once, never stored in plain text

def authenticate_key(raw):
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return ApiKey.objects.select_related("tenant").filter(
        key_hash=digest, revoked_at__isnull=True
    ).first()
```

SHA-256 is fine here because keys are long random strings, not human passwords (those need bcrypt/argon2). The prefix makes keys greppable by secret scanners and identifiable in support tickets.

### JWT verification

```python
import jwt  # PyJWT

claims = jwt.decode(
    token,
    key=public_key,
    algorithms=["RS256"],      # pin it; never accept "none" or HS/RS confusion
    audience="api",
    issuer="https://auth.example.com",
    options={"require": ["exp", "sub", "tid"]},
    leeway=30,
)
```

### Authorization layers in DRF

Three checks, in order:

1. **Authenticated**: `IsAuthenticated`.
2. **Tenant scope**: every queryset filtered by the caller's tenant.
3. **Role / object permission**: may this role do this action on this object?

```python
class HasProjectRole(BasePermission):
    write_roles = {"owner", "admin", "editor"}

    def has_object_permission(self, request, view, obj):
        membership = request.user.memberships.filter(
            tenant=request.tenant, project=obj
        ).first()
        if membership is None:
            return False
        if request.method in SAFE_METHODS:
            return True
        return membership.role in self.write_roles

class ProjectViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasProjectRole]

    def get_queryset(self):
        return Project.objects.filter(tenant=self.request.tenant)
```

RBAC (roles to permissions) covers most SaaS needs. When rules depend on attributes ("can edit if owner of the record and record not locked"), you are doing ABAC; keep those rules in one policy module, not scattered across views.

### Defense in depth: Postgres row-level security

```sql
ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON projects
  USING (tenant_id = current_setting('app.tenant_id')::bigint);
-- per request / transaction:
SET LOCAL app.tenant_id = '7';
```

RLS catches the query someone forgot to scope. It needs the app to connect as a non-owner role and to set the tenant inside each transaction (careful with PgBouncer transaction pooling: use `SET LOCAL`).

## Pitfalls

- **IDOR**: `GET /invoices/123` returns any tenant's invoice because the view used `Invoice.objects.get(pk=pk)`. Always go through the scoped queryset.
- **Long-lived JWTs** with no revocation path. A leaked token is valid for days.
- **`alg` confusion**: not pinning `algorithms` lets attackers pick a weaker one.
- **Tokens in `localStorage`** read by any XSS. Prefer `HttpOnly` cookies for browser apps.
- **Role checks only in the frontend**. The Vue app hides the button; the API still accepts the request.
- **Stale permissions in tokens**: roles baked into a JWT don't change until refresh. Keep tokens short or look up roles server-side for sensitive actions.
- **Celery tasks run without a request**: pass and re-check `tenant_id` and `user_id` explicitly in the task.

## Checklist

- Browser first-party app: session cookies (`HttpOnly`, `Secure`, `SameSite=Lax`) + CSRF.
- APIs and mobile: short access tokens + rotating refresh tokens with reuse detection.
- Third-party login: OIDC with Authorization Code + PKCE.
- Machine clients: OAuth2 client credentials or hashed, prefixed, revocable API keys.
- Tenant from the credential; scoped base queryset; object-level permission classes.
- Cross-tenant access tests for every endpoint.
- Rate-limit login, token and password-reset endpoints.

## Interview angle

- **"Sessions or JWT?"** Sessions for a first-party browser app (simple, instant revocation). JWTs when many services must verify identity without a shared store, with short expiry and refresh-token rotation.
- **"How do you log out a JWT user?"** Delete the refresh token server-side, let the short access token expire, and for immediate revocation check a `jti` denylist or a per-user token version.
- **"What is PKCE for?"** It binds the authorization code to the client that requested it, so a stolen code cannot be exchanged by an attacker. Required for public clients like SPAs and mobile apps.
- **"OAuth2 vs OIDC?"** OAuth2 grants access to APIs; OIDC adds a standardized ID token so the client also learns who the user is.
