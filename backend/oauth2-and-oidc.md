---
title: OAuth 2.0 and OpenID Connect
section: backend
order: 50
phase: "Phase 7 · Security"
summary: OAuth 2.0 roles, grants, scopes and tokens, why PKCE replaced implicit, and how OpenID Connect adds login, ID tokens, discovery and SSO.
---

A third-party app needs to read a user's calendar without seeing their password, a CLI needs to log in on a headless box, a nightly job needs to call another service, and employees want to sign in once through their company's identity provider. OAuth 2.0 solves *delegated authorization*: the user grants a client a scoped, revocable token to call an API on their behalf. It says nothing about who the user is. OpenID Connect (OIDC) is the identity layer on top, adding a signed ID token; it is what "Sign in with Google" uses. Sessions and JWT mechanics are covered in [Authentication and sessions]({{ '/backend/authentication-and-sessions/' | relative_url }}) and [JWT and refresh tokens]({{ '/backend/jwt-and-refresh-tokens/' | relative_url }}).

## OAuth 2.0 roles

| Role | What it is | Example |
|---|---|---|
| Resource owner | The user who owns the data and grants access | A Gmail user |
| Client | The app requesting access on the owner's behalf | A CRM that syncs contacts |
| Authorization server (AS) | Authenticates the user, gets consent, issues tokens | Google, Okta, Auth0, Keycloak, Entra ID |
| Resource server (RS) | The API that accepts access tokens | Google Contacts API, your own API |

**Confidential clients** run on a server and can hold a credential (a client secret, or better, a private key for `private_key_jwt` or mTLS). **Public clients** (SPAs, mobile apps, CLIs) run on user devices where anything shipped can be extracted, so they cannot hold a secret. That split decides which flows are safe. Keep AS and RS logically separate even when one product does both: the RS validates tokens and never sees passwords.

## Authorization code flow with PKCE

This is the default flow for anything with a user and a browser. The browser only carries a short-lived, single-use **authorization code**; the tokens travel on a back channel directly from the AS to the client.

```text
 Browser            Client                   Auth server        API
    |                   |                         |               |
    |-- click login --->|                         |               |
    |                   | verifier, state, nonce  |               |
    |                   | challenge=S256(verifier)|               |
    |<-- 302 /authorize?code_challenge=..&state=..|               |
    |-------------------------------------------->|               |
    |                   |         login + consent |               |
    |<-- 302 redirect_uri?code=abc&state=xyz -----|               |
    |------------------>| check state             |               |
    |                   |-- POST /token --------->|               |
    |                   |   code + code_verifier  | S256(verifier)|
    |                   |                         | == challenge? |
    |                   |<-- access, refresh, id -|               |
    |                   |-- GET /contacts  Bearer access -------->|
```

Step by step:

1. The client generates a random `code_verifier` (43 to 128 characters), computes `code_challenge = BASE64URL(SHA256(code_verifier))`, and also generates `state` (and `nonce` for OIDC). It stores all three in the user's server-side session.
2. It redirects to `/authorize` with `response_type=code`, `client_id`, `redirect_uri`, `scope`, `state`, `code_challenge`, `code_challenge_method=S256`.
3. The AS authenticates the user, shows consent, and redirects back with `code` and `state`.
4. The client checks `state` against the session, then POSTs `grant_type=authorization_code`, `code`, `redirect_uri` and `code_verifier` (plus client authentication if confidential).
5. The AS hashes the verifier, compares it to the stored challenge, checks the code is unused, unexpired and bound to this client and redirect URI, then returns tokens.

**Why PKCE (RFC 7636).** Without it, anyone who steals the code (a malicious mobile app registered for the same URL scheme, a log line, a `Referer` header) can redeem it. With PKCE the code is useless without the verifier, which never left the client. It also stops *code injection* into a victim's session against confidential clients, which is why current guidance requires PKCE for every client. Always use `S256`.

## Client credentials flow

For machine-to-machine calls with no user: a billing service calling an invoicing service, a cron job calling an internal API.

```http
POST /oauth/token HTTP/1.1
Host: auth.example.com
Authorization: Basic base64(client_id:client_secret)
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&scope=invoices:write
```

- The token represents the client itself (`sub` is the client), so authorization uses the client's scopes or roles.
- No refresh token; request a new access token near expiry, and cache it rather than fetching one per request.
- Prefer `private_key_jwt` or mTLS over shared secrets. Cloud and Kubernetes workload identity removes the stored secret entirely.

