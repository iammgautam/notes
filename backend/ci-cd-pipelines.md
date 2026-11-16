---
title: CI/CD pipelines
section: backend
order: 63
phase: "Phase 9 · DevOps and observability"
summary: Pipeline stages, the test pyramid in CI, caching, build-once artifacts, environments, migrations, trunk-based development and supply-chain security.
---

A pipeline is the only path code takes from a laptop to production, so it decides how fast you ship and how often a bad change reaches users. Continuous integration (CI) means every change is merged to a shared branch often and verified automatically. Continuous delivery means every green build on that branch is deployable, with a human pressing the button. Continuous deployment removes the button. The engineering problems are the same in all three: keep feedback fast, build one immutable artifact, promote it through environments, change the database without downtime, and make sure nobody can slip malicious code into the build.

## Pipeline stages

Order stages by cost: the cheapest check that can fail goes first. A lint error should fail in 30 seconds, not after a 6-minute integration suite.

```text
 developer push
      |
      v
 +----------------+  fail fast: cheapest checks first
 | lint / types   |
 +-------+--------+
         v
 +----------------+     +----------------------------+
 | unit tests     |<----| caches: pip wheels, Docker |
 | (parallel)     |     | layers, test durations     |
 +-------+--------+     +----------------------------+
         v
 +----------------+
 | build image    |---> registry: app@sha256:ab12... (immutable)
 | + SBOM, sign   |
 +-------+--------+
         v
 +----------------+
 | integration /  |  real Postgres + Redis as service containers
 | contract tests |
 +-------+--------+
         v  (main branch only)
 +----------------+     +-----------+     +------------------------+
 | staging:       |---->| approval  |---->| prod: migrate, canary, |
 | migrate, deploy|     | gate      |     | verify, promote        |
 +----------------+     +-----------+     +------------------------+
```

| Stage | Purpose | Typical budget |
|---|---|---|
| Static checks | Formatting, lint, types, migration drift, secret scan | < 1 min |
| Unit tests | Logic in isolation, parallelized | 2–5 min |
| Build | One container image per commit, tagged with the git SHA | 1–3 min with cache |
| Integration and contract | Real database, real broker, API schema checks | 3–10 min |
| Security | Dependency and image CVE scan, SBOM, signature | 1–2 min |
| Deploy to staging | Migrations, rollout, smoke tests | 5 min |
| Deploy to production | Gate, migrations, progressive rollout, automated verification | 10–60 min |

Pull requests run everything up to integration tests. Merges to the main branch run the full path. The whole pre-merge part should finish in about 10 minutes; past 15, developers batch changes and context-switch, which is exactly what CI exists to prevent.

## The test pyramid in CI

