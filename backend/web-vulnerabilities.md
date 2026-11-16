---
title: SQL injection, XSS and CSRF
section: backend
order: 55
phase: "Phase 7 · Security"
summary: How SQL injection, cross-site scripting and CSRF actually work, the ORM and template traps that reintroduce them, and the defenses Django gives you.
---

These three are old, well understood and still in every breach report, because each one comes from a single habit: mixing untrusted data into a language that a machine will interpret. SQL injection mixes it into SQL, XSS into HTML or JavaScript, and CSRF abuses the browser attaching your cookies to someone else's request. Frameworks like Django block the default paths; the bugs live in the escape hatches (`raw()`, `|safe`, `@csrf_exempt`) and in assumptions about what "internal" data is. Authorization flaws such as accessing another tenant's object by ID are covered in [RBAC and ABAC]({{ '/backend/rbac-and-abac/' | relative_url }}).

## SQL injection

### How it happens

```python
# Illustrative: the bug
sql = f"SELECT * FROM users WHERE tenant_id = {tenant_id} AND username = '{name}'"
```

With `name = "x' OR '1'='1"` the query becomes `... AND username = 'x' OR '1'='1'`. Because `AND` binds tighter than `OR`, the condition is true for every row in every tenant. Variants: `UNION SELECT` to read other tables, stacked queries (`; DROP TABLE`) where the driver allows them, and **blind** injection, where the attacker infers data one bit at a time from response differences or `pg_sleep()` delays.

### Parameterization

Send the query text and the values separately. The database (or driver) treats values as data, never as SQL, whatever characters they contain.

```python
# Illustrative: psycopg / Django cursor style
cursor.execute(
    "SELECT * FROM users WHERE tenant_id = %s AND username = %s",
    [tenant_id, name],
)
```

- psycopg 2 interpolates parameters on the client with correct quoting; psycopg 3 sends them to the server separately by default. Both are safe; what matters is that you never build the string yourself. Don't wrap placeholders in quotes (`'%s'`).
- **Identifiers and keywords can't be parameters**: table names, column names, `ASC`/`DESC`, `ORDER BY` targets. Map user input through an allowlist, or compose with `psycopg.sql.Identifier`.
- **Lists:** `WHERE id = ANY(%s)` with a Python list, not a hand-joined `IN (...)`.
- **`LIKE` patterns:** parameterize, and escape `%` and `_` in user input if they should match literally.

### ORM pitfalls

The Django ORM parameterizes everything it generates. Injection comes back through the APIs that accept SQL fragments, and through request data that controls query structure:

```python
# Illustrative Django: unsafe
User.objects.raw(f"SELECT * FROM auth_user WHERE username = '{name}'")
Invoice.objects.extra(where=[f"status = '{status}'"])
Invoice.objects.annotate(v=RawSQL(f"payload->>'{key}'", []))
cursor.execute(f"UPDATE invoices SET status = '{status}' WHERE id = {pk}")

# Safe: same APIs with parameters
User.objects.raw("SELECT * FROM auth_user WHERE username = %s", [name])
Invoice.objects.annotate(v=RawSQL("payload->>%s", [key]))
cursor.execute("UPDATE invoices SET status = %s WHERE id = %s", [status, pk])

# Unsafe: request data controls lookups or ordering
Invoice.objects.filter(**request.GET.dict())  # ?customer__user__password__startswith=p
Invoice.objects.order_by(request.GET["sort"])  # can sort by any field, incl. relations

# Safe: allowlist
SORTS = {"created_at", "-created_at", "amount", "-amount"}
sort = request.GET.get("sort", "-created_at")
Invoice.objects.order_by(sort if sort in SORTS else "-created_at")
```

`filter(**request.GET.dict())` isn't classic SQL injection, but it lets an attacker filter across relations on fields they can't see, such as a password hash, and extract them one character at a time from whether results come back. Django has also shipped security releases where user-controlled column aliases or kwargs keys passed into `annotate()`, `aggregate()` or `extra()` reached SQL, so never splat request data into ORM keyword arguments.

### Second-order injection

Data is stored safely with parameters, then later read back and concatenated into SQL by a different code path that "trusts the database": a nightly report, an admin tool, a migration script, a stored procedure building dynamic SQL. A username like `carol' --` sits harmlessly in the table until a report job interpolates it and comments out the tenant filter. The runnable demo below shows exactly this. The rule: every value is untrusted at the point it enters a query, regardless of where it came from.

