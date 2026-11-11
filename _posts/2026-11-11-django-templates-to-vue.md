---
date: 2026-11-11 09:00:00 +0530
title: "Moving legacy Django templates to Vue, one module at a time"
summary: "Moving Django template modules to a Vue app on REST APIs: a per-client switch, an API built for screens, and deleting the old code."
tags: [django, vue, architecture]
---

The first legacy screen I moved off Django templates at Gainfront was the form for creating a supplier. Building the new Vue page was the easy part. What I thought about longest was a few lines in the old view: the ones that decide, for each client, which of the two pages a user gets.

Gainfront makes supplier lifecycle management software for procurement teams. A procurement team keeps a profile for every supplier it buys from, and the supplier fills in much of it. A savings initiative is a line like "we renegotiated packaging and expect to spend 1,200 instead of 1,500 this year", and a savings tracker shows how much of that difference really turns up, month by month. Most of my migration work was on those two areas.

I had done a version of this before at MarsDevs, in a Django codebase about ten years old: replacing old implementations piece by piece and building new screens in Vue against Django REST Framework APIs. The rule I took from both jobs is that the rewrite you can ship in slices, and switch off, is the one that finishes.

## What was wrong with the template pages

Server-rendered templates are a fine way to build pages. They get painful when a page turns into an application. The screens we wanted next were editable data tables, dashboards and forms that validate as you type, and on a template page each of those means page-specific JavaScript bolted onto HTML the server already rendered.

The less visible problem: the old pages and the API answered "may this user do this?" through different code. Template views read a permission dictionary that middleware built for them, while REST endpoints used DRF permission classes. Two answers to one question eventually disagree.

## Constraints

- **Ship continuously.** Product work could not stop for a rewrite.
- **One login, one origin.** Users should not be able to tell which pages are old, except that the new ones are better.
- **Business rules stay on the server,** in one place that old and new screens share.
- **Every step reversible,** per client. Clients adopt changes at different speeds, and each has its own configuration.

## Options considered

| Approach | Upside | Downside |
|---|---|---|
| Big-bang SPA rewrite | Clean slate | Long freeze, double maintenance, high risk |
| Vue widgets inside templates | Very incremental | Two owners of state on one page; clashing double-brace syntax |
| Separate frontend on its own domain | Clean split | CORS, token auth, a second deploy |
| Strangler: whole modules move to a Vue app served by Django | Incremental, coherent per module | Two frontends coexist for a while |

We went with the strangler, at the granularity of a module: a feature area is owned end to end by either templates or Vue, never half and half.

## One Django, two front doors

The Vue app is not a separate site. Our build compiles it with Vite in the first stage of the Docker image, and a catch-all Django route returns the built `index.html` for every path under the app's prefix. In development that route proxies to the Vite dev server, so hot reload works.

```text
              Browser (one origin, one session cookie)
                |                              |
    old pages   |                              |   new screens
                v                              v
   +-------------------------+   +---------------------------+
   | Django template views   |   | catch-all route serves    |
   | forms, redirects        |   | the built Vue index.html  |
   +------------+------------+   +-------------+-------------+
                |                              | JSON + CSRF header
                |                              v
                |                +---------------------------+
                |                | DRF ViewSets: permissions,|
                |                | filters, pagination       |
                |                +-------------+-------------+
                |                              |
                v                              v
   +-----------------------------------------------------------+
   | services, models, permission codenames, PostgreSQL        |
   +-----------------------------------------------------------+
```

Because the app, the API and the old pages share an origin, the browser sends one session cookie to all of them: no tokens in the browser, no CORS, no second auth system. DRF's `SessionAuthentication` enforces CSRF on unsafe methods, and axios sends Django's token once you tell it the cookie and header names. Every response is parsed against a schema, so contract drift fails loudly in one place instead of as `undefined` three components later:

```javascript
import axios from "axios";
import { z } from "zod";

// Same origin as the Django pages, so the session cookie just works.
export const http = axios.create({
  baseURL: "/api",
  xsrfCookieName: "csrftoken",     // Django's CSRF cookie
  xsrfHeaderName: "X-CSRFToken",   // the header Django checks
});

const SupplierRef = z.object({ id: z.number(), name: z.string() });

export const Initiative = z.object({
  id: z.number(),
  name: z.string(),
  supplier: SupplierRef,
  estimated_spend: z.string(),     // DRF sends decimals as strings
  spend: z.string(),
});

const page = (row) =>
  z.object({ count: z.number(), results: z.array(row) });

export async function listInitiatives(params) {
  const { data } = await http.get("/savings-initiatives/", { params });
  return page(Initiative).parse(data);
}
```

## Switching one module, one client at a time

