---
title: Django security checklist
section: python-django
order: 16
summary: How Django's CSRF, XSS and SQL injection defenses work, how they get bypassed, and the settings and secrets to verify before every deploy.
---

Django is secure by default in the places that matter most: templates autoescape, the ORM parameterizes queries, forms and views check CSRF tokens. Real-world vulnerabilities in Django apps come from opting out of those defaults (`|safe`, `raw()` with f-strings, `csrf_exempt`), from deployment settings left at development values, and, in multi-tenant systems, from missing tenant filters. This note covers what each protection does and how it gets bypassed.

## How it works

### CSRF

`CsrfViewMiddleware` requires a token on unsafe methods (POST, PUT, PATCH, DELETE) for requests authenticated by cookies. The token comes from the `csrftoken` cookie and must be echoed in a form field or the `X-CSRFToken` header. The middleware also checks the `Origin` header (and `Referer` on HTTPS) against `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`.

DRF specifics:

- `SessionAuthentication` enforces CSRF for authenticated requests (it calls the CSRF check itself because DRF views are `csrf_exempt`).
- `TokenAuthentication` and JWT don't use cookies, so CSRF doesn't apply. If you store a JWT in a cookie, you are back to needing CSRF protection.

### XSS and autoescaping

The template engine escapes `<`, `>`, `&`, `'` and `"` in every variable. Escaping is turned off by:

{% raw %}
```text
{{ comment.body|safe }}
{% autoescape off %}{{ invoice.notes }}{% endautoescape %}
```
{% endraw %}

and in Python by `mark_safe()` or by building HTML with string formatting. Autoescaping also does not protect values placed inside a `<script>` block or in unquoted attributes, or URLs like `javascript:...` in an `href`.

Safe patterns:

{% raw %}
```text
{# pass data to JS without injection #}
{{ chart_data|json_script:"chart-data" }}

<a href="{{ invoice.portal_url }}">Open</a>  {# validate scheme server-side #}
```
{% endraw %}

```python
from django.utils.html import format_html

def status_badge(status: str) -> str:
    return format_html('<span class="badge badge-{}">{}</span>', status, status)
```

`format_html` escapes every argument and marks only the template string safe. For an API-only backend with a Vue or React frontend, the framework's escaping applies on the client; the same rules hold for `v-html` / `dangerouslySetInnerHTML`.

### SQL injection

The ORM sends values as query parameters, never interpolated into SQL. Injection appears when you build SQL strings yourself:

```python
# VULNERABLE
Invoice.objects.raw(f"SELECT * FROM billing_invoice WHERE number = '{number}'")
Invoice.objects.extra(where=[f"number = '{number}'"])
cursor.execute("SELECT ... WHERE tenant_id = %s AND status = '%s'" % (tid, status))

# SAFE: parameters passed separately
Invoice.objects.raw(
    "SELECT * FROM billing_invoice WHERE tenant_id = %s AND number = %s",
    [tenant_id, number],
)
with connection.cursor() as cur:
    cur.execute("SELECT count(*) FROM billing_invoice WHERE tenant_id = %s", [tenant_id])
```

- Don't quote placeholders: `'%s'` is wrong, `%s` is right.
- Identifiers (table, column, sort direction) can't be parameters. Whitelist them:

```python
SORTABLE = {"created": "created_at", "amount": "amount", "number": "number"}

def sorted_invoices(qs, sort_param: str):
    field = SORTABLE.get(sort_param.lstrip("-"), "created_at")
    return qs.order_by(f"-{field}" if sort_param.startswith("-") else field)
```

- `RawSQL(sql, params)` and `extra()` take params too; `extra()` is effectively deprecated, avoid it.
- JSON keys from user input in `KeyTextTransform` or `values()` lookups: also whitelist.

### Clickjacking, HTTPS and headers

- `XFrameOptionsMiddleware` sets `X-Frame-Options: DENY` by default.
- `SecurityMiddleware` handles HSTS, SSL redirect, `X-Content-Type-Options: nosniff`, `Referrer-Policy` and `Cross-Origin-Opener-Policy`.
- Content Security Policy is built into Django 6.0; on 5.x use `django-csp`.

## In practice

### Production settings

```python
DEBUG = False
ALLOWED_HOSTS = [".app.example.com"]          # subdomain tenants
CSRF_TRUSTED_ORIGINS = ["https://*.app.example.com"]   # scheme required (4.0+)

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")  # only behind a proxy that sets it
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000               # start low (3600), then raise
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True               # default
SESSION_COOKIE_SAMESITE = "Lax"              # default
X_FRAME_OPTIONS = "DENY"

DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
```

Why each matters:

