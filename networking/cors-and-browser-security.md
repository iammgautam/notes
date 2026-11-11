---
title: CORS, CSRF and browser security
section: networking
order: 10
summary: The same-origin policy, how CORS preflights work, why CSRF exists and how SameSite cookies and tokens stop it, and what CSP adds against XSS.
---

Browsers run code from many sites in one process and automatically attach cookies to requests. The same-origin policy, CORS, CSRF defenses and CSP are the rules that keep site A from abusing your session on site B. As a backend engineer serving a Vue SPA and an API, you configure most of these on the server, and misconfiguring them either breaks the frontend or opens a hole.

## How it works

### Same-origin policy

An **origin** is scheme + host + port. `https://app.example.com` and `https://api.example.com` are different origins; so are `http://` vs `https://` and `:8080` vs `:443`.

The browser lets a page **send** requests anywhere (forms, images, `fetch`), but blocks page JavaScript from **reading** cross-origin responses unless the server opts in via CORS. That asymmetry is the root of CSRF: the request still goes out, with cookies.

A **site** is coarser: scheme + registrable domain (`example.com`). `app.example.com` and `api.example.com` are same-site, cross-origin. SameSite cookie rules use "site"; CORS uses "origin".

### CORS

CORS is the server telling the browser "this other origin may read my responses". It relaxes the same-origin policy; it does not protect your server. curl, Postman and attackers' servers ignore it.

**Simple requests** (GET/HEAD/POST with only "simple" headers and `Content-Type` of `text/plain`, `multipart/form-data` or `application/x-www-form-urlencoded`) are sent directly; the browser checks the response headers before handing it to JS.

Everything else (PUT, PATCH, DELETE, `Content-Type: application/json`, an `Authorization` header) triggers a **preflight**:

```http
OPTIONS /v1/projects/42 HTTP/1.1
Origin: https://app.example.com
Access-Control-Request-Method: PATCH
Access-Control-Request-Headers: authorization, content-type

HTTP/1.1 204 No Content
Access-Control-Allow-Origin: https://app.example.com
Access-Control-Allow-Methods: GET, POST, PATCH, DELETE
Access-Control-Allow-Headers: authorization, content-type
Access-Control-Allow-Credentials: true
Access-Control-Max-Age: 600
Vary: Origin
```

- `Access-Control-Allow-Origin: *` cannot be combined with credentials (cookies). For cookies you must echo a specific origin and send `Allow-Credentials: true`, and the frontend must use `credentials: "include"`.
- `Access-Control-Max-Age` caches the preflight; without it every PATCH costs two round trips.
- `Access-Control-Expose-Headers` lists response headers JS may read (e.g. `Location`, `X-Request-Id`).

### CSRF

Cross-site request forgery: `evil.com` contains a form that auto-submits `POST https://app.example.com/v1/transfer`. The browser attaches your session cookie. If the server only checks the cookie, the action happens. The attacker never sees the response; they don't need to.

Defenses, layered:

1. **SameSite cookies.**
   - `Strict`: cookie never sent on cross-site requests, even top-level navigation from a link. Safe but annoying (users arriving from email look logged out).
   - `Lax` (browser default): sent on top-level GET navigations, not on cross-site POSTs, iframes or `fetch`. Blocks classic CSRF if your GETs have no side effects.
   - `None; Secure`: always sent. Needed for genuinely cross-site embeds; requires another CSRF defense.
2. **CSRF tokens.** Server issues a secret; state-changing requests must echo it in a header or form field. An attacker's page can't read it.
3. **Origin / Referer checks** on unsafe methods.
4. **Not using cookies for auth.** `Authorization: Bearer` headers are never attached automatically, so token-auth APIs are not CSRF-prone (but tokens readable by JS are exposed to XSS).

### XSS and CSP

Cross-site scripting runs attacker JS inside your origin, which defeats all of the above: it can read the DOM, call your API with the user's cookies, and steal tokens from `localStorage`. Primary defense is output encoding (Django templates and Vue's mustache interpolation escape by default; `v-html` and `|safe` do not). Content Security Policy is the second layer:

```http
Content-Security-Policy: default-src 'self'; script-src 'self' 'nonce-r4nd0m';
  connect-src 'self' https://api.example.com; img-src 'self' data: https:;
  frame-ancestors 'none'; base-uri 'self'; object-src 'none'
```