### Defense in depth

- The application's database role has only the privileges it needs: no superuser, no DDL, no `COPY ... PROGRAM`, ideally no access to other schemas.
- Row-level security for tenant isolation limits what an injection can read (see [Multi-tenancy patterns]({{ '/postgresql/multi-tenancy/' | relative_url }})).
- Generic error responses; database errors in API responses are a gift to attackers.
- `statement_timeout` limits time-based blind injection and runaway queries.
- A WAF catches noisy scanners, never subtle injection. It is not a fix.

## Cross-site scripting

XSS runs the attacker's JavaScript in your origin, in the victim's browser. That script can read the page, call your API with the user's cookies, change their email, and read any token stored in `localStorage`.

### Stored, reflected and DOM-based

| Type | Where the payload lives | Example |
|---|---|---|
| Stored | Your database, rendered to other users | A comment body `<img src=x onerror=...>` shown to every viewer |
| Reflected | The request, echoed in the response | `/search?q=<script>...</script>` rendered as "Results for ..." |
| DOM-based | Client-side JS writes untrusted data to a dangerous sink | `el.innerHTML = location.hash.slice(1)` |

### Output escaping by context

Escape at output time, for the context the data lands in. Validation on input helps but can't know every future output context.

| Context | Escape or rule |
|---|---|
| HTML body | `&`, `<`, `>`, `"`, `'` to entities (Django autoescape does this) |
| Quoted HTML attribute | Same, and always quote attributes |
| URL in `href`/`src` | Allowlist schemes (`https`, `mailto`); escaping doesn't stop `javascript:` |
| Inside `<script>` | Don't interpolate; pass data with `json_script` or a `data-` attribute |
| CSS, event handlers | Don't put user data there |

Django templates autoescape every variable. The traps are the opt-outs:

{% raw %}
```text
<p>{{ comment.body }}</p>                   escaped: safe
<p>{{ comment.body|safe }}</p>              XSS if body is user input
<a href="{{ profile.url }}">site</a>        escaped, but javascript: runs
{{ chart_data|json_script:"chart-data" }}   safe way to hand data to JS
{% autoescape off %}...{% endautoescape %}  avoid
```
{% endraw %}

In Python, `mark_safe(f"<b>{name}</b>")` is an XSS bug; `format_html("<b>{}</b>", name)` escapes the arguments. On the frontend, React and Vue escape by default, and `dangerouslySetInnerHTML` / `v-html` are the opt-outs. When you must accept rich text (Markdown, a WYSIWYG editor), sanitize with an allowlist-based HTML sanitizer (`nh3` in Python, since `bleach` is deprecated; DOMPurify in the browser) and still escape everything else.

### Content Security Policy

CSP is a response header that tells the browser which scripts may run. A strict, nonce-based policy turns most injected markup into inert text:

```http
Content-Security-Policy: script-src 'nonce-R4nd0mPerResponse' 'strict-dynamic';
  object-src 'none'; base-uri 'none'; frame-ancestors 'none'
```

- Each response generates a fresh random nonce and puts it on every legitimate `<script nonce="...">`. Injected scripts don't have it and are blocked; inline event handlers (`onerror=`) are blocked too.
- `'strict-dynamic'` lets nonce-approved scripts load their own dependencies, which makes nonce policies workable with bundlers.
- Host allowlists (`script-src cdn.example.com`) are weaker than they look: any JSONP endpoint or old library on an allowed host becomes a bypass.
- `'unsafe-inline'` in `script-src` defeats the purpose.
- Roll out with `Content-Security-Policy-Report-Only` first, collect violation reports, fix, then enforce.
- Trusted Types (`require-trusted-types-for 'script'`) closes DOM XSS sinks in browsers that support it.

Django 6.0 added built-in CSP middleware; older versions commonly use the `django-csp` package.

**Limit the blast radius** as well: session cookies `HttpOnly` so script can't read them, no long-lived tokens in `localStorage`, and re-authentication for sensitive actions such as changing email or adding an API key.

## CSRF protection

Cross-site request forgery works because browsers attach cookies to requests based on the destination, not on which site started the request.

```text
 victim browser               evil.example                app.example
      | visit page ---------------->|                            |
      |<-- auto-submitting form ----|                            |
      | POST /settings/email  (browser attaches session cookie)->|
      |                                     session valid, no    |
      |                                     token or origin check|
      |<----------------------------------- 302 email changed ---|
```

