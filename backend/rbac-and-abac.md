---
title: RBAC and ABAC
section: backend
order: 51
phase: "Phase 7 · Security"
summary: Role-, attribute- and relationship-based access control, policy engines like OPA and Cedar, and how to enforce authorization safely in a multi-tenant SaaS.
---

Authentication tells you who is calling; authorization decides what they may do to which object. Most real breaches in SaaS APIs are not broken crypto but broken authorization: an endpoint that loads `/invoices/123` without checking that invoice 123 belongs to the caller's tenant (OWASP lists broken object level authorization as the top API risk). A good authorization design picks a model that fits the product (roles, attributes, relationships), puts the decision in one place, and makes tenant isolation fail closed. Identity comes from [Authentication and sessions]({{ '/backend/authentication-and-sessions/' | relative_url }}) or [OAuth 2.0 and OpenID Connect]({{ '/backend/oauth2-and-oidc/' | relative_url }}).

Every model answers the same question: `allowed(subject, action, resource, context) -> bool`. They differ in what data the answer depends on.

## Role-based access control

RBAC puts a layer between users and permissions: permissions are granted to roles, users are assigned roles. `editor` grants `doc:read`, `doc:write`; Ana is an `editor` in tenant 42.

The NIST RBAC model defines increasing levels:

| Level | Adds | Example |
|---|---|---|
| Flat (core) | Users, roles, permissions, sessions | `viewer`, `editor`, `admin` |
| Hierarchical | Roles inherit from other roles | `admin` ⊇ `editor` ⊇ `viewer` |
| Constrained | Separation of duty (SoD) | The person who creates a payment can't approve it |
| Symmetric | Review of which roles hold a permission | Quarterly access reviews for SOC 2 |

Static SoD forbids holding two conflicting roles at all; dynamic SoD forbids exercising both in the same session or on the same object (a maker-checker rule on a specific payment).

**When it fits:** a small, stable set of job functions, admin screens where customers assign roles, compliance audits ("who can export data?" is a simple join).

**When it breaks:** *role explosion*. As soon as access depends on context (region, project, ownership, document sensitivity) people create `editor_eu`, `editor_project_x`, `editor_project_x_readonly`, and you have thousands of roles nobody can audit. That is the signal to add attributes or relationships, not more roles.

In Django, `Group` is a role and `Permission` is a permission (`app.change_document`). `user.has_perm()` is model-level only; object-level checks need `django-guardian`, `django-rules` or your own policy layer.

## Attribute-based access control

ABAC evaluates rules over attributes of four things:

- **Subject:** department, clearance, tenant, employment type, MFA level.
- **Resource:** owner, tenant, classification, status, region.
- **Action:** read, update, delete, export.
- **Environment:** time, IP range, device posture, how recently the user did MFA.

A rule reads like "editors may update documents in their own tenant that are not locked, and deleting requires MFA in the last 15 minutes". RBAC is a special case: the role is just one subject attribute.

The standard architecture (from XACML, reused by most engines) separates concerns:

```text
     request                         policy admin (PAP)
        |                                  | publish policies
        v                                  v
  +-----------+  subject, action,   +-------------+  lookup   +---------+
  |   PEP     |---- resource ------>|    PDP      |<--------->|   PIP   |
  | API view, |<--- allow/deny -----| policy eval |           | DB, IdP |
  | gateway   |     + reason        | (OPA, Cedar)|           | HR data |
  +-----------+                     +-------------+           +---------+
```

- **PEP** (enforcement point): your view, middleware or gateway; asks and enforces.
- **PDP** (decision point): evaluates policies.
- **PIP** (information point): supplies attributes the request didn't carry.
- **PAP** (administration point): where policies are authored and versioned.

| | RBAC | ABAC |
|---|---|---|
| Expressiveness | Coarse | Fine, contextual |
| Auditability | "Who has role X" is easy | "Who can do Y" requires evaluating policies |
| Performance | Set lookup | Needs attributes at decision time |
| Admin UX | Customers understand roles | Policies need engineers |
| Typical use | Product admin screens | Data sensitivity, compliance, zero trust |

