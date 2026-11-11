---
title: "Moving legacy Django templates to Vue, one module at a time"
summary: "A strangler-style migration from server-rendered Django templates to Vue.js and REST APIs, with both worlds running side by side."
tags: [django, vue, architecture]
---

At Gainfront I modernized legacy Django template modules into Vue.js on top of REST APIs. Not in one big rewrite: module by module, with old server-rendered pages and new Vue screens living in the same app during the transition.

It was not my first modernization project: at MarsDevs I modernized a Django codebase that was about ten years old. The principle I keep coming back to is that the rewrite you can ship in slices is the one that actually finishes.

## The problem

Server-rendered Django templates are a fine way to build pages. They get painful when a page grows into an application: lots of client-side state, inline jQuery or ad hoc JavaScript reaching into the DOM, partial reloads bolted on, and view functions that build a huge context dict for one template.

> **TODO (author):** describe, in generic terms, what made these particular modules painful (interactivity, bugs, slow iteration, duplicated logic).

The tempting fix is "rewrite the frontend as a SPA". The problem with that is the months of parallel work where nothing ships, while the old app keeps changing underneath you.

## Constraints

- **Ship continuously.** Product work could not stop for a rewrite.
- **One app, one login.** Users should not notice which pages are old and which are new, apart from the new ones being better.
- **No logic duplication.** Business rules stay on the server, in one place, used by both old and new screens during the transition.
- **Reversible steps.** Any single module migration should be small enough to roll back.

## Options considered

| Approach | Upside | Downside |
|---|---|---|
| Big-bang SPA rewrite | Clean slate | Long freeze, double maintenance, high risk |
| Sprinkle Vue widgets into templates | Very incremental | Two sources of truth for state on the same page |
| Strangler: migrate whole modules, Django keeps the shell | Incremental and coherent per module | Two frontend styles coexist for a while |

I went with the strangler approach at the granularity of a *module*: a feature area with its own pages, owned end to end by either Django templates or Vue, never half and half.

## What I built

These are simplified sketches of the pattern, not the production code.

### Django keeps the shell, Vue owns the module

The routing and authentication stay in Django. A migrated module still has a Django URL and view, but that view renders a thin template that mounts a Vue app:

{% raw %}
```html
{% extends "base.html" %}
{% load static %}

{% block content %}
  <div id="reports-app"></div>
  {{ initial_state|json_script:"reports-initial-state" }}
  <script type="module" src="{% static 'dist/reports.js' %}"></script>
{% endblock %}
```
{% endraw %}

This gives you a lot for free:

- The shared base template keeps the same navigation, header and styling as the legacy pages.
- Django's session authentication and permission checks still guard the page.
- `json_script` safely embeds initial data (escaped against XSS) so the first render does not need an extra API call.

On the Vue side, the entry point reads that data and mounts:

```javascript
import { createApp } from "vue";
import ReportsApp from "./ReportsApp.vue";

const el = document.getElementById("reports-initial-state");
const initialState = el ? JSON.parse(el.textContent) : {};

createApp(ReportsApp, { initialState }).mount("#reports-app");
```

> **TODO (author):** fill in the real Vue version and build tooling (bundler, how assets were built and served through `collectstatic`).

### REST APIs designed for the screen, built on the domain

Each migrated module needed an API. The trap is mirroring the template's context dict as JSON. The better rule: the API exposes the domain, and the business logic moves out of the view into something both the old views and the new API can call.

```python
# services.py: one place for the rule, used by templates and API alike
def visible_reports(user, tenant):
    return Report.objects.filter(tenant=tenant).visible_to(user)

# api.py (DRF)
class ReportViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ReportSerializer
    permission_classes = [IsAuthenticated, CanViewReports]

    def get_queryset(self):
        return visible_reports(self.request.user, self.request.tenant)
```

Rules of thumb that held up:

- **Same permission checks as the page.** If the template view checks a permission, the API checks the same one, through the same code.
- **Pagination and filtering on the server** from the start. Shipping a full list to the browser works in dev and falls over on a big tenant.
- **Query counts under control.** A list endpoint with nested serializers is an N+1 waiting to happen; `select_related`/`prefetch_related` belong in `get_queryset`.
- **Consistent error shape** so the Vue side can handle validation errors generically.

> **TODO (author):** mention any API conventions you standardized on (URL structure, versioning, error format) if they are worth sharing.

### Coexistence details that matter

Most of the real work is in the seams between old and new.

**Authentication and CSRF.** Because Vue runs on pages Django served, it can use the same session cookie. DRF's `SessionAuthentication` enforces CSRF on unsafe methods, so the frontend sends the token in a header:

```javascript
function getCookie(name) {
  const match = document.cookie.match(new RegExp("(^|; )" + name + "=([^;]*)"));
  return match ? decodeURIComponent(match[2]) : null;
}

export async function api(path, options = {}) {
  const res = await fetch(`/api/${path}`, {
    credentials: "same-origin",
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": getCookie("csrftoken"),
      ...(options.headers || {}),
    },
  });
  if (!res.ok) throw await res.json();
  return res.json();
}
```

No token endpoints, no CORS configuration, no second auth system to secure.

**Template syntax collisions.** Django templates and Vue templates both use double curly braces. If any Vue markup ever ends up inside a Django template (in-DOM templates, for example), Django will try to render it first. Either keep Vue templates in `.vue` single-file components, which Django never sees, or wrap the region in Django's `verbatim` tag:

{% raw %}
```html
{% verbatim %}
  <span>{{ report.title }}</span>
{% endverbatim %}
```
{% endraw %}

Single-file components are the cleaner answer: the boundary between the two template languages becomes a file boundary.

**Links in both directions.** Old pages link to new ones and vice versa, so URLs must stay stable. Keeping Django in charge of URL routing (with the Vue app handling only in-module navigation, if any) means `{% raw %}{% url %}{% endraw %}` keeps working for every link in the legacy templates.

**Shared look and feel.** The new module should not look like a different product. Reusing the existing CSS and base layout from the shell template does most of this.

### Migrating a module, step by step

The sequence I would follow for each module:

1. Pull the business logic out of the template view into a service function, with tests. Ship that alone; nothing visible changes.
2. Build the API on top of the service. Ship it; nothing calls it yet.
3. Build the Vue module against the API, behind its own URL or a switch.
4. Switch the module's route to the shell template. Keep the old template around until the new screen has run in production for a while.
5. Delete the old template, its view code and any JavaScript only it used.

Step 5 is the one that gets skipped. A strangler migration that never removes the old code is just two systems.

> **TODO (author):** describe how you actually switched modules over (direct cutover, feature flag, per-tenant rollout) and how many modules were migrated.

## Results

Legacy Django template modules were modernized into Vue.js frontends backed by REST APIs, incrementally, while the rest of the application kept running on server-rendered templates.

> **TODO (author):** add any concrete outcome you can stand behind (modules migrated, bugs reduced, delivery speed for features in migrated modules). Only numbers you measured.

## What I would do differently

- **Extract the service layer first, everywhere.** It is valuable even if the frontend never changes, and it makes every later migration step smaller.
- **Decide the API conventions before the first module,** not after the third. Retrofitting consistent pagination and error formats across endpoints is tedious.
- **Track the deletion step explicitly.** Put "remove the old template" in the same ticket as "ship the Vue module".
- **Add query-count tests to new API endpoints.** APIs make it very easy to ship nested data with hidden N+1 queries.

The broader lesson: a frontend migration is mostly a backend refactor. Once the business rules live behind clean service functions and APIs, swapping the rendering layer module by module is the easy part.
