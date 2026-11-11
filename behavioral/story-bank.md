---
title: Story bank
section: behavioral
order: 2
summary: Eight STAR story outlines drawn from the Gainfront and MarsDevs work, tagged by question type, with fill-in prompts for details to add.
---

Outlines only. The numbers come from the CV; anything in _italics_ starting with "Add:" is a gap to fill from memory before the interview. Don't fill gaps with guesses: a follow-up question will find them.

## Coverage map

| # | Story | Question types |
|---|---|---|
| 1 | Postgres task executor | ownership, ambiguity |
| 2 | Analytics 239s → 0.9s | performance win, ownership |
| 3 | AI/RAG platform | ownership, ambiguity |
| 4 | Pushing back on a client approach | conflict/pushback, client communication |
| 5 | Production issue via Sentry | ownership, failure/learning |
| 6 | N+1 removal at MarsDevs | performance win, failure/learning |
| 7 | Modernizing legacy Django | legacy modernization, ambiguity |
| 8 | Django modules from scratch | ownership, ambiguity, client communication |

## 1. Postgres-native distributed task executor (Gainfront)

**Answers:** ownership, ambiguity, technical decision, "most complex thing you built".

- **S:** Django services in a multi-tenant microservices setup needed background task execution. _Add: what triggered the need (which workloads, what was failing or missing)._
- **T:** Build a distributed task executor using PostgreSQL only, without Celery or Redis.
- **A:**
  - Designed queuing, task isolation, fan-out/join and idempotent execution.
  - _Add: why Postgres-only (operational simplicity? transactional enqueue? existing infra?)._
  - _Add: how workers claim tasks (e.g. row locking approach you actually used)._
  - _Add: how idempotency was guaranteed and how failures/retries are handled._
  - _Add: who you had to convince and how._
- **R:** _Add: what it runs today (task types, volume, reliability), and what it replaced or avoided._
- **Learning:** _Add: one thing you'd design differently._

## 2. Analytics query optimization: 239s → 0.9s (Gainfront)

**Answers:** performance win, ownership, "tell me about a hard technical problem".

- **S:** Postgres analytics were slow. _Add: which screen/report, who was affected, how they noticed._
- **T:** Make it fast enough for interactive use.
- **A:**
  - Measured first. _Add: how you found the slow parts (EXPLAIN ANALYZE, pg_stat_statements, logs?)._
  - Precomputed rollups instead of aggregating raw rows per request.
  - Added 56+ indexes, including partial and composite indexes.
  - Used JSONB and full-text search where they fit. _Add: what each one solved._
  - _Add: how you validated correctness of rollups vs raw data._
- **R:** **239s → 0.9s.** _Add: user/client effect (e.g. report usable, complaints stopped)._
- **Learning:** _Add: e.g. trade-off of maintaining rollups, or index write cost._

## 3. AI/RAG platform (Gainfront)

**Answers:** ambiguity, ownership, learning something new fast.

- **S:** _Add: business reason for the AI/RAG platform and who uses it._
- **T:** Build the platform.
- **A:**
  - Multi-model LLM routing.
  - Embeddings stored and searched with pgvector.
  - Tool orchestration, structured outputs, multi-step agents.
  - _Add: one hard decision (e.g. how routing chooses a model, how you handled unreliable outputs)._
- **R:** _Add: adoption, a capability it unlocked, or a measurable outcome._
- **Learning:** _Add._

## 4. Pushing back on an infeasible client approach (Gainfront)

**Answers:** conflict/pushback, client communication, disagree and commit.

- **S:** Owning a feature end-to-end with a client. _Add: what the client originally asked for._
- **T:** Deliver what the client needs, not necessarily what they asked for.
- **A:**
  - _Add: why the approach was infeasible (cost, performance, data model, timeline)._
  - Proposed alternatives. _Add: the alternatives and the trade-offs you laid out._
  - _Add: how you communicated (demo, written comparison, call) and how the client reacted._
- **R:** _Add: what was built and how the client felt about it afterwards._
- **Learning:** _Add: e.g. bring alternatives, not just "no"._

## 5. Resolving a production issue via Sentry (Gainfront)

**Answers:** ownership, failure/learning, working under pressure.

- **S:** _Add: the specific production issue, how it surfaced in Sentry, impact._
- **T:** Find and fix it.
- **A:** _Add: how you scoped it (tenant? release?), the root cause, the fix._
- **R:** _Add: resolution time, and the prevention you added (test, alert, guard)._
- **Learning:** _Add._

> **Interview tip:** For a failure question, pick a story where you own part of the cause, not just the fix.

## 6. N+1 removal and SQL speedup (MarsDevs)

**Answers:** performance win, failure/learning (if you inherited or introduced the issue).

- **S:** _Add: which pages/endpoints were slow and who noticed._
- **T:** Bring response times down.
- **A:**
  - Found N+1 query patterns. _Add: how you found them (debug toolbar, logging)._
  - Removed them. _Add: select_related/prefetch_related/annotations as applicable._
- **R:** Queries per request **500+ → ~5–10**; SQL time **~15–25s → ~1–2s**.
- **Learning:** _Add: e.g. now checking query counts before shipping list views._

## 7. Modernizing legacy Django (both companies)

**Answers:** legacy modernization, ambiguity, incremental change without breaking things.

- **MarsDevs:** modernized a ~10-year-old Django codebase.
- **Gainfront:** moved legacy Django templates to Vue.js + REST.
- **S:** _Add: the state of the code (versions, pain points)._
- **T:** _Add: your goal and constraints (no downtime? features still shipping?)._
- **A:** _Add: how you did it incrementally, how you avoided regressions._
- **R:** _Add: what got easier (shipping speed, bugs, onboarding)._
- **Learning:** _Add._

## 8. Building Django modules from scratch (MarsDevs)

**Answers:** ownership, ambiguity, client communication.

- **S:** Built 3–4 Django modules from scratch. _Add: what the modules did._
- **T:** _Add: requirements clarity at the start._
- **A:** _Add: how you clarified requirements, designed the data model, used Celery workflows for emails/reminders, shipped with Vue.js/TypeScript._
- **R:** _Add: delivery outcome._
- **Learning:** _Add._

## Before the interview

- Fill every _Add:_ for the top 4 stories at least.
- Say each one out loud in under 3 minutes.
- Know the numbers cold: **239s → 0.9s**, **56+ indexes**, **500+ → 5–10 queries**, **15–25s → 1–2s**, **3–4 modules**, **~10-year-old codebase**.