Most products end up hybrid: roles for coarse grants, a handful of attribute conditions (tenant, ownership, status, MFA) on top. AWS IAM is the best-known large ABAC system: policies with `Condition` blocks over tags and request context.

## Relationship-based access control

ReBAC derives permissions from relationships in a graph: "Ana can view this doc because she is a member of the Engineering group, which is a viewer of the folder that contains the doc." This is how Google Drive, GitHub and Notion-style sharing work, and it is awkward in both RBAC and ABAC.

Google's **Zanzibar** paper (USENIX ATC 2019) describes the system behind Drive, YouTube, Calendar and other products, publicly reported as storing trillions of relation tuples and serving millions of checks per second. Its core ideas:

- **Relation tuples** `object#relation@subject`, e.g. `doc:design#editor@user:raj`, `folder:eng#viewer@group:engineers#member`.
- **Userset rewrites** in a namespace config: `editor` implies `viewer`; `viewer` of a doc includes `viewer` of its parent folder.
- **Check** walks the graph; **Expand** returns the tree; reverse lookups answer "what can this user see?".
- **Zookies**: consistency tokens so a check is evaluated at a snapshot no older than the content change. This solves the *new enemy problem*: if you remove Bob from a doc and then add secret content, Bob must not see that content because of a stale ACL read.

Open-source Zanzibar-style systems include OpenFGA (a CNCF project), SpiceDB, Ory Keto and Permify. Adopting one means running another stateful service and keeping tuples in sync with your database (often through an outbox), so it pays off when sharing and nested hierarchies are central to the product.

## Policy engines

Hard-coding `if user.role == "admin"` across 200 views makes policy invisible and untestable. A policy engine moves the rules into a dedicated language evaluated by a PDP.

| Engine | Language | Model | Deployment | Notes |
|---|---|---|---|---|
| OPA | Rego (Datalog-like) | General purpose | Sidecar, daemon, library (Go, Wasm) | CNCF graduated; Kubernetes admission (Gatekeeper), Envoy `ext_authz` |
| Cedar | Cedar | RBAC + ABAC, principal/action/resource | Library (Rust), Amazon Verified Permissions | Designed for analysis; open-sourced by AWS in 2023 |
| Casbin | Model config + policy CSV/DB | ACL, RBAC, ABAC | Library in many languages | Lightweight, in-process |
| OpenFGA, SpiceDB | Schema DSL | ReBAC | Separate service | Zanzibar-style |

An illustrative Rego policy (Rego v1 syntax):

```text
package docs.authz

import rego.v1

default allow := false

allow if {
    input.subject.tenant_id == input.resource.tenant_id
    some role in input.subject.roles
    input.action in data.role_permissions[role]
}
```

The same idea in Cedar, illustrative:

```text
permit (
    principal in Role::"editor",
    action in [Action::"read", Action::"update"],
    resource
)
when { resource.tenant == principal.tenant };

forbid (principal, action, resource)
when { resource.classification == "restricted" }
unless { principal.department == "legal" };
```

Design choices that matter more than the engine:

- **Library vs sidecar vs service.** In-process evaluation costs microseconds; a sidecar adds a local hop (sub-millisecond); a central service adds a network hop and becomes a critical dependency. Whichever you choose, fail closed.
- **Where attributes come from.** Either the PEP sends everything in `input` (fast, but every caller must know what to send) or the PDP fetches from PIPs (simpler callers, slower decisions). OPA typically loads reference data as bundles.
- **Decision logs.** Log every decision with inputs, policy version and result. It is your audit trail and your debugging tool.
- **Policies are code.** Version them, review them, and unit-test them with allow and deny cases.

## Multi-tenant authorization

In SaaS, tenant isolation is the first authorization check and the one that must never be skipped. See [Multi-tenant architecture]({{ '/backend/multi-tenant-architecture/' | relative_url }}) and [Multi-tenancy patterns]({{ '/postgresql/multi-tenancy/' | relative_url }}).