The supplier-create flow used to be the first step of a multi-step form, one class-based view per step. I built the Vue page, extended the create endpoint it calls, then put the switch in the old view: on a GET, if this client has the new screen turned on, redirect. Written as a mixin, which is how I would do it now:

```python
from django.shortcuts import redirect


class NewScreenRedirectMixin:
    """Send GETs to the Vue screen when this client has it switched on."""

    new_screen_setting = None   # name of the per-client switch
    new_screen_url = None       # where the Vue route lives

    def dispatch(self, request, *args, **kwargs):
        if request.method == "GET" and client_setting(
            request, self.new_screen_setting
        ):
            return redirect(self.new_screen_url)
        return super().dispatch(request, *args, **kwargs)


class SupplierCreateView(LoginRequiredMixin, NewScreenRedirectMixin,
                         CreateView):
    new_screen_setting = "new_screens.supplier_create"
    new_screen_url = "/spa/suppliers/new/"
    form_class = SupplierStepOneForm
    template_name = "suppliers/step_one.html"
```

Three details carry the weight:

- **Only GET redirects.** A form already open in someone's tab when the switch flipped still posts to the old view, and works.
- **Login runs first.** `LoginRequiredMixin` sits before the redirect mixin, so an anonymous user lands on the login page, not on a Vue route that bounces them.
- **Old links keep working.** Menus, emails and bookmarks point at the old URL, which now decides where to send you.

Rolling back is flipping a setting for one client. No deploy.

```text
 stage          GET old URL          POST old URL          new screen
 -------------  -------------------  --------------------  ----------
 1 switch off   template renders     old view saves        built, idle
 2 on for one   302 to the Vue page  old view saves        live there
   client                            (tabs already open)
 3 on for all   302 to the Vue page  old view saves        live
 4 cleanup      route deleted        route deleted         live
```

One rule lived only in the old flow: when a supplier's own user creates the supplier record, they become its first contact. Copying that into the Vue page would have put a business rule in the browser, so I moved it behind the create endpoint, where every caller gets it.

## An API shaped for screens

The trap with a migrated module's endpoints is mirroring the old template's context dictionary as JSON. Our shared base layer, which every module inherits, pushes the other way:

- **Flat URLs, one per model.** A child takes its parent in the body or as a filter, not in a nested path, so "does this belong to that?" is checked in one serializer, not every view.
- **Bounded pagination.** Limit and offset with a hard maximum, because the UI needs "page 7 of 20" and nobody should be able to ask for a million rows.
- **One filter language.** Every list takes the same `search`, `id__in` and `order_by` parameters. `order_by` is checked against a whitelist, and an unknown value is ignored rather than rejected, so an old frontend survives a renamed field.
- **A protected delete returns 409** naming what blocked it, not a 500.
- **One permission class per module** serving the admin side, the supplier side and machine clients from one pair of read and write codenames.
- **An expand endpoint** that nests related objects one level deep, for detail screens.

The decision we revisited most was how a foreign key travels over the wire. Our oldest modules nest the related object and resolve it on write through a lookup table each serializer maintains by hand. The next generation went flat: plain integer ids both ways, with the frontend fetching names in batches, which costs every list screen a second round of requests. The current convention, which I moved the supplier module onto, reads a foreign key as a small object with an id and a display name and writes it as `{"id": N}`, through one generic field. A simplified version:

```python
from rest_framework import serializers


class SupplierRef(serializers.PrimaryKeyRelatedField):
    """Reads as {"id": 7, "name": "..."}; writes as {"id": 7}."""

    def use_pk_only_optimization(self):
        return False   # the read shape needs the name, not just the pk

    def to_representation(self, supplier):
        return {"id": supplier.pk, "name": supplier.name}

    def to_internal_value(self, data):
        if not isinstance(data, dict) or "id" not in data:
            raise serializers.ValidationError('Send {"id": <int>}.')
        return super().to_internal_value(data["id"])


class InitiativeSerializer(serializers.ModelSerializer):
    supplier = SupplierRef(queryset=Supplier.objects.all())

    class Meta:
        model = SavingsInitiative
        fields = ["id", "name", "supplier", "estimated_spend", "spend"]
```

With this field, a row you GET is also a valid body to PUT back, and a list cell shows `row.supplier.name` with no extra request. A bare integer is a 400 on purpose: one enforced shape per field is easier to keep in sync with a frontend schema than a field that quietly accepts two. In real code the queryset is scoped to what the caller may see, so a foreign id fails with the same "does not exist" error as one that was never there.

## Porting my own module, then porting it again

I built the savings tracker end to end, with its screens on the Vue app we had at the time. A few months later the team's new frontend arrived (Vue 3, TypeScript, zod, a new component library), and I ported my own module to it.

