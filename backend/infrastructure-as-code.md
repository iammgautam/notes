---
title: Infrastructure as code
section: backend
order: 67
phase: "Phase 9 · DevOps and observability"
summary: "Terraform state, plan and apply, modules and drift; Pulumi and CloudFormation; GitOps with Argo CD and Flux; and policy as code with OPA and admission control."
---

Infrastructure created by clicking in a console can't be reviewed, reproduced or rolled back. Nobody knows why the security group allows port 5432 from everywhere, staging differs from production in twenty undocumented ways, and rebuilding a region after a disaster means guessing. Infrastructure as code (IaC) puts the definition of networks, databases, clusters, DNS and permissions in version-controlled files that go through the same review and pipeline as application code. The tool compares that definition with reality and makes the changes needed to converge.

## Approaches and tools

Two distinctions shape every IaC tool:

- **Declarative vs imperative.** Declarative tools describe the end state ("a Postgres 16 instance with 200 GB, Multi-AZ") and compute the steps. Imperative scripts describe steps ("create, then attach, then resize") and break when reality differs from what the script assumed.
- **Mutable vs immutable infrastructure.** Configuration management (Ansible, Chef, Puppet) changes existing servers in place, which accumulates drift. Immutable infrastructure replaces instead of patching: new image, new instances, old ones destroyed. Containers and IaC pushed the industry toward immutable.

| Tool | Language | State | Scope |
|---|---|---|---|
| Terraform / OpenTofu | HCL | State file you store (S3, GCS, HCP Terraform) | Any provider: clouds, DNS, GitHub, Datadog |
| Pulumi | Python, TypeScript, Go, C#, Java | Pulumi Cloud or self-managed backend | Same breadth as Terraform |
| AWS CloudFormation / CDK | YAML or JSON; CDK in general-purpose languages | Managed by AWS | AWS only |
| Crossplane | Kubernetes CRDs | Kubernetes API (etcd) | Cloud resources managed as Kubernetes objects |
| Ansible | YAML playbooks | None (mostly imperative, idempotent modules) | Server configuration, ad hoc operations |

## Terraform

Terraform reads `.tf` files, builds a dependency graph of resources (an RDS instance depends on a subnet group, which depends on subnets), and calls **providers**, plugins that wrap a cloud's API, to create, update or delete them in dependency order, in parallel where the graph allows. In 2023 HashiCorp moved Terraform to the source-available Business Source License; the community fork **OpenTofu**, under the Linux Foundation, stays open source and is largely compatible. The concepts below apply to both.

### State

The state file maps each resource address in code (`aws_db_instance.orders`) to a real object ID (`orders-prod`) and caches its attributes. Without it Terraform couldn't know that a resource in code already exists, or which real objects it owns.

- **Store it remotely**: an S3 bucket with versioning and encryption, GCS, or HCP Terraform. Never on a laptop or in Git.
- **Lock it.** Two concurrent applies on one state corrupt it. The S3 backend can lock with a lock file in the bucket (`use_lockfile`, Terraform 1.10+); older setups use a DynamoDB table.
- **Treat it as a secret.** State contains every attribute, including generated passwords and connection strings. Restrict access, encrypt it, and prefer features that keep secrets out of state, such as letting RDS manage the master password in Secrets Manager. OpenTofu also offers client-side state encryption.
- **Split it.** One state per environment and per component (network, data, cluster, each service) limits the blast radius of a bad apply, keeps plans fast, and lets teams work in parallel. Components read each other's outputs through remote state data sources or, more loosely coupled, through lookups by tag or name.
- **Refactor with blocks, not surgery.** `moved` blocks rename addresses without destroying resources, `import` blocks adopt existing resources into code, and `removed` blocks stop managing a resource without deleting it. They are reviewed in PRs, unlike ad hoc `terraform state mv` commands.

### Plan and apply

`terraform plan` refreshes the state from the real APIs, diffs it against the code, and prints the actions: create (`+`), update in place (`~`), destroy (`-`), or **replace** (`-/+`) when an attribute can't change in place, such as an RDS `identifier` or a subnet's CIDR. `terraform apply` executes a plan.

The working pattern:

1. On a pull request, CI runs `plan -out=tfplan` and posts the human-readable plan to the PR. Reviewers read the plan, not just the code: a one-line change can produce a replace of a database.
2. Policy checks run against the plan's JSON (see policy as code below).
3. On merge, CI applies **that saved plan file**. Terraform refuses a saved plan if the state changed since, so what was reviewed is what runs.

Guard rails for stateful resources: `lifecycle { prevent_destroy = true }` makes any plan that would destroy the resource fail, provider-side `deletion_protection` blocks deletion at the API, and `create_before_destroy` changes replacement order for resources that support running two copies. Use `-target` only in emergencies; it applies a partial graph and leaves the rest unreconciled.