## Device authorization flow

RFC 8628, for input-constrained devices or headless CLIs: smart TVs, `gh auth login`, `az login --use-device-code`.

1. Device POSTs to `/device_authorization` with its `client_id` and scopes.
2. AS returns `device_code` (secret, for polling), `user_code` (short, like `WDJB-MJHT`), `verification_uri`, `expires_in` and a polling `interval`.
3. Device shows "Go to example.com/device and enter WDJB-MJHT".
4. User completes login and consent on their phone or laptop.
5. Meanwhile the device polls `/token` with `grant_type=urn:ietf:params:oauth:grant-type:device_code`. It receives `authorization_pending` until approval, `slow_down` if it polls too fast (add 5 seconds to the interval), then tokens, or `access_denied` / `expired_token`.

The main risk is phishing: an attacker starts a flow and tricks a victim into entering the code. Keep codes short-lived, show the client name and scopes on the verification page, and don't use device flow where a browser redirect works.

## Scopes

Scopes are strings the client requests and the user consents to, like `contacts.readonly`, `repo`, `invoices:write`, `openid email`. The AS may grant fewer scopes than requested; the token response's `scope` field tells the client what it actually got.

- Scopes limit what the **client** may do for the user; they are not the user's permissions. The effective permission is the intersection of token scopes and what your authorization model allows the user (see [RBAC and ABAC]({{ '/backend/rbac-and-abac/' | relative_url }})).
- `resource:action` (`orders:read`, `orders:write`) is a common shape: readable on a consent screen, fine enough for least privilege.
- Don't put tenant or object IDs in scopes; that explodes the scope list. `offline_access` (OIDC) requests a refresh token.

## Token types

| Token | Audience | Lifetime | Purpose |
|---|---|---|---|
| Authorization code | Token endpoint | Seconds to 1 min, single use | Exchanged for tokens |
| Access token | Resource server | Minutes (5 to 60) | Call APIs |
| Refresh token | Authorization server | Hours to months | Get new access tokens |
| ID token (OIDC) | Client | Minutes | Prove who logged in |

**Access token formats.** OAuth does not mandate a format.

| | Opaque (random string) | Self-contained JWT (RFC 9068) |
|---|---|---|
| Validation | RS calls introspection endpoint (RFC 7662) or shared store | RS verifies signature locally via JWKS |
| Revocation | Immediate | Only at expiry, unless you add a denylist |
| Latency | Network hop per request (cache it briefly) | None |
| Leaks claims | No | Payload is readable by anyone holding it |

A common hybrid: opaque tokens at the edge, and the gateway introspects once and forwards a short-lived internal JWT (see [API gateway and BFF]({{ '/backend/api-gateway-and-bff/' | relative_url }})).

**Bearer vs sender-constrained.** A bearer token (RFC 6750) works for whoever holds it. Sender-constrained tokens are bound to a key the client holds: mTLS-bound tokens (RFC 8705) carry the client certificate thumbprint, and DPoP (RFC 9449) has the client sign a per-request proof. A stolen one is useless without the key; financial-grade (FAPI) profiles require this.

**Refresh tokens** for public clients rotate on every use with reuse detection (see [JWT and refresh tokens]({{ '/backend/jwt-and-refresh-tokens/' | relative_url }})). RFC 7009 defines the `/revoke` endpoint.

## Deprecated flows

| Flow | What it did | Why it's gone |
|---|---|---|
| Implicit (`response_type=token`) | Access token in the URL fragment | Exposed to history, `Referer` and page scripts; can't be sender-constrained. Code + PKCE works in browsers now (CORS). |
| Resource owner password credentials | Client collects the password and posts it to `/token` | Client holds the real credential; bypasses MFA and consent |
| Tokens in query strings | `?access_token=...` | Lands in logs, proxies and `Referer` |

## OAuth 2.1 direction

OAuth 2.1 (an IETF draft, still in progress as of 2025–2026) adds no new mechanisms. It folds RFC 6749, RFC 6750, PKCE and the Security Best Current Practice (RFC 9700, 2025) into one document:

- Authorization code requires PKCE for all clients.
- Implicit and password grants are removed.
- Redirect URIs must match exactly (no wildcard or prefix matching), with a port exception for loopback redirects in native apps.
- Bearer tokens may not be sent in query strings.
- Refresh tokens for public clients must be sender-constrained or one-time use (rotation).