The order mattered more than the code. Schemas first, with tests, because every later file imports them. Then the API helper. Then the small pieces: formatting and proration utilities, and composables for pagination, grouping and export. Only then the screens, from the report table and the form to the dashboard. Then I pointed the menu links at the new screens and deleted the old copies.

Later I ported the supplier profile screens the same way, more than a dozen form steps plus the profile tabs. Porting is a good bug finder: typed schemas turn sloppy old code into compile errors, and a new component library has its own opinions. Some of what came out:

- **A stock ticker field was a number input,** so a ticker with a dot in it could not be typed. Old code also mapped over `{value, label}` objects as if they were strings. The types caught both.
- **A save that sent nothing and still said "saved".** The write schema took its read-only list from the serializer's `Meta.read_only_fields`, which DRF applies only to fields it generates itself. Seven explicitly declared fields were in fact writable, the schema left them out, zod stripped them before sending, and one form posted an empty update with a success toast.
- **Two dropdowns that never opened.** The old lists started with a blank `---` option whose value was an empty string, and the new library's select will not render its menu when any option has an empty value. No error anywhere.
- **A lost update.** One dialog saved by sending the whole supplier row back with PUT, so a field someone else changed while the dialog was open got quietly reverted. It now sends only the field it edits.

The ones the types forced, I fixed on the spot. The rest I carried across unchanged with a comment at the site, to fix in separate commits (the lost update was one), so the port itself never changed behavior quietly.

Deleting gets its own commit, so a revert restores the old screens without touching the new app. For the supplier profile that commit removed about 80 files from the old app and deliberately kept a few: other unmigrated modules still imported some of those components, and a blanket delete of the directory would have taken live code with it. Delete at zero importers, and grep to prove it.

## Moving the API under a live screen

Moving the supplier module's API onto the newer conventions was the riskiest piece: every consumer breaks the moment the wire shape changes. First I wrote characterization tests pinning every rule the old code enforced, including side effects in save hooks and who may write which field. Then I moved the profile's child records (contacts, addresses, certificates) from routes nested under the supplier to flat routes with the parent in the body, each child saved on its own. That matters: some of our older modules save children inside the parent's payload, and any child missing from the payload gets deleted.

The part I would copy into any migration is a permission matrix, one table of who can do what, tested row by row:

```python
from rest_framework.test import APITestCase


class InitiativePermissionMatrixTest(APITestCase):
    url = "/api/savings-initiatives/"

    @classmethod
    def setUpTestData(cls):
        cls.reader = make_staff_user(perms=["initiative_read"])
        cls.writer = make_staff_user(perms=["initiative_read",
                                            "initiative_write"])
        cls.vendor = make_supplier_user(perms=["initiative_read"])
        cls.row = make_initiative()

    def test_matrix(self):
        detail = f"{self.url}{self.row.pk}/"
        cases = [
            # user        method    url         expected
            (None,        "get",    self.url,   401),
            (self.reader, "get",    self.url,   200),
            (self.reader, "delete", detail,     403),
            (self.vendor, "get",    self.url,   403),  # buyer-side route
            (self.writer, "delete", detail,     204),
        ]
        for user, method, url, expected in cases:
            with self.subTest(user=user, method=method):
                self.client.force_authenticate(user)
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, expected)
```

The anonymous row expects 401 only if your first authentication class can send a `WWW-Authenticate` header; with session auth first, DRF answers 403. Alongside the matrix, a foreign row returns 404 rather than 403, a reader's failed write leaves the row unchanged, and list endpoints get a query-count test.

## Results

- The supplier-create flow moved from a template view to Vue behind a per-client switch, with the old view still answering in-flight posts.
- My savings tracker moved to the new frontend, and its old copies were deleted.
- The supplier profile screens moved too, with about 80 old files deleted.
- The supplier module's API moved onto our newer conventions, with characterization, permission and query-count tests in front of the change.

Teammates moved other modules the same way, and a teammate retired the older Vue app once the last module had left it. I have no delivery-speed number for any of this, and would distrust one. What I can point to: each step shipped on its own and had a cheap way back, a per-client setting for the template switch and a one-commit revert for each deletion.

## What I would do differently

- **Settle the foreign-key wire shape before the first module.** We changed it twice, so some modules built on the middle design had to move again.
- **Move the API before porting the screens.** I ported the supplier profile screens first and moved its API afterwards, so the new screens had to change again when the wire shape did.
- **Write the redirect as a mixin from the start.** My first version was an inline `dispatch` override. Fine for one view, but the second view that needs it should not get a copy.

Plenty of Django template pages are still live. The next ones will go the same way, in the order I should have used from the start: the endpoint first, then the screen behind a per-client switch, then a commit that deletes the old view.