The core of plan is a three-way comparison you can model in a few lines. A runnable toy with force-new attributes, drift and a `prevent_destroy` guard:

```python
# Which attribute changes force a destroy-and-recreate, per resource type.
FORCE_NEW = {
    "db_instance": {"engine", "identifier"},
    "bucket": {"name"},
}


def plan(desired, actual):
    """desired/actual: address -> attribute dict; returns a list of actions."""
    actions = []
    for addr in sorted(desired.keys() | actual.keys()):
        want, have = desired.get(addr), actual.get(addr)
        if have is None:
            actions.append(("create", addr, sorted(want)))
        elif want is None:
            actions.append(("delete", addr, []))
        else:
            changed = sorted(k for k in want.keys() | have.keys()
                             if want.get(k) != have.get(k))
            if not changed:
                continue
            forced = FORCE_NEW.get(want["type"], set()) & set(changed)
            kind = "replace" if forced else "update"
            actions.append((kind, addr, changed))
    return actions


def check_guards(actions, protected):
    """Mimic lifecycle prevent_destroy: refuse plans that destroy these."""
    bad = [a for kind, a, _ in actions if kind in ("delete", "replace")
           and a in protected]
    if bad:
        raise RuntimeError(f"plan would destroy protected resources: {bad}")


if __name__ == "__main__":
    desired = {
        "db_instance.main": dict(type="db_instance", engine="postgres",
                                 identifier="orders", instance_class="db.r6g.large"),
        "bucket.uploads": dict(type="bucket", name="acme-uploads", versioning=True),
    }
    # state as last refreshed from the cloud API: someone resized the DB by hand
    actual = {
        "db_instance.main": dict(type="db_instance", engine="postgres",
                                 identifier="orders", instance_class="db.r6g.xlarge"),
        "bucket.old_logs": dict(type="bucket", name="acme-old-logs"),
    }
    actions = plan(desired, actual)
    assert actions == [
        ("delete", "bucket.old_logs", []),
        ("create", "bucket.uploads", ["name", "type", "versioning"]),
        ("update", "db_instance.main", ["instance_class"]),   # drift reverted
    ], actions

    # renaming the DB identifier is not an in-place update: it is a replace
    desired["db_instance.main"]["identifier"] = "orders-v2"
    actions = plan(desired, actual)
    assert ("replace", "db_instance.main", ["identifier", "instance_class"]) in actions
    try:
        check_guards(actions, protected={"db_instance.main"})
        raise AssertionError("guard should have refused")
    except RuntimeError as exc:
        assert "db_instance.main" in str(exc)

    assert plan(actual, actual) == []          # no changes: exit code 0
    print("plan ok")
```

Real Terraform also keeps the previous state as a third input, which is how it knows that `bucket.old_logs` was created by this configuration and is safe to delete, rather than something it never managed.

### Modules

A module is a directory of `.tf` files with input variables and outputs: the unit of reuse. A company `postgres` module encodes the defaults everyone should get (encryption, backups, deletion protection, parameter group, alarms) so each service's database is ten lines of inputs.

- **Version modules** with Git tags or a registry and pin them (`?ref=v3.2.0`), so a module change doesn't silently alter every consumer's next plan.
- **Keep modules shallow.** A module wrapping a single resource with every argument passed through adds indirection without value. Good modules capture a decision ("our standard service database"), not a resource.
- **Prefer `for_each` over `count`** for collections. With `count`, removing the first item shifts every index and Terraform wants to recreate the rest; `for_each` keys resources by name.
- **Environment layout**: a directory per environment (`live/staging`, `live/prod`) calling the same versioned modules with different inputs is easy to reason about and lets prod pin an older module version. Workspaces share one configuration across environments and suit near-identical copies. Terragrunt is a common wrapper for keeping many such directories DRY.

### Drift

Drift is any difference between real infrastructure and code: a hand-resized instance during an incident, a security group rule added in the console, a tag changed by another tool. Detection is simple: run `terraform plan -detailed-exitcode` on a schedule (exit code 2 means there are changes) and alert. Each drift gets one of two fixes: revert it by applying the code, or codify it by changing the code to match. Fields legitimately owned by something else, such as an autoscaling group's desired count managed by the autoscaler, belong in `lifecycle { ignore_changes = [...] }`. Prevent drift in the first place by making the console read-only for humans in production and routing changes through the pipeline, with a documented break-glass role for emergencies.

## Pulumi and CloudFormation

**Pulumi** has the same model as Terraform (providers, state, preview, update) but programs are written in Python, TypeScript or Go. You get loops, functions, classes, unit tests and package managers, at the cost of code that can be more clever than infrastructure should be. Many Pulumi providers are bridged from Terraform providers, so resource coverage is similar. Illustrative:

```python
import pulumi
import pulumi_aws as aws

uploads = aws.s3.Bucket("uploads", tags={"team": "orders", "env": "prod"})
pulumi.export("uploads_bucket", uploads.id)
```

**CloudFormation** is AWS's managed IaC service: you submit a template, AWS keeps the state, applies changes as a **stack**, previews them with **change sets**, rolls back automatically when an update fails, and has built-in drift detection. No state file to manage, but AWS only, and new AWS features sometimes arrive late. The **AWS CDK** lets you write Python or TypeScript that synthesizes CloudFormation templates.

| | Terraform / OpenTofu | Pulumi | CloudFormation / CDK |
|---|---|---|---|
| Language | HCL | General-purpose | YAML/JSON, or code via CDK |
| State | You manage it | Pulumi Cloud or your backend | AWS manages it |
| Clouds | All, plus SaaS providers | All | AWS |
| Failed apply | Partial; fix and re-apply | Partial; fix and re-apply | Automatic rollback |
| Best when | Multi-cloud or many SaaS providers | Team wants real code and tests | AWS-only, wants managed state |

## GitOps with Argo CD and Flux

GitOps applies the IaC idea to what runs inside Kubernetes, with one twist: the cluster **pulls** its desired state from Git instead of CI pushing changes into it. The OpenGitOps principles: state is declarative, versioned and immutable in Git, pulled automatically by an agent, and continuously reconciled.

```text
 push-based CD                          pull-based GitOps
 -------------                          -----------------
 CI --kubectl apply--> cluster          CI --commit new image digest--+
 (CI holds cluster-admin creds)                                       v
                                                              config repo
                                                                     ^
                                        cluster: Argo CD or Flux ----+
                                        watches the repo, diffs it
                                        with live state, applies,
                                        reverts manual drift
```

Why teams adopt it:

- **No cluster credentials in CI.** The agent runs inside the cluster with its own permissions.
- **Drift correction.** A manual `kubectl edit` is reverted on the next sync (with self-heal on), and the UI shows exactly what differs.
- **Audit and rollback through Git.** Every production change is a commit; rollback is `git revert`.
- **Promotion as pull requests.** Promoting to production means a PR that changes the image digest in `overlays/prod`.

**Argo CD** centers on `Application` resources with a web UI, sync waves and hooks, and multi-cluster management. **Flux** is a set of controllers (`GitRepository`, `Kustomization`, `HelmRelease`) with a CLI-first feel. Both are CNCF graduated projects. The usual split is two repositories: application code (CI builds and pushes the image) and environment configuration (CI or an image updater commits the new digest). Secrets stay out of plaintext Git through SOPS or Sealed Secrets (encrypted in Git) or the External Secrets Operator (references to a secret manager). Terraform typically owns the cluster, network and databases; GitOps owns what runs on the cluster.

## Policy as code

Code review can't reliably catch "this bucket is public" in a 400-line plan. Policy as code expresses rules as executable checks that run automatically, in CI against plans and manifests, and at admission time in the cluster.

| Tool | Where it runs | Policy language |
|---|---|---|
| OPA with Conftest | CI, on Terraform plan JSON, Kubernetes YAML, Dockerfiles | Rego |
| OPA Gatekeeper | Kubernetes admission controller | Rego in constraint templates |
| Kyverno | Kubernetes admission and background scans | YAML patterns, CEL |
| ValidatingAdmissionPolicy | Built into Kubernetes (GA in 1.30) | CEL |
| Sentinel | HCP Terraform / Terraform Enterprise | Sentinel |
| Checkov, Trivy config | CI static scanning | Built-in rule packs |

Typical rules: no public databases or buckets, encryption on every volume, required ownership tags, no `latest` image tags, every container has requests and a memory limit, no privileged pods, and deletion of stateful resources needs a human override. Run them in warn mode first, fix existing violations, then enforce.

## In practice

Illustrative backend and module usage. Pin Terraform and provider versions and commit `.terraform.lock.hcl`.

```hcl
terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
  backend "s3" {
    bucket       = "acme-terraform-state"
    key          = "prod/orders/terraform.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

module "orders_db" {
  source                = "git::https://github.com/acme/tf-modules.git//postgres?ref=v3.2.0"
  name                  = "orders-prod"
  instance_class        = "db.r6g.large"
  allocated_storage     = 200
  multi_az              = true
  backup_retention_days = 14
}
```

Inside the module, the database resource with its guard rails:

```hcl
resource "aws_db_instance" "this" {
  identifier                  = var.name
  engine                      = "postgres"
  engine_version              = "16"
  instance_class              = var.instance_class
  allocated_storage           = var.allocated_storage
  multi_az                    = var.multi_az
  backup_retention_period     = var.backup_retention_days
  storage_encrypted           = true
  deletion_protection         = true
  manage_master_user_password = true   # password lives in Secrets Manager
  final_snapshot_identifier   = "${var.name}-final"

  lifecycle {
    prevent_destroy = true
  }
}
```