The attacker can't read the response, but they don't need to: the state change is the attack. Anything that authenticates with **ambient credentials** (cookies, HTTP Basic, client certificates) is exposed. APIs that require a bearer token in the `Authorization` header are not, because a cross-site form can't set that header. Put a JWT in a cookie and you are back to needing CSRF protection.

**Login CSRF** is the less obvious variant: the attacker logs the victim into the *attacker's* account, and the victim then saves card details or search history there. Protect the login form too.

### Synchronizer tokens

The server generates a random token tied to the session, embeds it in each form or page, and requires it on every state-changing request. A cross-site attacker can't read your pages (same-origin policy), so they can't learn the token. This is the classic, strongest pattern, and it's what Django implements.

### Double-submit cookie

For stateless backends: set the token in a cookie and require the same value in a header or form field. The attacker can't read the cookie to copy it into the header. The naive version is weak: if an attacker can write cookies for your domain (a compromised subdomain, an HTTP subdomain without HSTS), they can plant a matching pair. **Signed double-submit** fixes this by making the token an HMAC over the session ID, so a planted token doesn't validate for the victim's session. The demo implements that.

### SameSite cookies

| Value | Cookie sent on cross-site requests | Notes |
|---|---|---|
| `Strict` | Never | Users arriving via an external link look logged out |
| `Lax` | Only top-level `GET` navigations | Chrome's default when unset (since 2020); Django's default |
| `None` | Always (requires `Secure`) | For embedded or third-party use |

`Lax` blocks the cross-site `POST` above, which is why CSRF is much rarer now. It is not a complete defense:

- State changes on `GET` (`/unsubscribe?id=`, `/logout`) still go through.
- "Site" means registrable domain: `evil.app.example` (user content, a forgotten staging host) is same-site with `app.example`, so SameSite doesn't apply.
- Older clients and some embedded browsers behave differently.

Use SameSite as a layer, keep tokens, and never mutate state on `GET`.

### Origin and Fetch Metadata checks

Browsers send `Origin` on cross-origin `POST` requests and `Sec-Fetch-Site` (`same-origin`, `same-site`, `cross-site`, `none`) on requests in modern browsers. Rejecting state-changing requests whose `Origin` isn't yours, or whose `Sec-Fetch-Site` is `cross-site`, is a cheap, robust second check. CORS is not CSRF protection: it controls who can *read* responses, and simple form posts never trigger a preflight.

### Django CSRF

`CsrfViewMiddleware` combines several of these:

- A `csrftoken` cookie holds the secret; forms include it via the template tag, and JavaScript sends it in the `X-CSRFToken` header.
- The token in pages is **masked** with a random value per render, so compression attacks like BREACH can't recover it.
- For unsafe methods it checks the `Origin` header against the host and `CSRF_TRUSTED_ORIGINS` (which needs the scheme, `https://app.example.com`, since Django 4.0), and on HTTPS it checks `Referer` when `Origin` is missing.
- DRF's `SessionAuthentication` enforces CSRF for session-authenticated requests; token or JWT authentication in headers doesn't need it.

{% raw %}
```text
<form method="post" action="/settings/email">
  {% csrf_token %}
  <input name="email" type="email">
</form>
```
{% endraw %}

`@csrf_exempt` belongs only on endpoints that authenticate some other way, such as payment webhooks that verify an HMAC signature from the provider.

## In practice

### SQL injection, escaping and CSRF tokens, runnable

Uses `sqlite3` from the standard library to show first- and second-order injection, an identifier allowlist, context-aware escaping, and signed double-submit CSRF tokens with an Origin check.