- **Roles are per tenant.** Store a `membership(user_id, tenant_id, role)` table; the same person can be `admin` in one tenant and `viewer` in another. Never store a global `user.role` for tenant permissions.
- **Resolve the tenant, then verify membership.** Whether the tenant comes from a subdomain, a path, a header or a token claim, check that the user is a member before using it. A client-supplied `X-Tenant-Id` without that check is an instant cross-tenant breach.
- **Scope at the data layer.** A default manager or repository that always filters by the current tenant makes a forgotten filter fail closed. PostgreSQL row-level security is a strong second layer.
- **Custom roles.** Enterprise customers want their own roles. Keep permissions system-defined (`invoice:approve`) and let tenants define roles as named bundles of those permissions.
- **Platform staff.** Support engineers accessing customer data should go through an explicit, time-boxed, audited impersonation or break-glass flow, not a superuser flag that bypasses tenant filters.
- **Return 404, not 403,** for objects in other tenants, so IDs can't be probed for existence.
- **The list problem.** "Can Ana read doc 7?" is easy; "which of 2 million docs can Ana read?" can't be answered by calling `check` per row. Push conditions into SQL `WHERE` clauses, use OPA partial evaluation to compile a residual policy into a query filter, or use ReBAC reverse lookups (`ListObjects` in OpenFGA, `LookupResources` in SpiceDB).

## In practice

### A runnable policy evaluator

RBAC with a role hierarchy, ABAC rules with deny-overrides and default deny, tenant isolation, and a Zanzibar-style relationship check. Standard library only.