- No `'unsafe-inline'` scripts: injected `<script>` tags and `onclick=` attributes don't run.
- `frame-ancestors 'none'` stops clickjacking (successor of `X-Frame-Options: DENY`).
- `connect-src` limits where `fetch` can exfiltrate data.
- Roll out with `Content-Security-Policy-Report-Only` first and collect reports (Sentry accepts CSP reports).

## In practice

### Django + DRF + Vue on different subdomains

```python
# settings.py
INSTALLED_APPS += ["corsheaders"]
MIDDLEWARE.insert(0, "corsheaders.middleware.CorsMiddleware")

CORS_ALLOWED_ORIGINS = ["https://app.example.com"]
CORS_ALLOW_CREDENTIALS = True
CORS_PREFLIGHT_MAX_AGE = 600

SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_SECURE = True
CSRF_TRUSTED_ORIGINS = ["https://app.example.com"]
```

DRF's `SessionAuthentication` enforces CSRF for authenticated unsafe requests. The Vue app reads the `csrftoken` cookie (so it must not be `HttpOnly`) and sends it back:

```javascript
import axios from "axios";

export const api = axios.create({
  baseURL: "https://api.example.com/v1",
  withCredentials: true,
  xsrfCookieName: "csrftoken",
  xsrfHeaderName: "X-CSRFToken",
  withXSRFToken: true,   // axios >= 1.6 only sends it same-origin otherwise
});
```

Simplest setup of all: serve the SPA and API from the same origin (`app.example.com/api/` via a reverse proxy). No CORS, no `SameSite=None`, fewer moving parts.

### Security headers worth setting

```python
SECURE_CONTENT_TYPE_NOSNIFF = True                # X-Content-Type-Options: nosniff
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
```

Django 6.0+ (built-in CSP middleware) or `django-csp` on older versions handle CSP headers and nonces.

### Debugging CORS

```bash
curl -i -X OPTIONS https://api.example.com/v1/projects \
  -H 'Origin: https://app.example.com' \
  -H 'Access-Control-Request-Method: PATCH' \
  -H 'Access-Control-Request-Headers: content-type'
```

If the preflight returns 401 or 302, your auth middleware or `APPEND_SLASH` redirect runs before CORS handling. Preflights carry no credentials; they must succeed unauthenticated.

## Pitfalls

- **Reflecting any `Origin`** with `Allow-Credentials: true`. Any site can now read your users' data. Use an explicit allowlist; be careful with regexes like `.*example\.com` that match `evilexample.com`.
- **Thinking CORS is auth.** It only governs browsers reading responses. Server-side authz is still required.
- **Missing `Vary: Origin`** when the allowed origin varies, so a CDN serves the wrong CORS header.
- **State-changing GETs** bypass `SameSite=Lax`.
- **`SameSite=None` without CSRF tokens** for convenience.
- **`csrf_exempt` on a cookie-authenticated endpoint** because the frontend got a 403.
- **CORS errors hiding real errors.** A 500 without CORS headers shows in the browser as a CORS failure. Check the network tab and server logs.
- **Tokens in `localStorage`** plus any XSS equals account takeover.

## Checklist

- Same origin for SPA and API if you can; otherwise an explicit origin allowlist.
- Cookies: `Secure`, `HttpOnly` for session, `SameSite=Lax` by default.
- CSRF tokens on cookie-authenticated unsafe requests; no side effects on GET.
- Preflights answered before auth, cached with `Max-Age`.
- CSP with nonces, `frame-ancestors 'none'`, `nosniff`, sane referrer policy.
- Never render user HTML without sanitizing (`bleach`/`nh3` server-side, DOMPurify client-side).

## Interview angle

- **"What is CORS and what does it protect?"** A browser mechanism that lets a server allow specific other origins to read its responses. It protects users' data from being read by other sites' scripts, not your server from requests.
- **"Why is there a preflight?"** So that requests a plain HTML form could never send (custom headers, JSON, PUT/DELETE) don't hit legacy servers that assumed such requests were impossible cross-origin, unless the server opts in.
- **"How does CSRF work and how do you prevent it?"** The browser auto-attaches cookies to cross-site requests. Prevent with SameSite cookies, CSRF tokens, Origin checks, and no side effects on GET.
- **"If you use JWTs in headers, do you need CSRF protection?"** Not for those requests, since the browser doesn't attach headers automatically. But the token is now readable by JS, so XSS defenses and CSP matter more.