```python
import hashlib
import hmac
import html
import json
import secrets
import sqlite3
from urllib.parse import urlsplit

db = sqlite3.connect(":memory:")
db.executescript("""
CREATE TABLE users (id INTEGER PRIMARY KEY, tenant_id INT,
                    username TEXT, email TEXT);
INSERT INTO users (tenant_id, username, email) VALUES
  (1, 'alice', 'alice@acme.test'), (1, 'bob', 'bob@acme.test'),
  (2, 'carol', 'carol@globex.test');
""")


def find_unsafe(tenant_id, name):
    sql = ("SELECT username FROM users WHERE tenant_id = "
           f"{tenant_id} AND username = '{name}'")
    return [r[0] for r in db.execute(sql)]


def find_safe(tenant_id, name):
    sql = "SELECT username FROM users WHERE tenant_id = ? AND username = ?"
    return [r[0] for r in db.execute(sql, (tenant_id, name))]


SORTABLE = {"id", "username", "email"}


def list_users(tenant_id, sort):
    col = sort.lstrip("-")
    if col not in SORTABLE:  # identifiers can't be bound: allowlist them
        raise ValueError("invalid sort")
    order = "DESC" if sort.startswith("-") else "ASC"
    sql = f"SELECT username FROM users WHERE tenant_id = ? ORDER BY {col} {order}"
    return [r[0] for r in db.execute(sql, (tenant_id,))]


def register(tenant_id, username):  # first order: stored safely
    db.execute("INSERT INTO users (tenant_id, username, email) "
               "VALUES (?, ?, '')", (tenant_id, username))


def report_unsafe(username):  # second order: trusts stored data
    sql = f"SELECT email FROM users WHERE username = '{username}' AND tenant_id = 1"
    return [r[0] for r in db.execute(sql)]


def render_comment(author, body, website):
    scheme = urlsplit(website.strip()).scheme.lower()
    href = website if scheme in ("", "http", "https") else "#"
    return (f'<p title="{html.escape(author)}">{html.escape(body)} '
            f'<a href="{html.escape(href)}">site</a></p>')


def json_for_script(data):  # what Django's json_script does
    return (json.dumps(data).replace("<", "\\u003c")
            .replace(">", "\\u003e").replace("&", "\\u0026"))


CSRF_KEY = secrets.token_bytes(32)


def issue_csrf(session_id):
    nonce = secrets.token_urlsafe(16)
    mac = hmac.new(CSRF_KEY, f"{session_id}!{nonce}".encode(), hashlib.sha256)
    return f"{nonce}.{mac.hexdigest()}"  # set as cookie and embed in page


def check_csrf(session_id, cookie, header, origin, allowed):
    if origin is not None and origin not in allowed:
        return False
    if not cookie or not hmac.compare_digest(cookie.encode(),
                                             (header or "").encode()):
        return False
    nonce, _, mac = cookie.partition(".")
    good = hmac.new(CSRF_KEY, f"{session_id}!{nonce}".encode(), hashlib.sha256)
    return hmac.compare_digest(mac.encode(), good.hexdigest().encode())


if __name__ == "__main__":
    attack = "x' OR '1'='1"
    assert set(find_unsafe(1, attack)) == {"alice", "bob", "carol"}  # all tenants
    assert find_safe(1, attack) == []
    assert find_safe(1, "alice") == ["alice"]

    assert list_users(1, "-username") == ["bob", "alice"]
    try:
        list_users(1, "(SELECT email FROM users LIMIT 1)")
        raise AssertionError("allowlist should reject")
    except ValueError:
        pass

    register(1, "carol' --")                     # harmless when stored
    leaked = report_unsafe("carol' --")          # tenant filter commented out
    assert "carol@globex.test" in leaked

    out = render_comment('"><script>', "<img src=x onerror=alert(1)>",
                         "javascript:alert(1)")
    assert "<script>" not in out and "<img" not in out
    assert 'href="#"' in out
    assert "</script>" not in json_for_script({"bio": "</script><script>"})

    allowed = {"https://app.example.com"}
    tok = issue_csrf("sess-victim")
    assert check_csrf("sess-victim", tok, tok, "https://app.example.com", allowed)
    # cross-site form: cookie is sent, but the attacker can't read it
    assert not check_csrf("sess-victim", tok, None, None, allowed)
    # attacker plants their own valid token pair via a sibling subdomain
    planted = issue_csrf("sess-attacker")
    assert not check_csrf("sess-victim", planted, planted, None, allowed)
    assert not check_csrf("sess-victim", tok, tok, "https://evil.example", allowed)
    print("ok")
```

### Composing identifiers safely with psycopg

Illustrative, psycopg 3:

```python
from psycopg import sql

columns = [c for c in requested_columns if c in EXPORTABLE]  # allowlist first
query = sql.SQL("SELECT {} FROM invoices WHERE tenant_id = %s ORDER BY {}").format(
    sql.SQL(", ").join(sql.Identifier(c) for c in columns),
    sql.Identifier(sort_column),
)
cur.execute(query, [tenant_id])
```

