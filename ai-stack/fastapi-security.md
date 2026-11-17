---
title: Security in FastAPI
section: ai-stack
order: 6
phase: "Phase 1 · FastAPI"
summary: JWT auth, scopes and roles as dependencies, API keys, tenant isolation, CORS, rate limits, input limits and secrets for a finance API.
---

Finance data is regulated data. Interviewers assume you can add a login endpoint; they probe whether a clerk at one client can ever see another client's invoices, whether expired tokens are rejected everywhere, and whether the LLM endpoint can run up a five-figure bill. After this note you can issue and verify JWTs, express permissions as dependencies, authenticate services with API keys, enforce tenant isolation down to the row, and harden the edges with CORS, rate limits, input limits, secrets and security headers, with tests for the 401 and 403 paths.

## Prerequisites

[Dependency injection]({{ '/ai-stack/fastapi-dependency-injection/' | relative_url }}) and [databases]({{ '/ai-stack/fastapi-databases/' | relative_url }}). Background on the protocols is in [JWT and refresh tokens]({{ '/backend/jwt-and-refresh-tokens/' | relative_url }}) and [OAuth2 and OIDC]({{ '/backend/oauth2-and-oidc/' | relative_url }}).

## The mental model: security is a chain of dependencies

FastAPI has no auth middleware and no permission classes. Each check is a dependency that either returns a value (the principal, a tenant-bound repository) or raises an `HTTPException`. Because dependencies compose and cache per request, a route declares what it needs and FastAPI builds the chain.

```text
request
  │  CORS, security headers, body size     (middleware: every request)
  ▼
rate limit            ──► 429 + Retry-After
  ▼
authenticate: who?    ──► 401 + WWW-Authenticate   (token, API key)
  ▼
authorize: may they?  ──► 403                      (scopes, roles)
  ▼
scope data: which rows? ──► 404 for other tenants  (tenant-bound repo)
  ▼
handler
```

Status codes carry meaning. **401** means "I do not know who you are" and must include a `WWW-Authenticate` header. **403** means "I know who you are and the answer is no". **404** for another tenant's object hides whether it exists at all.

## Password hashing

Store a slow, salted hash, never the password. The demo below uses stdlib `hashlib.scrypt` so it runs anywhere. In a real service use Argon2id through `pwdlib`, which current FastAPI docs recommend now that `passlib` is unmaintained:

```python
# illustrative: pip install "pwdlib[argon2]"
from pwdlib import PasswordHash

password_hash = PasswordHash.recommended()        # Argon2id with sane defaults
stored = password_hash.hash("correct horse battery")
assert password_hash.verify("correct horse battery", stored)
```

Compare secrets with `hmac.compare_digest`, and verify against a dummy hash when the username does not exist, so response time does not reveal which usernames are real. More in [password hashing]({{ '/backend/password-hashing/' | relative_url }}).

## JWTs, scopes, roles, tenants and API keys

The demo is a small invoice API with three users in two tenants:

- `/token` checks the password and issues a 15-minute HS256 JWT with `sub`, `tid` (tenant), `roles`, `scope`, `iss`, `aud`, `iat`, `exp` and `jti`.
- `get_principal` verifies signature, expiry, audience and issuer with PyJWT, turns any failure into 401, and checks OAuth2 scopes declared by the route through `SecurityScopes`.
- `require_role("controller")` is a dependency factory for role checks.
- `invoice_repo` binds a repository to the caller's tenant, so a route cannot forget the tenant filter.
- `get_service` authenticates internal callers (an ingestion worker) by API key.

