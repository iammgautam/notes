---
title: Common questions and how to answer
section: behavioral
order: 3
summary: Templates for tell me about yourself, why leave, strengths and weaknesses, conflict, failure and why this company, built on the CV.
---

Templates, not scripts. Fill the _Add:_ prompts with real details and practice out loud. Stories referenced below are in the [Story bank]({{ '/behavioral/story-bank/' | relative_url }}).

## Tell me about yourself (60–90 seconds)

Structure: **present → past → why you're here**. Not a life story.

```text
PRESENT (30s)
I'm a backend engineer at Gainfront in Bengaluru, where I've worked since
March 2024 on Django services in a multi-tenant microservices setup. Two
things I'm proudest of: I built a PostgreSQL-native distributed task
executor, with queuing, fan-out/join and idempotent execution and no
Celery or Redis, and I took a set of analytics queries from 239 seconds
to under a second with rollups and indexing. I've also built our AI/RAG
platform: multi-model LLM routing, pgvector, tool orchestration and
multi-step agents.

PAST (20s)
Before that I spent two years at MarsDevs in Pune, building Django modules
from scratch, cutting SQL time from 15-25 seconds to 1-2 by removing
N+1 queries, and modernizing a ten-year-old Django codebase. I did my
B.Tech in Computer Science at Sikkim Manipal Institute of Technology.

WHY HERE (15s)
_Add: what you want next, tied to this role (e.g. the scale, the domain,
the team's problems) and one specific thing about this company._
```

> **Interview tip:** Pick the two highlights that match the job description and lead with those. For an AI-heavy role, lead with the RAG platform; for a data/infra role, lead with the task executor and the 239s → 0.9s work.

## Why are you leaving?

Rules:

- Forward-looking: what you're moving **toward**, not away from.
- Never criticize the current employer, team or manager.
- Keep it to 2–3 sentences.

Template:

```text
I've learned a lot at Gainfront, especially <one concrete thing>.
I'm looking for <what you want: scale / domain / type of problems /
team size>, and this role offers <specific match>.
```

_Add: your real reason, phrased as the thing you want next._

## Strengths

Pick 2–3 and back each with a story. Candidates supported by the CV:

| Strength | Evidence |
|---|---|
| Database performance | 239s → 0.9s; 56+ indexes, rollups; 500+ → 5–10 queries |
| Building infrastructure from first principles | Postgres-native task executor without Celery/Redis |
| End-to-end ownership | Owned features with clients; production issues via Sentry |
| Working with legacy code | ~10-year-old Django codebase; templates → Vue.js + REST |
| Communicating with clients | Pushed back on infeasible approaches with alternatives |

## Weaknesses

Formula: **real weakness → concrete impact → what you're doing about it → evidence it's improving.**

- Pick something real but not core to the role.
- Avoid fake weaknesses ("I'm a perfectionist", "I work too hard").
- Show the fix in action, not just intention.

```text
_Add: weakness._ It showed up when _Add: situation_. Since then I
_Add: specific habit or practice_, and _Add: evidence it's working_.
```

## Tell me about a conflict

Use [Story 4: pushing back on a client approach]({{ '/behavioral/story-bank/' | relative_url }}).

What they're checking: do you disagree respectfully, with data, and commit to the outcome?

- Describe the disagreement neutrally. The other side had a reason.
- Show you understood their goal before arguing the approach.
- Bring alternatives, not just "no".
- End with the outcome and the relationship afterwards.

## Tell me about a failure

What they're checking: ownership and learning, not perfection.

- Pick a real failure where **you** contributed to the cause. A failure caused only by others is a conflict story.
- Spend ~30% on what happened, ~70% on what you did and changed.
- End with a habit you still use.

Candidate: [Story 5: production issue via Sentry]({{ '/behavioral/story-bank/' | relative_url }}). _Add: a failure where you owned part of the cause._

## Why this company?

Research for 15 minutes before the call. Answer with three specifics:

```text
1. Product/problem: _Add: what they build and why it interests you._
2. Tech/scale match: _Add: how your work (Postgres, Django, task queues,
   RAG) maps to their stack or challenges._
3. Team/culture signal: _Add: something from their blog, talks, or the
   interview so far._
```

Generic answers ("great culture", "fast-growing") score nothing.

## Other quick ones

| Question | Approach |
|---|---|
| Where do you see yourself in 5 years? | Growth in depth/scope that this role supports. _Add: your real direction._ |
| Describe a time you dealt with ambiguity | Story 1 or 3: how you turned an open problem into a design. |
| Tell me about something you're proud of | Story 1 or 2; lead with the number. |
| How do you handle tight deadlines? | Scope down, communicate early, protect quality on what ships. Back with a story. |
| What do you do when you disagree with your manager? | Raise it privately with data, propose an alternative, commit once decided. |

## Pitfalls

- Going past 90 seconds on "tell me about yourself".
- Reciting the CV in order instead of choosing highlights.
- Negativity about current or past employers.
- Weakness answers with no fix.
- Same story for every question.