### Django settings that matter

Illustrative:

```python
SESSION_COOKIE_HTTPONLY = True        # default
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_SAMESITE = "Lax"       # default
CSRF_COOKIE_SECURE = True
CSRF_TRUSTED_ORIGINS = ["https://app.example.com"]  # scheme required
SECURE_CONTENT_TYPE_NOSNIFF = True    # default; stops MIME sniffing
X_FRAME_OPTIONS = "DENY"              # clickjacking; or CSP frame-ancestors
```

## Pitfalls

- **"It's an internal value, no need to parameterize."** Tenant slugs, stored usernames, values from another service and admin inputs are all untrusted when they enter a query. That is second-order injection.
- **Quoted placeholders.** `cursor.execute("... WHERE name = '%s'", [name])` produces broken or exploitable SQL depending on the driver. Placeholders are never quoted.
- **`|safe` on "trusted" fields.** A field that was admin-only becomes user-editable a year later, and the `|safe` stays. Sanitize at write time and escape at output.
- **Escaping for the wrong context.** HTML-escaped data inside a `<script>` block or an `href` is still exploitable. Use `json_script` and scheme allowlists.
- **CSP with `'unsafe-inline'`.** It looks like protection in an audit and blocks almost nothing. Symptom: the policy exists and XSS still pops.
- **State changes on `GET`.** SameSite `Lax` sends cookies on top-level `GET` navigations, so `GET /account/delete` is CSRF-able. Use `POST` (or `DELETE`) for every mutation.
- **`@csrf_exempt` to "fix" a failing AJAX call.** The real fix is sending `X-CSRFToken`. Exempt only endpoints with their own authentication, like signed webhooks.
- **Thinking CORS stops CSRF.** A permissive CORS config is its own vulnerability, and a strict one doesn't stop simple cross-site form posts.

## Interview questions

**How does parameterization prevent SQL injection, and what can't be parameterized?**
Values are sent separately from the query text, so the parser never interprets them as SQL. Identifiers (table and column names), sort directions and SQL keywords can't be parameters; validate them against an allowlist or compose with an identifier-quoting API.

**Is using an ORM enough to prevent SQL injection?**
For queries it builds, yes. Raw SQL APIs (`raw()`, `extra()`, `RawSQL`, `cursor.execute`) are only safe with parameters, and request data must never control field names, lookups or ordering without an allowlist.

**What is second-order SQL injection?**
Malicious input is stored safely, then later read back and concatenated into SQL by another code path that trusts database contents. The fix is the same: parameterize every query regardless of where values come from.

**Compare stored, reflected and DOM-based XSS.**
Stored XSS persists in your data and hits every viewer. Reflected XSS is echoed from the request and needs the victim to open a crafted link. DOM XSS happens entirely in client-side JavaScript writing untrusted data to sinks like `innerHTML`. Escaping on output handles the first two; DOM XSS needs safe sinks (`textContent`) and Trusted Types.

**How does CSP mitigate XSS, and why nonces?**
The browser only runs scripts the policy allows. A per-response nonce marks legitimate scripts, so injected ones are blocked even if escaping fails. Host allowlists are routinely bypassed through JSONP endpoints and outdated libraries on allowed hosts.

**Does SameSite=Lax make CSRF tokens unnecessary?**
No. It doesn't cover `GET` state changes, attacks from sibling subdomains (same site), or clients with different defaults. It's a strong layer combined with tokens and Origin checks.

**Does an API using JWT bearer tokens need CSRF protection?**
Not if the token is sent in the `Authorization` header, because a cross-site page can't attach it. If the JWT lives in a cookie, the browser sends it automatically and you need CSRF protection again.

**How does Django's CSRF protection work?**
A secret in the `csrftoken` cookie, a masked copy in each form or the `X-CSRFToken` header, compared on every unsafe request, plus `Origin` validation against the host and `CSRF_TRUSTED_ORIGINS`, and a `Referer` check on HTTPS when `Origin` is absent.

## Further reading

- OWASP SQL Injection Prevention Cheat Sheet
- OWASP Cross Site Scripting Prevention Cheat Sheet
- OWASP Cross-Site Request Forgery Prevention Cheat Sheet
- Django documentation, *Security in Django* and *Cross Site Request Forgery protection*
- W3C, *Content Security Policy Level 3*