```python
import hashlib
import hmac
import os
import secrets
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import (APIKeyHeader, OAuth2PasswordBearer,
                              OAuth2PasswordRequestForm, SecurityScopes)
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FIN_")
    jwt_secret: SecretStr                 # required: FIN_JWT_SECRET
    jwt_issuer: str = "finance-api"
    jwt_audience: str = "finance-web"
    access_token_minutes: int = 15


@lru_cache
def get_settings() -> Settings:
    return Settings()


SettingsDep = Annotated[Settings, Depends(get_settings)]


# ---- password hashing (stdlib scrypt; prefer pwdlib + Argon2id in production) ----
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    _, salt, digest = stored.split("$")
    candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                               n=2**14, r=8, p=1)
    return hmac.compare_digest(candidate.hex(), digest)


DUMMY_HASH = hash_password("timing-equalizer")
USERS = {
    "asha": {"hash": hash_password("asha-pw"), "tenant": "t1",
             "roles": ["ap_clerk"], "scopes": ["invoices:read"]},
    "ravi": {"hash": hash_password("ravi-pw"), "tenant": "t1",
             "roles": ["controller"], "scopes": ["invoices:read", "invoices:approve"]},
    "meera": {"hash": hash_password("meera-pw"), "tenant": "t2",
              "roles": ["ap_clerk"], "scopes": ["invoices:read"]},
}
INVOICES = {
    1: {"id": 1, "tenant": "t1", "amount": "1250.00", "status": "draft"},
    2: {"id": 2, "tenant": "t2", "amount": "980.00", "status": "draft"},
}
# service API keys: store only the SHA-256 of high-entropy keys
API_KEYS = {hashlib.sha256(b"ingest-key-0c1f9a").hexdigest(): "ingest-worker"}


# ---- tokens ----
def create_access_token(username: str, s: Settings, ttl: timedelta | None = None) -> str:
    user = USERS[username]
    now = datetime.now(UTC)
    claims = {
        "sub": username, "tid": user["tenant"], "roles": user["roles"],
        "scope": " ".join(user["scopes"]),
        "iss": s.jwt_issuer, "aud": s.jwt_audience,
        "iat": now, "exp": now + (ttl or timedelta(minutes=s.access_token_minutes)),
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(claims, s.jwt_secret.get_secret_value(), algorithm="HS256")


class Principal(BaseModel):
    sub: str
    tenant_id: str
    roles: set[str]
    scopes: set[str]


oauth2 = OAuth2PasswordBearer(
    tokenUrl="token",
    scopes={"invoices:read": "Read invoices", "invoices:approve": "Approve invoices"},
)


async def get_principal(
    security_scopes: SecurityScopes,
    token: Annotated[str, Depends(oauth2)],
    s: SettingsDep,
) -> Principal:
    challenge = "Bearer"
    if security_scopes.scopes:
        challenge = f'Bearer scope="{security_scopes.scope_str}"'
    try:
        claims = jwt.decode(
            token, s.jwt_secret.get_secret_value(), algorithms=["HS256"],
            audience=s.jwt_audience, issuer=s.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "tid"]},
        )
    except jwt.InvalidTokenError:  # expired, bad signature, wrong aud/iss, malformed
        raise HTTPException(401, "invalid or expired token",
                            headers={"WWW-Authenticate": challenge})
    principal = Principal(sub=claims["sub"], tenant_id=claims["tid"],
                          roles=set(claims.get("roles", [])),
                          scopes=set(claims.get("scope", "").split()))
    missing = set(security_scopes.scopes) - principal.scopes
    if missing:
        raise HTTPException(403, f"missing scope: {' '.join(sorted(missing))}",
                            headers={"WWW-Authenticate": challenge})
    return principal


def require_role(role: str):
    async def check(p: Annotated[Principal, Security(get_principal)]) -> Principal:
        if role not in p.roles:
            raise HTTPException(403, f"requires role {role}")
        return p
    return check


# ---- tenant scoping: every query goes through a repo bound to one tenant ----
class InvoiceRepo:
    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id

    def list(self) -> list[dict]:
        return [i for i in INVOICES.values() if i["tenant"] == self.tenant_id]

    def get(self, invoice_id: int) -> dict | None:
        inv = INVOICES.get(invoice_id)
        return inv if inv and inv["tenant"] == self.tenant_id else None


def invoice_repo(
    p: Annotated[Principal, Security(get_principal, scopes=["invoices:read"])],
) -> InvoiceRepo:
    return InvoiceRepo(p.tenant_id)


RepoDep = Annotated[InvoiceRepo, Depends(invoice_repo)]
api_key_header = APIKeyHeader(name="X-API-Key")


async def get_service(key: Annotated[str, Depends(api_key_header)]) -> str:
    name = API_KEYS.get(hashlib.sha256(key.encode()).hexdigest())
    if name is None:
        raise HTTPException(401, "invalid API key", headers={"WWW-Authenticate": "APIKey"})
    return name


app = FastAPI()


@app.post("/token")
async def login(form: Annotated[OAuth2PasswordRequestForm, Depends()], s: SettingsDep):
    user = USERS.get(form.username)
    # verify against a dummy hash for unknown users so timing does not leak usernames
    ok = verify_password(form.password, user["hash"] if user else DUMMY_HASH)
    if not (user and ok):
        raise HTTPException(401, "incorrect username or password",
                            headers={"WWW-Authenticate": "Bearer"})
    return {"access_token": create_access_token(form.username, s), "token_type": "bearer"}


@app.get("/invoices")
async def list_invoices(repo: RepoDep) -> list[dict]:
    return repo.list()


@app.get("/invoices/{invoice_id}")
async def get_invoice(invoice_id: int, repo: RepoDep) -> dict:
    inv = repo.get(invoice_id)
    if inv is None:  # 404, not 403: do not confirm another tenant's IDs exist
        raise HTTPException(404, "invoice not found")
    return inv


@app.post("/invoices/{invoice_id}/approve")
async def approve(
    invoice_id: int,
    repo: RepoDep,
    p: Annotated[Principal, Security(get_principal, scopes=["invoices:approve"])],
    _: Annotated[Principal, Depends(require_role("controller"))],
) -> dict:
    inv = repo.get(invoice_id)
    if inv is None:
        raise HTTPException(404, "invoice not found")
    inv["status"] = f"approved by {p.sub}"
    return inv


@app.post("/internal/reindex")
async def reindex(service: Annotated[str, Depends(get_service)]) -> dict:
    return {"queued_by": service}


if __name__ == "__main__":
    os.environ["FIN_JWT_SECRET"] = secrets.token_urlsafe(32)
    s = get_settings()
    assert "**********" in repr(s.jwt_secret)  # SecretStr never prints the value
    c = TestClient(app)

    def login_as(user: str) -> dict:
        r = c.post("/token", data={"username": user, "password": f"{user}-pw"})
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    asha, ravi, meera = login_as("asha"), login_as("ravi"), login_as("meera")

    # 401: no token, wrong password, garbage, expired, forged signature
    r = c.get("/invoices")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    assert c.post("/token", data={"username": "asha", "password": "x"}).status_code == 401
    assert c.post("/token", data={"username": "nobody", "password": "x"}).status_code == 401
    assert c.get("/invoices", headers={"Authorization": "Bearer abc"}).status_code == 401
    expired = create_access_token("asha", s, ttl=timedelta(seconds=-5))
    assert c.get("/invoices", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    forged = jwt.encode({"sub": "asha", "tid": "t2", "iat": 0, "exp": 2**31,
                         "aud": s.jwt_audience, "iss": s.jwt_issuer},
                        "attacker-secret-attacker-secret-32", algorithm="HS256")
    assert c.get("/invoices", headers={"Authorization": f"Bearer {forged}"}).status_code == 401

    # tenant isolation: list is filtered, cross-tenant read is 404
    assert [i["id"] for i in c.get("/invoices", headers=asha).json()] == [1]
    assert c.get("/invoices/1", headers=meera).status_code == 404
    assert c.get("/invoices/1", headers=asha).status_code == 200

    # 403: authenticated but lacking the scope
    r = c.post("/invoices/1/approve", headers=asha)
    assert r.status_code == 403 and "invoices:approve" in r.json()["detail"]
    assert c.post("/invoices/1/approve", headers=ravi).json()["status"] == "approved by ravi"

    # API key for service-to-service calls
    assert c.post("/internal/reindex").status_code == 401  # 403 in older FastAPI
    assert c.post("/internal/reindex", headers={"X-API-Key": "nope"}).status_code == 401
    ok = c.post("/internal/reindex", headers={"X-API-Key": "ingest-key-0c1f9a"})
    assert ok.json() == {"queued_by": "ingest-worker"}
    print("ok")
```