The pyramid (Mike Cohn's framing) says: many fast unit tests, fewer integration tests, very few end-to-end tests. In CI the pyramid becomes a scheduling decision: which tests run on which trigger.

| Layer | Django example | When it runs |
|---|---|---|
| Static | `ruff`, `mypy`, `makemigrations --check` | Every push |
| Unit | Pure functions, serializers, service objects with fakes | Every push |
| Integration | ORM queries on real PostgreSQL, Redis locks, Celery tasks run synchronously | Every PR |
| Contract | OpenAPI schema diff, consumer-driven contracts (Pact), Schemathesis fuzzing | Every PR |
| End-to-end | API or browser flows against staging | After each staging deploy |
| Load and soak | k6 or Locust | Nightly or before big releases |

Practical rules:

- **Test against the database you run.** SQLite in tests hides JSONB, partial indexes, `SELECT ... FOR UPDATE SKIP LOCKED` and isolation behavior. Run Postgres as a service container.
- **Parallelize.** `pytest -n auto` (pytest-xdist) within a job, and shard across jobs by recorded durations once one machine is not enough.
- **Quarantine flaky tests, don't retry them blindly.** Automatic retries turn a race condition into a green build. Move the test to a non-blocking job with a ticket and an owner, fix it within days.
- **Keep end-to-end tests few and stable.** They catch wiring mistakes (wrong env var, missing migration), not logic bugs. A dozen smoke flows beat four hundred brittle UI tests.

## Caching

Most pipeline time goes to repeating work that did not change. Cache it, keyed by the inputs that would change the result.

- **Dependency caches.** Key the pip or uv cache by a hash of the lockfile. A changed lockfile is a new key; an unchanged one restores in seconds.
- **Docker layer cache.** Order the Dockerfile so dependency installation comes before copying source code, then export BuildKit cache to the CI cache or to a registry (`cache-to: type=registry`). An unchanged `requirements.txt` skips the most expensive layer.
- **Test metadata.** Store test durations so sharding stays balanced; some setups also skip tests unaffected by a change (test impact analysis), which needs a reliable dependency graph and is worth it mostly in large monorepos.
- **Remote build caches.** Bazel, Pants, Nx and similar tools cache build outputs by content hash across machines.

Two rules keep caches safe. A cache is an optimization, never a source of truth: a job must pass with a cold cache. And never let untrusted code (a fork's pull request) write a cache that a release job later restores, because that is a path to cache poisoning.

## Artifacts

**Build once, promote everywhere.** The artifact that passed tests in staging must be the exact artifact that runs in production, byte for byte. Rebuilding per environment means production runs something that was never tested; a floating base image or a new dependency release can change between builds.

- Build one container image per commit, tag it with the git SHA, and deploy it by **digest** (`app@sha256:...`), which is immutable. Tags like `latest` or `prod` can be moved.
- Keep configuration out of the image. Environment differences come from environment variables, mounted config and secrets at runtime (twelve-factor style), so the same image runs everywhere.
- Attach metadata: an SBOM, a build provenance attestation (which commit, which workflow, which builder) and a signature.
- Keep the last N production artifacts in the registry so rollback is a redeploy, not a rebuild.

CI also produces non-deployable artifacts: test reports, coverage, built docs. Upload those with short retention; they are for humans debugging a run.

## Environments

| Environment | Purpose | Data | Lifetime |
|---|---|---|---|
| Local / compose | Development | Fixtures | Developer's machine |
| Preview | One per pull request, for review and QA | Seeded or synthetic | Deleted when the PR closes |
| Staging | Production-like, last automated gate | Anonymized or synthetic, never raw prod PII | Permanent |
| Production | Real users | Real | Permanent |

Staging is only useful if it matches production in the things that break deploys: same image, same migration path, same infrastructure code, same kind of database. It doesn't need the same size. Protect production with environment rules: required reviewers or a change window, deploys only from the main branch, and secrets scoped to the environment so a pull request job can never read production credentials.

## Database migrations in the pipeline

Migrations are the riskiest part of most deploys because code can be rolled back in a minute and a schema change often cannot.

- **Run migrations as a separate pipeline step**, a Kubernetes Job or a one-off task using the new image, before the application rollout. Don't run `migrate` in every pod's entrypoint: replicas race each other, startup gets slower, and a failing migration turns into a crash loop across the fleet. Django doesn't take a lock around `migrate`, so two concurrent runs can both try to apply the same migration.
- **Every migration must work with the code currently running**, because during a rolling deploy old and new pods share one schema. Use expand and contract: add nullable columns and new tables first, deploy code that uses them, backfill in batches, and drop old columns in a later release. Details in [database migrations]({{ '/backend/database-migrations/' | relative_url }}) and [zero-downtime migrations]({{ '/postgresql/zero-downtime-migrations/' | relative_url }}).
- **Check migrations in CI**: `makemigrations --check` catches models without migrations, a linter (django-migration-linter, Squawk for raw SQL) flags `ALTER TABLE` forms that rewrite or lock tables, and running `sqlmigrate` puts the real SQL in the PR for review.
- **Set `lock_timeout`** on the migration role so a blocked `ALTER` fails in seconds instead of queueing every query behind it.
- **Keep long backfills out of migrations.** Run them as resumable background jobs; a 40-minute data migration inside the deploy blocks every other release.

## Trunk-based development

Trunk-based development means everyone integrates into one branch (`main`) at least daily, with short-lived branches that live hours to a couple of days. Unfinished work ships dark behind [feature flags]({{ '/backend/deployment-strategies/' | relative_url }}) or branch by abstraction instead of living on a long branch.

| | Trunk-based | GitFlow-style long branches |
|---|---|---|
| Branch lifetime | Hours to 2 days | Weeks |
| Merge conflicts | Small and frequent | Large and painful |
| Release | Any green commit on main | Release branch, stabilization phase |
| Unfinished work | Behind flags | On the branch |
| Fits | Services, continuous delivery | Versioned products with several supported releases |

It only works with a fast, reliable pipeline and a rule that main is always green. A merge queue helps at scale: it tests each PR against the latest main plus the PRs ahead of it, so two individually green PRs can't combine into a red main. The DORA research (summarized in *Accelerate*) links trunk-based development with higher deployment frequency and lower change failure rates; its four key metrics, deployment frequency, lead time for changes, change failure rate and time to restore, are the standard way to measure a delivery pipeline.

## Supply-chain security

The pipeline has write access to production and reads code and packages from thousands of strangers, which makes it a prime target. Public incidents show the range: the SolarWinds build compromise (2020), the Codecov uploader script tampering (2021), dependency confusion attacks on internal package names (2021), the xz-utils backdoor (2024) and the tj-actions/changed-files GitHub Action whose tags were repointed to code that dumped CI secrets (2025).

| Threat | Control |
|---|---|
| Malicious or hijacked dependency | Lockfile with hashes (`pip install --require-hashes`, uv lock), private index with explicit sources, automated update PRs reviewed like code |
| Compromised CI action or plugin | Pin third-party actions to a full commit SHA, not a tag; allowlist actions |
| Over-privileged pipeline | Least-privilege `GITHUB_TOKEN` (`permissions:` per job), no write tokens on PR jobs from forks |
| Long-lived cloud keys in CI | OIDC federation: the job exchanges a short-lived signed token for cloud credentials scoped to repo, branch and environment |
| Untrusted PR code with secrets | Never check out and run PR code in `pull_request_target` or `workflow_run` jobs that have secrets |
| Tampered artifact | Sign images (Sigstore cosign, keyless via OIDC), verify signatures at admission time in the cluster |
| Unknown contents | Generate an SBOM (CycloneDX or SPDX via Syft or BuildKit), scan it for CVEs continuously |
| Secrets in code | Secret scanning on push, pre-commit hooks, rotate anything that leaks |

SLSA (Supply-chain Levels for Software Artifacts) gives a maturity ladder for the build itself: provenance exists (L1), is generated and signed by a hosted build platform (L2), and comes from a hardened, isolated builder that tenants can't tamper with (L3).

## In practice

### A GitHub Actions workflow

Illustrative workflow for a Django service: tests on every PR with a real Postgres, an image built once on main with layer cache, SBOM and provenance, then staging and production deploys gated by environments. Action versions are illustrative; in production pin each third-party action to a commit SHA.

{% raw %}
```yaml
name: ci
on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read            # default for every job; widen per job

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true  # a new push cancels the stale run

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_PASSWORD: postgres
          POSTGRES_DB: app
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U postgres"
          --health-interval 5s --health-timeout 5s --health-retries 10
    env:
      DATABASE_URL: postgres://postgres:postgres@localhost:5432/app
      DJANGO_SETTINGS_MODULE: config.settings.test
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: requirements*.txt
      - run: pip install --require-hashes -r requirements-dev.txt
      - run: ruff check . && ruff format --check .
      - run: python manage.py makemigrations --check --dry-run
      - run: pytest -n auto -q --junitxml=report.xml
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: test-report
          path: report.xml
          retention-days: 7

  build:
    needs: test
    if: github.event_name == 'push'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    outputs:
      digest: ${{ steps.push.outputs.digest }}
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - id: push
        uses: docker/build-push-action@v6
        with:
          push: true
          tags: ghcr.io/acme/orders-api:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
          sbom: true
          provenance: true

  deploy-staging:
    needs: build
    runs-on: ubuntu-latest
    environment: staging
    permissions:
      contents: read
      id-token: write       # OIDC token for short-lived cloud credentials
    steps:
      - uses: actions/checkout@v4
      - run: ./deploy/migrate.sh staging "ghcr.io/acme/orders-api@${{ needs.build.outputs.digest }}"
      - run: ./deploy/rollout.sh staging "ghcr.io/acme/orders-api@${{ needs.build.outputs.digest }}"
      - run: ./deploy/smoke.sh https://staging.example.com

  deploy-production:
    needs: [build, deploy-staging]
    runs-on: ubuntu-latest
    environment: production   # required reviewers set in repo settings
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: actions/checkout@v4
      - run: ./deploy/migrate.sh production "ghcr.io/acme/orders-api@${{ needs.build.outputs.digest }}"
      - run: ./deploy/rollout.sh production "ghcr.io/acme/orders-api@${{ needs.build.outputs.digest }}"
```
{% endraw %}

### The migration step

An illustrative `migrate.sh` body: a one-off Kubernetes Job using the new image, waiting for completion before the rollout starts.

```bash
#!/usr/bin/env bash
set -euo pipefail
ENV="$1"; IMAGE="$2"
kubectl -n "orders-$ENV" delete job migrate --ignore-not-found
kubectl -n "orders-$ENV" create job migrate --image="$IMAGE" \
  -- python manage.py migrate --noinput
kubectl -n "orders-$ENV" wait --for=condition=complete job/migrate --timeout=15m
```

Pair it with `python manage.py migrate --check` in the application's startup or readiness logic in non-production environments: it exits non-zero when migrations are unapplied, which catches a rollout that skipped the migration step.

## Pitfalls

- **Slow pipeline.** Symptom: PRs wait 30 minutes, people stack changes. Fix: fail-fast ordering, caching, parallel shards, move e2e and load tests off the PR path.
- **Flaky tests retried until green.** Symptom: intermittent failures nobody trusts. Fix: quarantine with an owner, fix the race or the time dependency (`freezegun`, deterministic seeds).
- **Rebuilding per environment.** Symptom: "it worked in staging". Fix: one image per commit, promoted by digest.
- **Migrations in the container entrypoint.** Symptom: deploys hang or crash-loop when a migration takes a lock. Fix: a dedicated migration job with `lock_timeout`, expand-contract.
- **Mutable action tags and broad tokens.** Symptom: none until the day it matters. Fix: SHA pins, per-job `permissions`, OIDC instead of stored cloud keys.
- **Secrets leaking into logs.** Masking only covers registered secrets; values derived from them (base64-encoded, split) leak. Fix: don't echo secrets, scan logs, rotate on exposure.
- **Untested rollback.** Symptom: the first rollback attempt happens during an incident and fails. Fix: rehearse redeploying the previous digest; keep schema changes backward compatible.

## Interview questions

**What is the difference between continuous integration, delivery and deployment?** CI merges and tests every change on a shared branch. Continuous delivery keeps every green build releasable, with a manual approval to go to production. Continuous deployment ships every green build automatically, which requires strong automated verification and easy rollback.

**How do you keep a pipeline fast?** Cheapest checks first, parallel test shards, dependency and Docker layer caches keyed on lockfiles, a real but small integration suite, and e2e or load tests moved off the PR path. Target about 10 minutes from push to merge-ready.

**Why build once and promote by digest?** The artifact you tested is then the artifact you run. Rebuilding can pick up a new base image or dependency, and tags can be moved; digests can't.

**How do you run database migrations in continuous deployment?** As a separate job using the new image, before the rollout, with `lock_timeout` set. Every migration must be compatible with the code still running, so destructive changes are split with expand and contract across releases, and backfills run as background jobs.

**How would you protect the pipeline from supply-chain attacks?** Hash-pinned lockfiles, SHA-pinned third-party actions, least-privilege tokens per job, OIDC instead of long-lived cloud keys, no secrets for untrusted PR code, signed images with SBOM and provenance, and signature verification at deploy time.

**How do you ship a half-finished feature with trunk-based development?** Merge it behind a feature flag that defaults off, or behind an abstraction where the new implementation is not wired in yet. The code deploys continuously; the release is a flag change.

**A test fails one run in fifty. What do you do?** Quarantine it from the blocking path immediately, file it with an owner, and find the root cause: shared state between tests, ordering, time, randomness or a race. Blind retries hide real concurrency bugs that will show up in production.

**How does CI get AWS access without stored keys?** The CI provider issues a signed OIDC token per job. An IAM role trusts that provider and restricts the token's subject claim to a specific repo, branch or environment, and the job exchanges the token for short-lived credentials.

## Further reading

- *Continuous Delivery*, Jez Humble and David Farley (Addison-Wesley, 2010).
- *Accelerate*, Nicole Forsgren, Jez Humble and Gene Kim (IT Revolution, 2018).
- SLSA specification, Supply-chain Levels for Software Artifacts (OpenSSF).
- GitHub documentation: Security hardening for GitHub Actions; About security hardening with OpenID Connect.
- *Trunk Based Development*, Paul Hammant (trunkbaseddevelopment.com).