If you build to RFC 9700 today, you are effectively compliant with 2.1.

## OpenID Connect

OIDC (OpenID Connect Core 1.0, from the OpenID Foundation) reuses the authorization code flow. Adding the `openid` scope makes the AS act as an OpenID Provider (OP) and return an **ID token** next to the access token. Your app is the Relying Party (RP).

Logging users in with a plain access token is a classic bug: a malicious app that obtained a user's token for itself can replay it to your "login with token" endpoint. The ID token is addressed to your client by its `aud` claim, so it can't be replayed that way.

### ID tokens

An ID token is always a signed JWT (usually RS256 or ES256). Key claims:

| Claim | Meaning |
|---|---|
| `iss` | Issuer URL; must exactly match the OP you expected |
| `sub` | Stable user identifier, unique within `iss` |
| `aud` | Must contain your `client_id` |
| `exp`, `iat` | Expiry and issue time |
| `nonce` | Echo of the value you sent; prevents replay |
| `auth_time`, `acr`, `amr` | When and how the user authenticated (MFA?) |
| `azp` | Authorized party, when `aud` has several values |

Validate: signature with the JWKS key matching `kid`, `alg` from an allowlist (never `none`), `iss`, `aud`, `exp` (small clock skew) and `nonce`. Identify users by `(iss, sub)`, never by `email`, which can be reassigned or unverified. The ID token is for the client only; APIs get the access token.

### Discovery and JWKS

Every OP publishes metadata at `{issuer}/.well-known/openid-configuration`:

```json
{
  "issuer": "https://auth.example.com",
  "authorization_endpoint": "https://auth.example.com/authorize",
  "token_endpoint": "https://auth.example.com/oauth/token",
  "userinfo_endpoint": "https://auth.example.com/userinfo",
  "jwks_uri": "https://auth.example.com/.well-known/jwks.json",
  "scopes_supported": ["openid", "profile", "email", "offline_access"],
  "response_types_supported": ["code"],
  "code_challenge_methods_supported": ["S256"],
  "id_token_signing_alg_values_supported": ["RS256", "ES256"]
}
```

Clients configure only the issuer URL and discover the rest. The `jwks_uri` serves public signing keys. Cache the JWKS (hours), and when a token arrives with an unknown `kid`, refetch once (rate-limited) to pick up key rotation. RFC 8414 defines the same idea for plain OAuth servers.

### UserInfo endpoint

`GET /userinfo` with the access token returns profile claims (`name`, `email`, `email_verified`, `picture`) for the granted scopes, letting providers keep ID tokens small. Its `sub` must match the ID token's `sub`; reject it otherwise.

### Single sign-on

SSO falls out of the OP keeping its own session cookie. After logging into app A via the OP, app B's redirect finds that session and comes straight back with a code, no password prompt. Each app still creates its own local session.

- `prompt=none` asks for silent authentication; the OP returns `login_required` if there is no session.
- `max_age` or `prompt=login` forces re-authentication for sensitive actions; check `auth_time` in the ID token.
- Logout is the hard part. RP-Initiated Logout sends the user to the OP's `end_session_endpoint`; Back-Channel Logout has the OP POST a signed logout token to each RP to kill server-side sessions. Front-channel (iframe) logout breaks under third-party cookie blocking.
- B2B customers often bring their own IdP: configure OIDC or SAML per tenant and route by email domain (see [Multi-tenant architecture]({{ '/backend/multi-tenant-architecture/' | relative_url }})).

### State and nonce

Three random values, three different jobs:

| Value | Bound to | Prevents |
|---|---|---|
| `state` | Browser session ↔ callback | CSRF on the callback: an attacker logging the victim into the attacker's account (login CSRF) |
| `nonce` | Auth request ↔ ID token | ID token replay or injection |
| PKCE verifier | Client instance ↔ code | Code interception and code injection |

All three must be unguessable, single-use and stored server-side (or in an encrypted, HttpOnly cookie). If the client talks to multiple authorization servers, also check the `iss` parameter in the authorization response (RFC 9207) to stop mix-up attacks.

## In practice

### End-to-end flow, runnable

A stdlib-only simulation of the code + PKCE + OIDC flow: a toy authorization server, a client that creates `state`, `nonce` and the verifier, and ID token validation. Real providers sign ID tokens with RS256 or ES256 and publish keys via JWKS; HS256 is used here only because the standard library has no RSA.