- `DEBUG = True` in production exposes settings, SQL and stack traces on every error page, and grows memory.
- `ALLOWED_HOSTS` blocks Host-header attacks (poisoned password-reset links built from `request.get_host()`).
- `SECURE_PROXY_SSL_HEADER` set when the proxy does **not** strip client-supplied `X-Forwarded-Proto` lets clients claim HTTPS.

### Run the deploy check

```bash
DJANGO_SETTINGS_MODULE=config.settings.prod python manage.py check --deploy --fail-level WARNING
```

It flags missing HSTS, insecure cookies, `DEBUG`, a weak `SECRET_KEY` and more. Run it in CI against production settings.

### Secrets

- Load `SECRET_KEY`, DB passwords and API keys from the environment or a secrets manager, never from the repo.
- Rotate `SECRET_KEY` without logging everyone out using `SECRET_KEY_FALLBACKS` (4.1+): put the old key there, deploy, remove it later.
- `SECRET_KEY` signs sessions, password reset tokens and `signing` payloads. A leak means forged sessions; rotate immediately.
- Scrub secrets and PII from logs and Sentry (`send_default_pii=False`, `before_send` filters). Django's error reports already mask settings whose names contain `KEY`, `SECRET`, `TOKEN`, `PASS`.
- Scan the repo in CI (`gitleaks`, GitHub secret scanning).

### Multi-tenant authorization

The most common SaaS vulnerability is not XSS; it's IDOR: `GET /api/invoices/123/` returning another tenant's invoice.

```python
class InvoiceViewSet(viewsets.ModelViewSet):
    def get_queryset(self):
        return Invoice.objects.filter(tenant=self.request.tenant)   # always
```

- Scope every queryset by tenant in `get_queryset`, not in permission checks after fetching.
- A tenant-aware default manager or a base viewset makes forgetting harder.
- Validate foreign keys in input: a serializer `PrimaryKeyRelatedField(queryset=...)` must also be tenant-filtered, or users can attach tenant B's customer to their invoice.
- Test cross-tenant access for every endpoint.

### Other items

- **Passwords**: Argon2 (`argon2-cffi`) or the default PBKDF2; enable `AUTH_PASSWORD_VALIDATORS`.
- **File uploads**: validate type and size, store outside the web root or on S3 with private ACLs, serve via signed URLs, never trust the client filename.
- **Open redirects**: check `next` parameters with `url_has_allowed_host_and_scheme`.
- **Rate limiting**: login, password reset and expensive endpoints (DRF throttling, or at the proxy).
- **Admin**: move it off `/admin/`, require 2FA, restrict by network if possible.
- **Dependencies**: `pip-audit` in CI; keep Django on a supported release and apply security patches promptly.

## Pitfalls

- **`|safe` on user content** or on content derived from it (Markdown rendered without sanitizing). Sanitize with `nh3` before marking safe.
- **`csrf_exempt` on a view to fix a frontend bug.** Fix the frontend to send `X-CSRFToken` instead.
- **f-strings in `raw()`, `extra()`, `RawSQL` or cursors.** Grep for them in review.
- **Debug toolbar or `DEBUG` enabled via an env var defaulting to true.** Default to the safe value.
- **Error messages leaking existence.** "Invoice belongs to another tenant" tells the attacker it exists; return 404.
- **Serializers with `fields = "__all__"`.** New model fields (internal notes, cost price) become exposed and writable. List fields explicitly.

## Checklist

- `manage.py check --deploy` passes in CI.
- `DEBUG=False`, strict `ALLOWED_HOSTS`, HTTPS settings and secure cookies.
- Secrets from env/secret manager; `SECRET_KEY_FALLBACKS` for rotation.
- No `|safe`/`mark_safe` on untrusted data; `json_script` for JS data.
- No string-built SQL; whitelist identifiers.
- Every queryset tenant-scoped; cross-tenant tests per endpoint.
- Explicit serializer fields; rate limits on auth endpoints; `pip-audit` in CI.

## Interview angle

**How does Django prevent SQL injection?** The ORM passes values as driver parameters. Injection only appears with string-built SQL in `raw()`, `extra()`, `RawSQL` or cursors.

**When is CSRF needed in a DRF API?** When requests are authenticated by cookies (session auth, or JWT in a cookie). Header-based token auth isn't vulnerable to CSRF.

**How does autoescaping fail?** `|safe`, `mark_safe`, `autoescape off`, values inside `<script>` or unquoted attributes, and `javascript:` URLs.

**What's the biggest security risk in a multi-tenant Django app?** Missing tenant scoping (IDOR). Enforce it in `get_queryset` and related-field querysets, and test it.