The pipeline commands:

```bash
terraform fmt -check -recursive
terraform init -input=false
terraform validate
terraform plan -input=false -out=tfplan
terraform show -json tfplan > tfplan.json
conftest test tfplan.json --policy policy/      # fails the PR on violations
terraform apply -input=false tfplan             # on merge, the reviewed plan

# nightly drift detection: exit code 2 means real infra differs from code
terraform plan -detailed-exitcode -input=false -lock=false
```

A Conftest policy over the plan JSON (illustrative, OPA v1 syntax):

```rego
package main

import rego.v1

deny contains msg if {
	some rc in input.resource_changes
	rc.type == "aws_db_instance"
	rc.change.after.publicly_accessible
	msg := sprintf("%s: databases must not be publicly accessible", [rc.address])
}

deny contains msg if {
	some rc in input.resource_changes
	"delete" in rc.change.actions
	rc.type in {"aws_db_instance", "aws_s3_bucket"}
	msg := sprintf("%s: destroying stateful resources needs an override", [rc.address])
}
```

An illustrative Argo CD `Application` that keeps a namespace in sync with a config repo, pruning removed objects and reverting drift:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: orders-api-prod
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/acme/deploy-config.git
    targetRevision: main
    path: apps/orders-api/overlays/prod
  destination:
    server: https://kubernetes.default.svc
    namespace: orders
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

## Pitfalls

- **Local or unlocked state.** Symptom: two applies collide, resources orphaned or duplicated. Fix: remote backend with locking from day one.
- **One giant state.** Symptom: 10-minute plans, everyone blocked on the lock, a typo in DNS code can touch the database. Fix: split by environment and component.
- **Unreviewed replaces.** A renamed resource or a force-new attribute destroys a database. Fix: read plans in PRs, `prevent_destroy`, provider deletion protection, policy checks on delete actions, `moved` blocks for renames.
- **Secrets in state or in `.tfvars` in Git.** Fix: secret managers, managed passwords, restricted and encrypted state.
- **`count` over a list.** Removing one item recreates everything after it. Fix: `for_each` with stable keys.
- **Applying from laptops.** Different provider versions and credentials, no audit trail. Fix: apply only from CI with OIDC credentials; break-glass for emergencies.
- **Ignored drift.** The next unrelated apply silently reverts an emergency fix. Fix: scheduled drift detection, codify or revert promptly.
- **GitOps plus manual hotfixes.** Self-heal reverts the hotfix mid-incident. Fix: hotfix through Git, or pause sync deliberately and record it.

## Interview questions

**What is Terraform state and why does it matter?** It maps resource addresses in code to real object IDs and caches their attributes, so Terraform knows what it manages and can compute diffs. It must be remote, locked, encrypted and access-controlled because it contains secrets and is the source of truth for ownership.

**What happens during `terraform plan`?** Terraform refreshes real attributes through provider APIs, compares them and the previous state with the code, and produces create, update, replace and destroy actions in dependency order. Replace happens when a changed attribute can't be updated in place.

**How do you detect and handle drift?** Schedule `plan -detailed-exitcode` and alert on exit code 2. Then either apply to revert the manual change or update the code to adopt it. Use `ignore_changes` for attributes owned by other controllers, and restrict console write access.

**How do you structure Terraform for multiple environments?** Versioned modules for shared patterns, a separate root configuration and state per environment and component, the same modules with different inputs, plan on PR and apply the saved plan on merge.

**What is GitOps and how does it differ from CI-driven deployment?** Git holds the desired state of the cluster and an in-cluster agent (Argo CD, Flux) pulls and continuously reconciles it, reverting drift. CI-driven deployment pushes changes with credentials held by CI and doesn't correct drift afterwards.

**Terraform vs CloudFormation vs Pulumi?** Terraform: multi-cloud, HCL, self-managed state, huge provider ecosystem. CloudFormation: AWS-managed state, automatic rollback, AWS only. Pulumi: same model as Terraform in general-purpose languages, which helps testing and abstraction but invites over-engineering.

**What would you enforce with policy as code?** No public data stores, encryption everywhere, required tags for ownership and cost, pinned images, resource requests and limits, no privileged containers, and human approval for destroying stateful resources, checked in CI on plans and at Kubernetes admission.

## Further reading

- *Terraform: Up & Running*, Yevgeniy Brikman (O'Reilly, 3rd edition).
- *Infrastructure as Code*, Kief Morris (O'Reilly, 2nd edition).
- Terraform documentation: State, Backends, and the `lifecycle` meta-argument.
- OpenGitOps principles (CNCF OpenGitOps project) and the Argo CD documentation.
- Open Policy Agent documentation: Policy Language (Rego) and Conftest.