```python
from dataclasses import dataclass, field

# ---- RBAC: permissions per role, with inheritance -------------------
ROLE_PERMS = {
    "viewer": {"doc:read"},
    "editor": {"doc:write", "doc:comment"},
    "admin": {"doc:delete", "member:invite"},
}
ROLE_PARENT = {"admin": "editor", "editor": "viewer"}


def permissions(roles):
    perms = set()
    for role in roles:
        while role:
            perms |= ROLE_PERMS[role]
            role = ROLE_PARENT.get(role)
    return perms


@dataclass
class Principal:
    id: str
    tenant_id: int
    roles: set  # roles in *this* tenant, from the membership table
    attrs: dict = field(default_factory=dict)


@dataclass
class Resource:
    id: str
    tenant_id: int
    owner_id: str
    attrs: dict = field(default_factory=dict)


# ---- ABAC rules: (effect, actions, condition, reason) ---------------
RULES = [
    ("deny", {"*"},
     lambda p, a, r, env: p.tenant_id != r.tenant_id, "cross-tenant"),
    ("deny", {"doc:read", "doc:write"},
     lambda p, a, r, env: r.attrs.get("classification") == "restricted"
     and p.attrs.get("department") != "legal", "restricted document"),
    ("deny", {"doc:write", "doc:delete"},
     lambda p, a, r, env: r.attrs.get("locked", False), "document locked"),
    ("deny", {"doc:delete"},
     lambda p, a, r, env: env.get("mfa_age_s", float("inf")) > 900,
     "step-up MFA required"),
    ("permit", {"*"},
     lambda p, a, r, env: a in permissions(p.roles), "role grant"),
    ("permit", {"doc:read", "doc:write"},
     lambda p, a, r, env: r.owner_id == p.id, "owner"),
]


def decide(p, action, r, env=None):
    env = env or {}
    hits = [(eff, why) for eff, acts, cond, why in RULES
            if ("*" in acts or action in acts) and cond(p, action, r, env)]
    denies = [why for eff, why in hits if eff == "deny"]
    if denies:
        return False, denies[0]          # deny overrides
    permits = [why for eff, why in hits if eff == "permit"]
    return (True, permits[0]) if permits else (False, "default deny")


# ---- ReBAC: Zanzibar-style tuples object#relation@subject ----------
TUPLES = {
    ("doc:design", "parent", "folder:eng"),
    ("doc:design", "editor", "user:raj"),
    ("folder:eng", "viewer", "group:engineers#member"),
    ("group:engineers", "member", "user:ana"),
}
IMPLIED_BY = {"viewer": ["editor"], "editor": ["owner"]}
INHERITED = {"viewer", "editor"}  # these flow down from the parent


def check(obj, rel, user, depth=0):
    if depth > 16:
        return False                     # guard against cycles
    for o, r, subj in TUPLES:
        if (o, r) != (obj, rel):
            continue
        if subj == user:
            return True
        if "#" in subj:                  # userset, e.g. group:x#member
            s_obj, s_rel = subj.split("#")
            if check(s_obj, s_rel, user, depth + 1):
                return True
    if any(check(obj, r, user, depth + 1) for r in IMPLIED_BY.get(rel, [])):
        return True
    if rel in INHERITED:
        parents = [s for o, r, s in TUPLES if o == obj and r == "parent"]
        return any(check(par, rel, user, depth + 1) for par in parents)
    return False


if __name__ == "__main__":
    ana = Principal("ana", 42, {"editor"})
    bob = Principal("bob", 42, {"viewer"})
    eve = Principal("eve", 7, {"admin"})
    lee = Principal("lee", 42, {"viewer"}, {"department": "legal"})
    doc = Resource("d1", 42, owner_id="bob")
    secret = Resource("d2", 42, "ana", {"classification": "restricted"})

    assert permissions({"admin"}) >= {"doc:read", "doc:write", "doc:delete"}
    assert decide(ana, "doc:write", doc) == (True, "role grant")
    assert decide(bob, "doc:write", doc) == (True, "owner")
    assert decide(bob, "doc:delete", doc)[0] is False
    assert decide(eve, "doc:read", doc) == (False, "cross-tenant")
    assert decide(ana, "doc:read", secret) == (False, "restricted document")
    assert decide(lee, "doc:read", secret)[0] is True
    assert decide(Principal("x", 42, {"admin"}), "doc:delete", doc,
                  {"mfa_age_s": 3600}) == (False, "step-up MFA required")
    assert decide(Principal("x", 42, {"admin"}), "doc:delete", doc,
                  {"mfa_age_s": 60})[0] is True
    assert decide(Principal("x", 42, set()), "doc:read", doc) == \
        (False, "default deny")

    assert check("doc:design", "viewer", "user:ana")      # group -> folder
    assert check("doc:design", "viewer", "user:raj")      # editor => viewer
    assert not check("doc:design", "editor", "user:ana")
    assert not check("doc:design", "viewer", "user:bob")
    print("ok")
```

Returning a reason with every decision costs nothing and makes decision logs and support tickets ("why can't I see this?") answerable.

### Django: scope first, then check

Illustrative: a tenant-scoped queryset plus an object-level DRF permission that calls the same `decide()` function.

```python
# Illustrative Django/DRF wiring.
from rest_framework import permissions, viewsets

class TenantScopedViewSet(viewsets.ModelViewSet):
    def get_queryset(self):
        # Fail closed: every query is tenant-filtered before any lookup.
        return super().get_queryset().filter(tenant_id=self.request.tenant.id)

class PolicyPermission(permissions.BasePermission):
    action_map = {"GET": "doc:read", "PUT": "doc:write",
                  "PATCH": "doc:write", "DELETE": "doc:delete"}

    def has_object_permission(self, request, view, obj):
        allowed, reason = decide(request.principal,
                                 self.action_map[request.method], obj,
                                 {"mfa_age_s": request.mfa_age_s})
        audit_log.info("authz", extra={"allowed": allowed, "reason": reason,
                                       "user": request.principal.id,
                                       "object": obj.pk})
        return allowed

class DocumentViewSet(TenantScopedViewSet):
    queryset = Document.objects.all()
    permission_classes = [permissions.IsAuthenticated, PolicyPermission]
```