Things to notice:

- `algorithms=["HS256"]` pins what you accept; PyJWT 2 refuses to decode without it. Unpinned, old JWT libraries accepted `alg: none` or let an attacker switch RS256 to HS256 using the public key as the secret.
- `options={"require": [...]}` rejects tokens that simply omit `exp`. PyJWT only validates claims that are present unless you require them.
- `Security(get_principal, scopes=[...])` is `Depends` plus scope metadata. `SecurityScopes` collects the scopes declared along that dependency path, and they appear in the OpenAPI docs.
- Dependency results are cached per request, but a dependency that reads `SecurityScopes` is cached per distinct scope set. In `approve`, `get_principal` and the `oauth2` extractor under it run three times (read scope via the repo, approve scope, role check). Decoding a JWT costs microseconds, so that is fine. If verification were a remote introspection call, cache the result on `request.state` or move it into a scope-free dependency.
- A missing `X-API-Key` returns **401** in current FastAPI. Older versions returned 403 when `APIKeyHeader` or `HTTPBearer` credentials were missing; tests written against them need updating.
- `Principal` carries `tenant_id` from the token. Never trust a tenant ID from the URL, the body or a header the client controls.

### Tokens from a real identity provider (illustrative)

With Entra ID, Okta or Auth0 issuing tokens, your API only verifies RS256 signatures against the provider's published keys:

```python
# illustrative: needs network access to the identity provider
import jwt

jwks = jwt.PyJWKClient(
    "https://login.microsoftonline.com/<tenant-id>/discovery/v2.0/keys",
    cache_keys=True,
)

def verify(token: str) -> dict:
    signing_key = jwks.get_signing_key_from_jwt(token)   # picks key by `kid`
    return jwt.decode(token, signing_key.key, algorithms=["RS256"],
                      audience="api://finance-api",
                      issuer="https://login.microsoftonline.com/<tenant-id>/v2.0")
```

Swap this into `get_principal`, map the provider's claim names (Entra ID uses `scp` for scopes and `roles`), and no route changes. That is the payoff of auth as a dependency.

## Row-level access control for finance data

The repository pattern above is the application-level guard. For regulated data add a second, independent guard in PostgreSQL so a missed filter in code still cannot leak rows:

```sql
-- illustrative: PostgreSQL row-level security
ALTER TABLE invoices ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoices FORCE ROW LEVEL SECURITY;   -- applies to the table owner too
CREATE POLICY tenant_isolation ON invoices
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);  -- unset: no rows
```

```python
# illustrative: in the session dependency, after authentication
await session.execute(
    text("SELECT set_config('app.tenant_id', :tid, true)"),  # true = this transaction only
    {"tid": principal.tenant_id},
)
```

The application must connect as a role without `BYPASSRLS`, and the setting must be transaction-local so a pooled connection never carries one tenant's ID into another request. More in [PostgreSQL multi-tenancy]({{ '/postgresql/multi-tenancy/' | relative_url }}) and [RBAC and ABAC]({{ '/backend/rbac-and-abac/' | relative_url }}).

The same rule applies to RAG: filter retrieval by tenant and document entitlements **before** chunks reach the model. A system prompt saying "only answer about tenant t1" is not access control. See [security and guardrails]({{ '/llm/security-and-guardrails/' | relative_url }}).

## CORS, rate limits, input limits and headers

These protect the edges and the wallet. LLM endpoints are expensive per call, so limits matter more than on a CRUD API.

