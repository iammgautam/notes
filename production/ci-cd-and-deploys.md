---
title: CI/CD and deploy strategies
section: production
order: 7
summary: Pipeline stages that catch problems early, running Django migrations safely during deploys, rolling vs blue-green vs canary, and fast rollbacks.
---

The goal of CI/CD is to make deploys boring: small, frequent, automated and reversible. Most outages are caused by changes, so the deploy pipeline is where you get the most leverage. The two hard parts are not the YAML. They are database migrations that must work with two versions of the code at once, and a rollback path you've actually tested.

## How it works

### Pipeline stages

```text
push / PR
  -> lint + type check          (seconds)  ruff, mypy
  -> unit tests                 (1-3 min)  pytest, no network
  -> integration tests          (3-8 min)  real Postgres in a service container
  -> migration checks                      makemigrations --check, lint SQL
  -> build image, tag with SHA             push to registry
merge to main
  -> deploy to staging                     run migrations, smoke tests
  -> deploy to production                  canary or rolling, auto-verify
  -> post-deploy checks                    error rate, p95, Sentry new issues
```

Order by cost: the cheapest checks that catch the most bugs run first, so a typo fails in 20 seconds, not after a 10-minute test suite.

**Build once, deploy many.** The image that passed tests is the same artifact promoted to staging and production, identified by git SHA. Rebuilding per environment means production runs something you didn't test.

### Deploy strategies

| Strategy | How | Rollback speed | Cost | Risk |
|---|---|---|---|---|
| Recreate | Stop old, start new | Slow (redeploy) | Low | Downtime |
| Rolling | Replace instances N at a time | Medium (roll again) | Low | Mixed versions during rollout |
| Blue-green | Full new env, switch traffic at once | Instant (switch back) | 2x capacity briefly | Whole fleet hit at once |
| Canary | Send 1–5% of traffic to new version, watch, ramp | Fast (route away) | Low–medium | Needs good metrics |

- **Rolling** is the default on Kubernetes and most PaaS. Both versions serve traffic at the same time, so the code and schema must be compatible across one version.
- **Blue-green** gives instant cutover and rollback at the router, but the database is shared. A migration applied for green is live for blue too.
- **Canary** limits blast radius. You compare the canary's error rate and latency against the baseline and promote automatically if they stay within thresholds. Only useful if your metrics can separate canary from stable (tag by version).

Background workers deploy too. During a rolling deploy, old workers may pick up tasks enqueued by new code (and vice versa). Task payloads need the same compatibility discipline as the schema.

### Migrations in deploys: expand and contract

During any non-recreate deploy, old and new code run against the same schema. So every migration must be compatible with **both** the previous and the next version of the code.

Renaming `customer.name` to `customer.full_name` safely takes several deploys:

```text
Deploy 1 (expand)    add full_name NULL; code writes both, reads name
Backfill             copy name -> full_name in batches
Deploy 2             code reads full_name, still writes both
Deploy 3 (contract)  code stops using name
Deploy 4             drop column name
```

Rules for Postgres + Django:

- **Add columns as nullable** or with a constant default (Postgres 11+ adds those without rewriting the table).
- **Create indexes concurrently** (`AddIndexConcurrently` from `django.contrib.postgres.operations`, with `atomic = False` on the migration). A plain `CREATE INDEX` blocks writes for the whole build.
- **Add `NOT NULL` and foreign keys in two steps**: add a `CHECK ... NOT VALID` or `NOT VALID` FK, then `VALIDATE CONSTRAINT` in a later migration (takes a weaker lock).
- **Never drop or rename a column the running code reads.** Remove it from the model first (Django's `SeparateDatabaseAndState`), drop it a release later.
- **Set `lock_timeout`** so a migration waiting behind a long transaction fails fast instead of queuing all traffic behind it:

```sql
SET lock_timeout = '5s';
SET statement_timeout = '15min';
```

- **Backfills are not migrations.** Run large data updates as a batched job (10k rows per transaction), not inside a migration that holds a lock for 40 minutes.

Run migrations as a **single pre-deploy job**, not in every container's entrypoint.

## In practice

### GitHub Actions example

{% raw %}
```yaml
name: ci
on:
  pull_request:
  push:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: pgvector/pgvector:pg16
        env:
          POSTGRES_PASSWORD: postgres
        ports: ["5432:5432"]
        options: >-
          --health-cmd pg_isready --health-interval 5s --health-retries 10
    env:
      DATABASE_URL: postgres://postgres:postgres@localhost:5432/postgres
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
      - run: pip install -r requirements-dev.txt
      - run: ruff check . && mypy .
      - run: python manage.py makemigrations --check --dry-run
      - run: pytest -q -n auto

  build:
    needs: test
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/build-push-action@v6
        with:
          push: true
          build-args: GIT_SHA=${{ github.sha }}
          tags: ghcr.io/${{ github.repository }}:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```
{% endraw %}

Deployment then promotes `ghcr.io/org/app:<sha>` through environments, runs `python manage.py migrate` as a one-off job, rolls out, and runs smoke tests against a few critical endpoints.

### Post-deploy verification

Automate the "is it OK?" check rather than eyeballing dashboards:

- Error rate and p95 latency for the new version vs the previous 30 minutes.
- New Sentry issues tagged with the new `release`.
- Queue depth and task failure rate for workers.
- If any threshold is breached within the first 10–15 minutes, roll back automatically or page.

### Rollbacks

- **Code rollback** = redeploy the previous image SHA. It should be one command and take minutes. Practice it.
- **Schema rollback** is usually *not* running reverse migrations in production. Reverse migrations that drop columns lose data written since. Expand/contract makes this unnecessary: the old code already works with the new schema.
- **Roll forward** only when the fix is tiny, obvious and faster than a rollback. Under pressure, people overestimate this.
- **Feature flags** decouple deploy from release: deploy dark, enable per tenant, turn off without a deploy.

## Pitfalls

- **Migration locks.** `ALTER TABLE` waits for an `ACCESS EXCLUSIVE` lock behind a long-running query, and every new query queues behind it. Symptom: the whole app stalls during deploy. Fix: `lock_timeout` and retry.
- **Non-backward-compatible migrations** that break old pods still serving during a rolling deploy. Symptom: a burst of `column does not exist` errors that clears once the rollout finishes.
- **Flaky tests** that teach everyone to click "re-run". Quarantine and fix them; a red build must mean something.
- **Long pipelines.** A 40-minute pipeline pushes people to batch changes into larger, riskier deploys. Parallelize tests, cache dependencies.
- **Deploying on Friday evening** isn't a moral failing; deploying without a rollback plan and someone watching is.
- **Environment drift.** Staging with 1,000 rows doesn't catch the migration that takes 20 minutes on a 50M-row production table. Test heavy migrations against a production-sized copy.

## Rules of thumb

- Build once, promote the same image by SHA.
- Every migration compatible with N-1 and N+1 code.
- Indexes concurrently, constraints `NOT VALID` then validated, `lock_timeout` always.
- Migrations as one pre-deploy job; backfills as batched jobs.
- Rollback = previous image, tested regularly.
- Small, frequent deploys over big batched ones.

## Interview angle

- **"How do you rename a column with zero downtime?"** Expand/contract: add the new column, dual-write, backfill, switch reads, stop writing old, drop it in a later release.
- **"Blue-green vs canary?"** Blue-green switches all traffic at once with instant rollback; canary shifts a small slice first and promotes based on metrics, limiting blast radius. Both share the database, so schema changes still need backward compatibility.

Related: [Docker essentials]({{ '/production/docker-essentials/' | relative_url }}), [Feature flags]({{ '/production/feature-flags/' | relative_url }}).