```python
import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlparse


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def s256(verifier: str) -> str:
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())


# ---- toy authorization server --------------------------------------
ISSUER = "https://auth.example.com"
SIGNING_KEY = secrets.token_bytes(32)
CLIENTS = {"web-app": ["https://app.example.com/callback"]}
codes = {}


def sign_jwt(claims: dict) -> str:
    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64url(json.dumps(claims).encode())
    sig = hmac.new(SIGNING_KEY, f"{header}.{payload}".encode(), hashlib.sha256)
    return f"{header}.{payload}.{b64url(sig.digest())}"


def authorize(client_id, redirect_uri, scope, state, nonce, challenge, method):
    if redirect_uri not in CLIENTS.get(client_id, []):  # exact match only
        raise ValueError("invalid redirect_uri")
    if method != "S256":
        raise ValueError("PKCE S256 required")
    # ... user authenticates and consents here ...
    code = secrets.token_urlsafe(32)
    codes[code] = dict(client_id=client_id, redirect_uri=redirect_uri,
                       scope=scope, nonce=nonce, challenge=challenge,
                       sub="user-42", exp=time.time() + 60)
    return redirect_uri + "?" + urlencode({"code": code, "state": state})


def token(code, client_id, redirect_uri, verifier):
    grant = codes.pop(code, None)  # single use, even if checks below fail
    if grant is None or grant["exp"] < time.time():
        raise PermissionError("invalid_grant")
    if (grant["client_id"], grant["redirect_uri"]) != (client_id, redirect_uri):
        raise PermissionError("invalid_grant")
    if not hmac.compare_digest(s256(verifier), grant["challenge"]):
        raise PermissionError("invalid_grant: PKCE verifier mismatch")
    now = int(time.time())
    id_token = sign_jwt({"iss": ISSUER, "sub": grant["sub"], "aud": client_id,
                         "iat": now, "exp": now + 300, "nonce": grant["nonce"]})
    return {"access_token": secrets.token_urlsafe(32), "token_type": "Bearer",
            "expires_in": 600, "scope": grant["scope"], "id_token": id_token}


# ---- client side ------------------------------------------------------
def validate_id_token(id_token, client_id, expected_nonce, skew=60):
    h_b64, p_b64, s_b64 = id_token.split(".")
    if json.loads(b64url_decode(h_b64)).get("alg") != "HS256":  # allowlist
        raise ValueError("unexpected alg")
    good = hmac.new(SIGNING_KEY, f"{h_b64}.{p_b64}".encode(), hashlib.sha256)
    if not hmac.compare_digest(good.digest(), b64url_decode(s_b64)):
        raise ValueError("bad signature")
    claims = json.loads(b64url_decode(p_b64))
    aud = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    if claims["iss"] != ISSUER or client_id not in aud:
        raise ValueError("wrong iss or aud")
    if claims["exp"] < time.time() - skew:
        raise ValueError("expired")
    if not hmac.compare_digest(claims["nonce"], expected_nonce):
        raise ValueError("nonce mismatch")
    return claims


def start_login(session):
    session["verifier"] = b64url(secrets.token_bytes(32))  # 43 chars
    session["state"] = secrets.token_urlsafe(16)
    session["nonce"] = secrets.token_urlsafe(16)
    return authorize("web-app", "https://app.example.com/callback",
                     "openid email", session["state"], session["nonce"],
                     s256(session["verifier"]), "S256")


if __name__ == "__main__":
    # RFC 7636 appendix B test vector
    assert s256("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == \
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

    session = {}
    callback = parse_qs(urlparse(start_login(session)).query)
    assert hmac.compare_digest(callback["state"][0], session["state"])
    tokens = token(callback["code"][0], "web-app",
                   "https://app.example.com/callback", session["verifier"])
    claims = validate_id_token(tokens["id_token"], "web-app", session["nonce"])
    assert (claims["iss"], claims["sub"]) == (ISSUER, "user-42")

    # code replay fails: codes are single use
    try:
        token(callback["code"][0], "web-app",
              "https://app.example.com/callback", session["verifier"])
        raise AssertionError("replay should fail")
    except PermissionError:
        pass

    # an attacker who intercepts a code but not the verifier gets nothing
    victim = {}
    stolen = parse_qs(urlparse(start_login(victim)).query)["code"][0]
    try:
        token(stolen, "web-app", "https://app.example.com/callback",
              b64url(secrets.token_bytes(32)))
        raise AssertionError("PKCE should block this")
    except PermissionError:
        pass

    # an ID token from another login (different nonce) is rejected
    try:
        validate_id_token(tokens["id_token"], "web-app", victim["nonce"])
        raise AssertionError("nonce check should fail")
    except ValueError:
        pass

    print("ok")
```