Because `get_queryset()` is tenant-filtered, an object from another tenant raises `404` before the permission check runs.

### PostgreSQL row-level security as a backstop

Illustrative:

```sql
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;  -- applies to table owner too

CREATE POLICY tenant_isolation ON documents
    USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::bigint)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::bigint);

-- Per request, inside the transaction (safe with transaction pooling):
BEGIN;
SET LOCAL app.tenant_id = '42';
SELECT id, title FROM documents WHERE id = 7;   -- other tenants invisible
COMMIT;
```

If the setting is missing, `current_setting(..., true)` returns NULL (or an empty string once the session has set it before, hence the `nullif`), so the policy matches nothing and fails closed. The app role must not be a superuser or have `BYPASSRLS`.

## Pitfalls

- **Checks only in the UI or only on list endpoints.** Hiding a button is not authorization. Symptom: a pen tester changes an ID in a `PATCH` URL and edits another tenant's record. Every endpoint, including bulk, export and GraphQL resolvers, goes through the same PDP.
- **Trusting role claims in long-lived tokens.** If roles live in a JWT valid for 24 hours, demoting a user takes 24 hours. Keep tokens short, or look up roles server-side per request with a short cache.
- **Caching decisions without the right key.** Cache keys must include subject, tenant, action, resource and policy version, or a cached "allow" survives a role change.
- **Role explosion.** Hundreds of near-duplicate roles, and access reviews become rubber stamps. Collapse into a few roles plus attribute conditions.
- **Fail-open on PDP errors.** A timeout to the policy service must deny (or serve a stale decision for read-only actions by explicit choice), never allow.
- **Background jobs bypassing authorization.** Celery tasks and admin scripts often use unscoped querysets. Pass the tenant explicitly and use the same scoped managers.
- **Mass assignment.** A serializer that accepts `role` or `tenant_id` lets users promote themselves. Use explicit writable field lists.

## Interview questions

**What is the difference between RBAC and ABAC?**
RBAC grants permissions to roles and roles to users; decisions depend only on role membership. ABAC evaluates rules over subject, resource, action and environment attributes, so it can express ownership, sensitivity, time and MFA conditions. RBAC is easier to audit; ABAC avoids role explosion. Most systems combine them.

**When would you choose ReBAC?**
When access follows relationships and sharing: nested folders, teams, "anyone in this org with the link". Zanzibar-style systems store tuples and walk the graph, and support reverse lookups for listing. The cost is a separate stateful service and keeping tuples consistent with your primary data.

**How do you enforce tenant isolation?**
Resolve the tenant from a trusted source, verify membership, then scope every query at the data layer (scoped managers, repositories) with RLS as defense in depth. Roles live in a per-tenant membership table. Cross-tenant access by staff goes through audited impersonation.

**How do you authorize list endpoints efficiently?**
Translate the policy into a query filter instead of checking each row: SQL conditions for RBAC/ABAC, OPA partial evaluation, or ReBAC reverse-lookup APIs. Paginate after filtering so counts and pages are correct.

**What is the new enemy problem?**
A check evaluated against a stale ACL can grant access a user just lost, for content added after the revocation. Zanzibar solves it with zookies: content writes record a consistency token, and checks run at a snapshot at least that fresh.

**Where should the policy engine run?**
In-process or as a sidecar for latency-sensitive checks, a central service when you need a single point for policy and audit. Fail closed on errors, log decisions with policy versions, and test policies like code.

## Further reading

- Sandhu, Ferraiolo and Kuhn, *The NIST Model for Role-Based Access Control: Towards a Unified Standard* (2000)
- NIST SP 800-162, *Guide to Attribute Based Access Control (ABAC) Definition and Considerations*
- Pang et al., *Zanzibar: Google's Consistent, Global Authorization System* (USENIX ATC 2019)
- Open Policy Agent documentation (Rego and policy testing) and the Cedar policy language documentation
- OWASP API Security Top 10 and the OWASP Authorization Cheat Sheet