```python
import time
from collections import defaultdict, deque
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

MAX_BODY_BYTES = 64 * 1024
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
}


class SlidingWindowLimiter:
    """In-process limiter. Correct for one worker only; use Redis for more."""

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit, self.window = limit, window_s
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    async def __call__(self, request: Request) -> None:  # async: no thread races
        key = request.client.host  # real code: principal.sub or tenant_id
        now = time.monotonic()
        q = self.hits[key]
        while q and q[0] <= now - self.window:
            q.popleft()
        if len(q) >= self.limit:
            retry = int(self.window - (now - q[0])) + 1
            raise HTTPException(429, "rate limit exceeded",
                                headers={"Retry-After": str(retry)})
        q.append(now)


ask_limit = SlidingWindowLimiter(limit=5, window_s=60)


class AskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=3, max_length=2000)
    document_ids: list[int] = Field(default_factory=list, max_length=20)
    top_k: int = Field(default=5, ge=1, le=20)


app = FastAPI(docs_url=None, redoc_url=None)  # hide docs on public deployments


@app.middleware("http")
async def harden(request: Request, call_next):
    length = request.headers.get("content-length")
    if length and int(length) > MAX_BODY_BYTES:
        return JSONResponse({"detail": "request body too large"}, status_code=413)
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    return response


# added last = outermost, so even 413/429 responses carry CORS headers
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://finance.example.com"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.post("/ask", dependencies=[Depends(ask_limit)])
async def ask(body: AskIn) -> dict:
    return {"answer": f"stub answer using top {body.top_k} chunks"}


if __name__ == "__main__":
    c = TestClient(app)
    ok = {"question": "What is our net-30 policy?"}

    pre = c.options("/ask", headers={"Origin": "https://finance.example.com",
                                     "Access-Control-Request-Method": "POST"})
    assert pre.headers["access-control-allow-origin"] == "https://finance.example.com"
    evil = c.options("/ask", headers={"Origin": "https://evil.example",
                                      "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in evil.headers

    r = c.post("/ask", json=ok)
    assert r.status_code == 200 and r.headers["x-content-type-options"] == "nosniff"
    assert c.post("/ask", json={"question": "x" * 5000}).status_code == 422
    assert c.post("/ask", json={**ok, "role": "admin"}).status_code == 422  # extra=forbid
    assert c.post("/ask", content=b"{" + b" " * 70_000 + b"}",
                  headers={"Content-Type": "application/json"}).status_code == 413

    ask_limit.hits.clear()
    codes = [c.post("/ask", json=ok).status_code for _ in range(6)]
    assert codes == [200] * 5 + [429]
    assert int(c.post("/ask", json=ok).headers["retry-after"]) > 0
    print("ok")
```

Notes on each piece:

- **CORS** is a browser rule, not authentication: it stops another origin's page from reading your responses with the user's credentials, and does nothing against `curl`. List exact origins: with `"*"` plus `allow_credentials=True`, Starlette reflects whatever origin asks, so any site can make credentialed calls.
- **Middleware order:** the last `add_middleware` call is the outermost layer. CORS goes outermost so error responses (413, 429, 500) still carry CORS headers and the browser can show the real error.
- **Rate limits:** the in-process limiter is correct for one worker. With four workers in each of three pods, the real limit is 12 times what you set. In production, layer them: the gateway or ingress (per IP or key), Redis counters shared by all processes (`slowapi`, or `INCR` plus `EXPIRE`), and per-tenant token budgets on LLM routes charged from the provider's reported usage. Dependencies run before body validation, so malformed requests count too.
- **Input limits:** Pydantic `max_length`, `ge`/`le` and `extra="forbid"` bound what the handler and the prompt can receive. A 2,000-character question cap and `top_k <= 20` keep prompt size and cost predictable. The `Content-Length` check rejects large bodies before parsing; chunked uploads have no `Content-Length`, so also set `client_max_body_size` at the proxy.
- **Security headers:** for a JSON API, `nosniff`, `no-store` on responses with financial data, HSTS (only when served over HTTPS), and a locked-down CSP. Turn off `/docs` on public deployments or put it behind auth; keep the OpenAPI schema available internally.

## Secrets with pydantic-settings

`Settings` in the first demo reads `FIN_JWT_SECRET` from the environment, has no default (the app refuses to start without it), and stores it as `SecretStr`, which prints as `**********` in logs, tracebacks and `repr`. Call `.get_secret_value()` only at the point of use. In production the variables come from a secret manager (AWS Secrets Manager, Azure Key Vault, Kubernetes secrets) injected at deploy time; `.env` files are for local development only and never committed. Rotate signing keys by putting a `kid` header in tokens and accepting the previous key until old tokens expire. See [API keys and secrets]({{ '/backend/api-keys-and-secrets/' | relative_url }}).

## Coming from Django

| Django / DRF | FastAPI |
|---|---|
| `AuthenticationMiddleware`, `request.user` | `get_principal` dependency returning a typed `Principal` |
| DRF `authentication_classes` | `OAuth2PasswordBearer`, `HTTPBearer`, `APIKeyHeader` dependencies |
| DRF `permission_classes`, `has_object_permission` | `Security(..., scopes=)`, role dependencies, tenant-bound repositories |
| `make_password` / `check_password` | `pwdlib` (Argon2id) |
| DRF throttling classes | gateway limits, Redis counters, `slowapi`, a limiter dependency |
| `django-cors-headers` | `CORSMiddleware` |
| `SecurityMiddleware`, `SECURE_*` settings | your own header middleware plus proxy config |
| `settings.SECRET_KEY` | `pydantic-settings` with `SecretStr` |
| CSRF middleware | not needed for bearer tokens in headers; needed again if you put tokens in cookies |