### In a Django app

Use a maintained library (Authlib, `mozilla-django-oidc`, `django-allauth`) rather than hand-rolling JWKS handling. The callback should compare and delete `state`, exchange the code with the stored verifier, validate the ID token, look the user up by `(iss, sub)`, and call Django's `login()`, which rotates the session key against session fixation.

### SPAs: keep tokens out of the browser

A pure SPA can run code + PKCE itself, but then any XSS can read its tokens. For sensitive apps, make the backend (a BFF) the OAuth client: it holds tokens server-side, gives the browser only an HttpOnly `SameSite` session cookie, and attaches the access token when proxying API calls.

## Pitfalls

- **Matching accounts by email.** A provider that allows unverified emails, or an email reassigned after an employee leaves, becomes an account takeover. Key identities on `(iss, sub)`; link by email only if `email_verified` is true and the user proves ownership of the existing account.
- **Loose redirect URI matching.** Prefix or wildcard matching plus an open redirect on your domain lets attackers receive codes. Register exact URIs.
- **Missing or static `state`.** Symptom: users occasionally find themselves logged into someone else's account (login CSRF). Generate per request, store server-side, compare, delete.
- **Accepting any `alg`.** Libraries that honor `alg: none` or let an attacker switch RS256 to HS256 using the public key as the HMAC secret. Pin the allowed algorithms.
- **Not handling key rotation.** Hard-coding the IdP's public key works until the IdP rotates it, and then every login fails. Use JWKS with `kid` lookup and refresh on miss.
- **Tokens in logs and URLs.** Codes and tokens leak through access logs, Sentry breadcrumbs and `Referer`. Scrub `code`, `access_token`, `id_token`, `refresh_token` from logs; set `Referrer-Policy`.

## Interview questions

**What is the difference between OAuth 2.0 and OpenID Connect?**
OAuth is delegated authorization: it issues access tokens a client uses to call an API. It doesn't define how to learn who the user is. OIDC adds authentication on top: the `openid` scope, a signed ID token with `iss`/`sub`/`aud`/`nonce`, a UserInfo endpoint, discovery and logout specs.

**Why does PKCE exist, and why use it for confidential clients too?**
It binds the authorization code to the client instance that started the flow, so a stolen code can't be redeemed without the verifier. Public clients can't authenticate to the token endpoint, so PKCE is their only protection. For confidential clients it additionally prevents code injection into a victim's session, so RFC 9700 and OAuth 2.1 require it everywhere.

**Why was the implicit flow deprecated?**
It put access tokens in the URL fragment, exposing them to history, extensions and any script, with no way to bind them to the client and no refresh. Browsers now support CORS on token endpoints, so code + PKCE works in SPAs, and a BFF is better still.

**How do `state`, `nonce` and PKCE differ?**
`state` ties the callback to the browser session (CSRF). `nonce` ties the ID token to the auth request (replay). PKCE ties the code to the client instance (interception). They overlap partially; use all three.

**How should one microservice authenticate to another?**
Client credentials grant with a short-lived token and narrow scopes, the client authenticating with `private_key_jwt` or mTLS rather than a static secret, or platform workload identity. Inside a mesh, mTLS with SPIFFE identities often handles the transport identity, and tokens carry the end-user context when needed (see [Service discovery and mesh]({{ '/backend/service-discovery-and-mesh/' | relative_url }})).

**How does SSO work with OIDC, and what makes logout hard?**
The OP keeps a session cookie, so the second app's redirect returns immediately with a code. Each app has its own local session, so logout must reach all of them: RP-initiated logout ends the OP session, and back-channel logout notifies each RP server-to-server to delete its sessions.

## Further reading

- RFC 6749, *The OAuth 2.0 Authorization Framework*, and RFC 7636, *Proof Key for Code Exchange by OAuth Public Clients*
- RFC 9700, *Best Current Practice for OAuth 2.0 Security*
- RFC 8628, *OAuth 2.0 Device Authorization Grant*
- *OpenID Connect Core 1.0* and *OpenID Connect Discovery 1.0* (OpenID Foundation)
- *The OAuth 2.1 Authorization Framework* (IETF draft, draft-ietf-oauth-v2-1)
