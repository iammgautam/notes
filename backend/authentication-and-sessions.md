---
title: Authentication, authorization and sessions
section: backend
order: 48
phase: "Phase 7 · Security"
summary: Separate who the caller is from what they may do, and run server-side sessions safely with cookie flags, shared stores, fixation fixes and logout everywhere.
---

HTTP is stateless, so every request has to prove again who sent it. Authentication answers "who is this"; authorization answers "may they do this to that". Sessions are the oldest and still the simplest way to carry authentication across requests: the server remembers the login and the browser carries a random handle to it. Most real breaches in APIs are not broken cryptography but broken authorization (a valid user reading another tenant's invoice) and sloppy session handling, which is what this note is about. Token-based alternatives are in [JWT and refresh token rotation]({{ '/backend/jwt-and-refresh-tokens/' | relative_url }}).

## Authentication vs authorization

| | Authentication (authn) | Authorization (authz) |
|---|---|---|
| Question | Who is calling? | May this caller do this action on this resource? |
| Input | Credentials: password, passkey, OTP, OIDC assertion, client cert | Principal + action + resource + context |
| Output | A principal: user id, tenant, auth time, method, MFA level | Allow or deny |
| Runs | Once at login; then a cheap session or token check per request | On every action, often per object |
| Failure | `401 Unauthorized` with a `WWW-Authenticate` header | `403 Forbidden`, or `404` to hide existence |
| Models | Passwords, MFA, WebAuthn, OAuth/OIDC, mTLS | Ownership checks, RBAC, ABAC, ReBAC |

The status codes are misnamed: `401` means unauthenticated ("tell me who you are"), `403` means authenticated but not allowed. Returning `404` for objects the caller may not see avoids confirming that they exist.

Authentication strength comes from factors: something you know (password), have (phone, security key), or are (biometric unlocking a device key). Passkeys (WebAuthn) are phishing-resistant because the browser binds the signature to the site's origin. Password storage is covered in [Password hashing]({{ '/backend/password-hashing/' | relative_url }}).

Authorization comes in two granularities, and the second is where bugs live:

- **Coarse**: may this role call this endpoint at all (`IsAuthenticated`, `is_staff`, a scope check).
- **Object-level**: does *this* invoice belong to the caller's tenant, is the caller a member of *this* project. Broken Object Level Authorization is the first item in the OWASP API Security Top 10 (2023): the endpoint checks that you are logged in and then loads `/invoices/9812` for anyone.

In a multi-tenant service, take the tenant from the authenticated principal, never from the request body or a header the client controls, and enforce it in the query itself so a forgotten check cannot leak rows:

```python
# DRF, illustrative
class InvoiceViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]          # authentication gate
    serializer_class = InvoiceSerializer

    def get_queryset(self):                         # authorization: tenant scope
        return Invoice.objects.filter(tenant_id=self.request.user.tenant_id)

    def perform_create(self, serializer):           # never trust tenant_id input
        serializer.save(tenant_id=self.request.user.tenant_id)
```

Postgres row-level security adds a second net under that (see [multi-tenancy in Postgres]({{ '/postgresql/multi-tenancy/' | relative_url }})). Role and attribute models are in [RBAC and ABAC]({{ '/backend/rbac-and-abac/' | relative_url }}). Sensitive actions (changing email, adding a payout account, viewing API keys) should require step-up authentication: a recent re-entry of the password or a second factor, checked against an `auth_time` stored in the session.

## Session-based authentication

### How it works

```text
 browser                     app server                 session store
   | POST /login ----------->|                               |
   |   email + password      | verify password hash          |
   |                         | sid = 256 random bits         |
   |                         | SET sess:sha256(sid) -------->| {user 42,
   |<-- Set-Cookie: sid=...; |                               |  tenant 7,
   |    Secure; HttpOnly;    |                               |  auth_time}
   |    SameSite=Lax         |                               |
   | GET /invoices --------->|                               |
   |   Cookie: sid=...       | GET sess:sha256(sid) -------->|
   |                         |<------------------------------|
   |<-- 200 (as user 42) ----| authorize, query, respond     |
```

The session id is an opaque random value from a CSPRNG (`secrets.token_urlsafe(32)` gives 256 bits; OWASP's floor is 64 bits of entropy). It carries no meaning, so guessing or decoding it is useless. All state lives server-side: user id, tenant, authentication time and method, MFA status, CSRF secret. Keep it small, because it is read on every request.

Properties that follow from server-side state:

- **Revocation is immediate.** Delete the record and the next request is anonymous. This is the main advantage over self-contained tokens.
- **Every request does a lookup.** A Redis `GET` is well under a millisecond on a local network; a database lookup is a primary-key read. Rarely a bottleneck.
- **Storing a hash of the id** (as the demo below does) means a leaked session store or backup contains no usable cookies.

### Cookie flags

| Attribute | Effect | Recommendation |
|---|---|---|
| `Secure` | Sent only over HTTPS | Always (plus HSTS) |
| `HttpOnly` | Invisible to JavaScript | Always for session cookies |
| `SameSite=Lax` | Not sent on cross-site subrequests or POSTs; sent on top-level GET navigation | Default choice |
| `SameSite=Strict` | Never sent cross-site, even when following a link | High-value apps; breaks "logged in" on inbound links |
| `SameSite=None` | Always sent; requires `Secure` | Only for cross-site embeds and iframes |
| `Domain` | Omitted: host-only. Set: shared with all subdomains | Omit unless you need sharing |
| `Path` | Limits which paths receive it | `/` |
| `Max-Age` / `Expires` | Omitted: session cookie (cleared on browser close, but browsers restore sessions) | Set explicitly to match server expiry |
| `__Host-` name prefix | Browser enforces `Secure`, `Path=/`, no `Domain` | Use it; subdomains cannot overwrite the cookie |

What the flags do not do: `HttpOnly` stops script from *reading* the cookie, but XSS can still send requests as the user from the page. `SameSite` is not full CSRF protection: "same-site" includes sibling subdomains, `Lax` allows top-level GETs, and older clients ignore it. Keep CSRF tokens on state-changing requests and never change state on GET (see [Web vulnerabilities]({{ '/backend/web-vulnerabilities/' | relative_url }})). Chromium-based browsers treat a cookie with no `SameSite` attribute as `Lax`, but set it explicitly.

### Session stores

| Store | Lookup cost | Revocation | Scaling | Notes |
|---|---|---|---|---|
| In-process memory | Nanoseconds | Easy | Needs sticky sessions; lost on deploy | Development only |
| Relational DB (Django default) | PK read | Easy | Fine for most apps | Purge expired rows (`clearsessions`) |
| Redis | Sub-millisecond | Easy, plus native TTL | Shared by all app servers | Disable eviction or use a dedicated instance |
| Memcached | Sub-millisecond | Easy | Shared | Eviction logs users out; no persistence |
| Cache + DB (`cached_db`) | Cache hit, DB fallback | Easy | Shared | Writes go to both |
| Signed cookie (client-side) | None | Not per session | Stateless | Readable (signed, not encrypted), ~4 KB limit, replayable until expiry |

Prefer a shared store over sticky sessions: sticky sessions break on deploys, scale-in and uneven load. Signed-cookie sessions trade away the main benefit of sessions (revocation); if roles live in the cookie, a demoted user keeps admin until it expires.

### Session fixation

The attack: the attacker gets a valid session id (by visiting the site anonymously), plants it in the victim's browser (a cookie set from a compromised or user-content subdomain, or a session id accepted from a URL), and waits. The victim logs in; if the server keeps the same id and simply marks that session as authenticated, the attacker's copy is now logged in as the victim.

Defenses:

- **Issue a new session id on every privilege change**: login, completing MFA, entering sudo mode, switching tenant. Copy only harmless state (a cart) into the new session and destroy the old one.
- Never accept session ids from URLs or request parameters.
- Do not create a session for an unknown id the client presents; issue a fresh one instead.
- Use the `__Host-` prefix so subdomains cannot set the cookie at all.

Django's `login()` does the rotation for you: it calls `cycle_key()` on the session, or flushes it if a different user was logged in.

### Timeouts and logout

- **Idle timeout**: expire after N minutes without activity (minutes for banking and admin consoles, days for consumer apps). Implemented by refreshing a last-seen time on activity.
- **Absolute timeout**: expire N hours or days after login regardless of activity, so a stolen session cannot be kept alive forever.
- **Logout**: delete the server-side record, not just the cookie. Optionally send `Clear-Site-Data: "cookies", "storage"`.

### Logout everywhere

"Log out all devices" and "password changed, kill other sessions" need you to find every session for a user. Options:

- **Index by user.** Keep `user:42:sessions` as a Redis set (or a `user_id` column on a sessions table) and delete them all. Also powers an "active sessions" page with device and last-seen time.
- **Per-user epoch.** Store `session_epoch` on the user, copy it into the session at login, and reject sessions whose epoch is behind. Incrementing one integer revokes everything, and the check is free if you already load the user per request.

Django already does a version of the epoch for password changes: the session stores a hash derived from the user's password hash, `get_user` compares it on every request, and changing the password invalidates all other sessions. `update_session_auth_hash(request, user)` keeps the current one valid.

## In practice

### Runnable: a session store with rotation, timeouts and logout everywhere

```python
import hashlib
import secrets
from http.cookies import SimpleCookie


class SessionStore:
    """Server-side sessions keyed by a hash of the cookie value (Redis in prod)."""

    def __init__(self, clock, idle=30 * 60, absolute=12 * 3600):
        self.clock, self.idle, self.absolute = clock, idle, absolute
        self.sessions = {}   # sha256(sid) -> session dict
        self.by_user = {}    # user_id -> set of sha256(sid), for logout-everywhere

    @staticmethod
    def _key(sid):
        return hashlib.sha256(sid.encode()).hexdigest()  # a leaked store has no usable ids

    def create(self, data):
        sid = secrets.token_urlsafe(32)                    # 256 bits from a CSPRNG
        now = self.clock()
        self.sessions[self._key(sid)] = dict(data, created=now, seen=now)
        if "user_id" in data:
            self.by_user.setdefault(data["user_id"], set()).add(self._key(sid))
        return sid

    def get(self, sid):
        key = self._key(sid or "")
        s = self.sessions.get(key)
        now = self.clock()
        if s is None:
            return None
        if now - s["seen"] > self.idle or now - s["created"] > self.absolute:
            self.destroy(sid)
            return None
        s["seen"] = now                                    # sliding idle timeout
        return s

    def destroy(self, sid):
        s = self.sessions.pop(self._key(sid), None)
        if s and "user_id" in s:
            self.by_user.get(s["user_id"], set()).discard(self._key(sid))

    def login(self, old_sid, user_id):
        """Rotate the id on privilege change: this is the session fixation fix."""
        carried = {}
        if old_sid and (old := self.get(old_sid)):
            carried = {"cart": old.get("cart")}            # keep harmless state only
            self.destroy(old_sid)
        return self.create(dict(carried, user_id=user_id))

    def logout_everywhere(self, user_id):
        for key in self.by_user.pop(user_id, set()):
            self.sessions.pop(key, None)


def session_cookie(sid):
    c = SimpleCookie()
    c["__Host-sid"] = sid
    c["__Host-sid"].update({"path": "/", "secure": True, "httponly": True,
                            "samesite": "Lax"})
    return c.output()


if __name__ == "__main__":
    now = [0.0]
    store = SessionStore(clock=lambda: now[0])

    anon = store.create({"cart": ["sku-1"]})      # attacker could have planted this id
    sid = store.login(anon, user_id=42)
    assert sid != anon and store.get(anon) is None  # fixated id is now useless
    assert store.get(sid)["cart"] == ["sku-1"]

    header = session_cookie(sid)
    for attr in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/"):
        assert attr in header, header
    assert sid not in str(store.sessions)          # only hashes are stored

    now[0] += 29 * 60
    assert store.get(sid) is not None              # activity slides idle window
    now[0] += 31 * 60
    assert store.get(sid) is None                  # idle timeout

    laptop = store.login(None, 42)
    for _ in range(25):                            # active every 29 min ...
        now[0] += 29 * 60
        store.get(laptop)
    assert store.get(laptop) is None               # ... absolute timeout still wins

    phone = store.login(None, 42)
    laptop = store.login(None, 42)
    other_user = store.login(None, 7)
    store.logout_everywhere(42)
    assert store.get(phone) is None and store.get(laptop) is None
    assert store.get(other_user)["user_id"] == 7    # other users untouched
    print("ok", header)
```

### Django sessions (illustrative)

```python
# settings.py
SESSION_ENGINE = "django.contrib.sessions.backends.cache"   # or "cached_db"
SESSION_CACHE_ALIAS = "sessions"
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.redis.RedisCache",
                "LOCATION": "redis://redis:6379/1"},
    "sessions": {"BACKEND": "django.core.cache.backends.redis.RedisCache",
                 "LOCATION": "redis://sessions-redis:6379/0"},  # no eviction here
}
SESSION_COOKIE_NAME = "__Host-sessionid"
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True        # default
SESSION_COOKIE_SAMESITE = "Lax"       # default
SESSION_COOKIE_AGE = 60 * 60 * 24 * 7 # seconds from the last save (default 2 weeks)
SESSION_SAVE_EVERY_REQUEST = False    # True = sliding expiry, but a write per request
CSRF_COOKIE_SECURE = True
```

A per-user epoch for "log out other devices", without touching Django internals:

```python
from django.contrib.auth import logout
from django.contrib.auth.signals import user_logged_in
from django.db.models import F
from django.dispatch import receiver


@receiver(user_logged_in)
def stamp_epoch(sender, request, user, **kwargs):
    request.session["epoch"] = user.session_epoch      # new field on the user model


class SessionEpochMiddleware:                          # after AuthenticationMiddleware
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and request.session.get("epoch", 0) != user.session_epoch:
            logout(request)                            # flushes this stale session
        return self.get_response(request)


def logout_other_devices(request):
    type(request.user).objects.filter(pk=request.user.pk).update(
        session_epoch=F("session_epoch") + 1)
    request.user.refresh_from_db(fields=["session_epoch"])
    request.session["epoch"] = request.user.session_epoch   # keep this device
    request.session.cycle_key()
```

With the database backend, schedule `python manage.py clearsessions` daily; expired rows are not deleted otherwise.

## Pitfalls

- **No rotation on login.** Session fixation. Rotate on every privilege change.
- **Logout that only clears the cookie.** A copied cookie still works. Delete the server record.
- **Authorization data cached in the session.** Roles stored at login survive demotion. Load permissions per request, or bump the epoch on role change.
- **Cache eviction as a logout bug.** Sessions in a shared Redis with `allkeys-lru` vanish under memory pressure. Symptom: random logouts during traffic peaks. Use a dedicated instance with `noeviction` or `volatile-ttl`.
- **`Domain=.example.com`.** Every subdomain, including user-generated content or a forgotten marketing site, receives and can overwrite the session cookie.
- **`SameSite=None` without `Secure`.** Browsers reject the cookie; embedded flows silently break.
- **Route-level authorization only.** BOLA: correct endpoint guard, wrong object. Scope every query by owner or tenant.
- **Session writes on every request.** `SESSION_SAVE_EVERY_REQUEST=True` on a busy site turns reads into writes. Refresh the expiry at most every few minutes.
- **Concurrent requests overwriting each other.** Django sessions are not locked; two requests that both modify the session are last-write-wins. Keep mutable workflow state in tables, not the session.

## Interview questions

**What is the difference between authentication and authorization, and between 401 and 403?** Authentication establishes identity; authorization decides whether that identity may perform an action on a resource. `401` means the request lacks valid credentials and must carry `WWW-Authenticate`; `403` means the caller is known but not allowed. Use `404` when existence itself is sensitive.

**When would you pick sessions over JWTs?** For first-party web apps on one domain: immediate revocation, tiny cookies, no token storage problem in the browser, and mature framework support. JWTs help when many services must verify identity without a shared store, or for third-party API access.

**What do `HttpOnly`, `Secure` and `SameSite` protect against, and what not?** `HttpOnly` stops script from reading the cookie but not from using it via XSS. `Secure` keeps it off plain HTTP. `SameSite` reduces CSRF but does not cover same-site subdomains or GET-based state changes, so CSRF tokens remain necessary.

**What is session fixation and how do you prevent it?** An attacker plants a known session id before login and reuses it after the victim authenticates. Prevent it by issuing a new session id on login and other privilege changes, never accepting ids from URLs, and using `__Host-` cookies.

**How do sessions scale across many app servers?** A shared store (Redis or the database) keyed by session id, so any server can handle any request. Sticky sessions work but break on deploys and scale-in.

**How do you implement "log out of all devices"?** Index sessions by user and delete them, or keep a per-user epoch that sessions must match and increment it. Keep the current device by re-stamping its session.

**Where do you enforce authorization in a multi-tenant API?** In the data access path: every query is scoped by the tenant from the authenticated principal, ideally with a database-level backstop such as row-level security, plus object-level checks for anything finer than the tenant.

## Further reading

- OWASP Session Management Cheat Sheet and Authentication Cheat Sheet
- RFC 6265, "HTTP State Management Mechanism", and its successor draft RFC 6265bis (SameSite, cookie prefixes)
- Django documentation: "How to use sessions" and "User authentication in Django"
- OWASP API Security Top 10 (2023), API1: Broken Object Level Authorization
- NIST SP 800-63B, Digital Identity Guidelines: Authentication and Lifecycle Management