Django protects you by default and you opt out; FastAPI protects nothing by default and you opt in. Use a router-level dependency (`APIRouter(dependencies=[Security(get_principal)])`) so new routes are authenticated unless someone deliberately opts out.

## Common mistakes

- **Decoding without `algorithms=`** or with `options={"verify_signature": False}` left over from debugging. Pin the algorithm and require `exp`.
- **Tenant ID from the request.** `GET /tenants/t2/invoices` with a t1 token must fail. Take the tenant from the verified token only.
- **403 for other tenants' objects.** It confirms the ID exists. Return 404.
- **Long-lived access tokens** with no revocation. Keep access tokens to minutes, use refresh tokens, and keep a `jti` denylist for emergencies.
- **Logging the `Authorization` header** or the full prompt with customer data. Redact in the logging filter, not at each call site.
- **Per-process rate limits** behind a load balancer, reported as the global limit.
- **Hashing passwords with SHA-256.** Fast hashes are brute-forced on GPUs. Use Argon2id, scrypt or bcrypt.

## Practice tasks

1. Add a refresh-token flow: `/token` returns a refresh token stored hashed server-side, `/token/refresh` rotates it. Done: a reused refresh token returns 401 and revokes the whole family.
2. Add an approval limit: controllers approve up to 50,000, larger invoices need role `cfo`. Done: tests for 49,999, 50,000 and 50,001 pass.
3. Replace `SlidingWindowLimiter` with a Redis-backed fixed window keyed by tenant. Done: two app instances in a test share the limit.
4. Add PostgreSQL row-level security to the invoices table from the databases note and set `app.tenant_id` in the session dependency. Done: a deliberately unfiltered `select(Invoice)` returns only the caller's rows.
5. Add a `/ask` RAG route that filters chunks by tenant and per-document ACL before calling a fake model. Done: a test proves a restricted document's text never appears in the prompt for an unauthorized user.

## Interview questions

**401 or 403?**
401: not authenticated (missing, invalid or expired credentials), with a `WWW-Authenticate` header. 403: authenticated but not allowed. For another tenant's object, 404.

**How do you validate a JWT properly?**
Verify the signature with a pinned algorithm list, then `exp`, `iat`, `aud` and `iss`, and require the claims you depend on. For provider tokens, fetch the signing key by `kid` from the JWKS endpoint and cache it.

**How are permissions modeled in FastAPI?**
As dependencies. Scopes via `Security(..., scopes=[...])` and `SecurityScopes`, roles via a dependency factory, data access via repositories bound to the principal's tenant. Router-level dependencies make auth the default.

**How do you guarantee tenant isolation for finance data?**
Tenant ID from the verified token only, every query through a tenant-bound repository, PostgreSQL row-level security as a second layer, transaction-local `set_config`, and tests that try cross-tenant reads. For RAG, filter retrieval before the model sees anything.

**Why hash API keys?**
A database leak should not leak working credentials. Keys are high-entropy, so a single SHA-256 is enough (unlike passwords), and lookups stay a simple index hit.

**How would you rate limit an LLM endpoint?**
Request limits at the gateway, plus per-tenant token budgets in Redis charged from actual usage, plus a cap on concurrent streams per user. Return 429 with `Retry-After`.

**What does CORS protect against?**
A malicious page in a user's browser reading responses from your API with the user's credentials. It does not stop direct calls; it is not authentication.

## Further reading

- FastAPI docs: "Security", "OAuth2 with Password (and hashing), Bearer with JWT tokens", "OAuth2 scopes", "CORS (Cross-Origin Resource Sharing)", "Settings and Environment Variables"
- PyJWT docs: "Usage Examples" and "API Reference" (`PyJWKClient`)
- pydantic-settings docs: "Settings Management"
- PostgreSQL docs: "Row Security Policies"
- OWASP API Security Top 10
- Notes: [rate limiting]({{ '/backend/rate-limiting/' | relative_url }}), [web vulnerabilities]({{ '/backend/web-vulnerabilities/' | relative_url }})
