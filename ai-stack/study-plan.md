---
title: Study plan for the Python AI engineer role
section: ai-stack
order: 1
phase: "Start here"
summary: Four weeks from Django and custom RAG to FastAPI, Pandas, LangChain and LangGraph, with the job description mapped to notes, builds and stories.
---

This plan takes a backend engineer who knows Django and has shipped a custom RAG and agent platform to the point where they can build and defend a finance AI service on FastAPI, Pandas, LangChain and LangGraph. After four weeks you should have one public repository that covers every must-have in the job description, three or four interview stories with numbers, and clear answers for the gaps (no LangChain in production, little finance domain knowledge).

## Prerequisites

None beyond your current experience. If any LLM basics feel shaky, refresh [RAG architecture]({{ '/llm/rag-architecture/' | relative_url }}) and [Tool calling]({{ '/llm/tool-calling/' | relative_url }}) first.

## How to use this plan

- **Budget.** About 2–3 hours on weekdays and 5–6 hours on weekend days. Each day is one note: read it, run its code, and do at least the first two practice tasks.
- **One repository.** Build everything in a single repo, say `finance-ai-platform`, with `api/`, `data/`, `rag/` and `agent/` packages. Each week adds a package, and the capstone wires them together. Interviewers would rather see one coherent system than four toy demos.
- **Write as you go.** Keep a `DECISIONS.md` in the repo with one paragraph per choice (why pgvector, why a semaphore of 5, why a function-scoped session). Those paragraphs become your interview answers.

```text
 Week 1           Week 2              Week 3              Week 4
 FastAPI    -->   Pandas +      -->   LangChain    -->    LangGraph +
                  LangChain core      RAG + evals         capstone
 ledger API       reconciliation      policy Q&A with     finance agent,
 (auth, SSE)      job + LLM notes     citations, evals    mock interviews
```

## The job description mapped to notes

| JD line | Notes | Proof artifact in your repo |
|---|---|---|
| FastAPI REST APIs | [Fundamentals]({{ '/ai-stack/fastapi-fundamentals/' | relative_url }}), [DI]({{ '/ai-stack/fastapi-dependency-injection/' | relative_url }}), [async]({{ '/ai-stack/fastapi-async-and-performance/' | relative_url }}), [databases]({{ '/ai-stack/fastapi-databases/' | relative_url }}), [testing]({{ '/ai-stack/fastapi-testing-and-production/' | relative_url }}) | Ledger API with tests and OpenAPI |
| Core Python, Git, testing | [Async]({{ '/ai-stack/fastapi-async-and-performance/' | relative_url }}), [testing and production]({{ '/ai-stack/fastapi-testing-and-production/' | relative_url }}) | Typed code, pytest in CI, clean history |
| Pandas for structured and unstructured data | [Pandas fundamentals]({{ '/ai-stack/pandas-fundamentals/' | relative_url }}) through [Pandas for AI and finance]({{ '/ai-stack/pandas-for-ai-and-finance/' | relative_url }}) | Bank-to-GL reconciliation job |
| SQL | [Databases]({{ '/ai-stack/fastapi-databases/' | relative_url }}), [groupby, merge, reshape]({{ '/ai-stack/pandas-groupby-merge-reshape/' | relative_url }}) | Trial balance in SQL and in pandas, both tested |
| LLM engineering across providers | [Models, prompts, structured output]({{ '/ai-stack/langchain-models-prompts-structured-output/' | relative_url }}), [Structured outputs]({{ '/llm/structured-outputs/' | relative_url }}) | One config switch moves between OpenAI, Azure OpenAI, Anthropic and a local model |
| LangChain and LangGraph agents, tool orchestration | [Tools and agents]({{ '/ai-stack/langchain-tools-and-agents/' | relative_url }}), [LangGraph core]({{ '/ai-stack/langgraph-core-concepts/' | relative_url }}), [tool-calling agents]({{ '/ai-stack/langgraph-tool-calling-agents/' | relative_url }}) | Agent graph with tools, checkpointer and an approval step |
| RAG: embeddings, vector DBs, retrieval tuning, grounding | [LangChain RAG]({{ '/ai-stack/langchain-rag/' | relative_url }}), [Hybrid search and reranking]({{ '/llm/hybrid-search-and-reranking/' | relative_url }}), [pgvector]({{ '/postgresql/pgvector/' | relative_url }}) | Policy Q&A with citations, plus recall@k before and after tuning |
| Evaluation, hallucination reduction | [Observability and evals]({{ '/ai-stack/langchain-observability-and-evals/' | relative_url }}), [RAG evaluation]({{ '/llm/rag-evaluation/' | relative_url }}) | Eval set of 30+ questions with a CI gate on faithfulness |
| Latency, cost, reliability | [Async]({{ '/ai-stack/fastapi-async-and-performance/' | relative_url }}), [streaming]({{ '/ai-stack/fastapi-streaming-llm-apis/' | relative_url }}), [Latency and cost]({{ '/llm/latency-and-cost/' | relative_url }}) | Table of p95 latency and cost per question |
| Security, access control, regulated data | [Security]({{ '/ai-stack/fastapi-security/' | relative_url }}), [Security and guardrails]({{ '/llm/security-and-guardrails/' | relative_url }}) | Tenant-scoped retrieval, role checks on tools, audit log |
| Finance domain (GL, reconciliation, reporting) | [Pandas for AI and finance]({{ '/ai-stack/pandas-for-ai-and-finance/' | relative_url }}), [AI system design for finance data]({{ '/ai-stack/finance-ai-system-design/' | relative_url }}) | Reconciliation exceptions plus LLM variance commentary |
| Nice to have: Databricks, MLflow, PySpark | [Pandas performance]({{ '/ai-stack/pandas-performance/' | relative_url }}), [system design]({{ '/ai-stack/finance-ai-system-design/' | relative_url }}) | A page on how the reconciliation job maps to PySpark and Delta tables |
| Nice to have: Docker, CI/CD, cloud | [Testing and production]({{ '/ai-stack/fastapi-testing-and-production/' | relative_url }}), [LangGraph deployment]({{ '/ai-stack/langgraph-streaming-and-deployment/' | relative_url }}) | Dockerfile, CI workflow, one-command local stack |

## Week 1: FastAPI

You already know HTTP, ORMs and auth, so this week is about mapping DRF habits onto FastAPI and getting async right.

| Day | Read | Do |
|---|---|---|
| 1 | [FastAPI fundamentals]({{ '/ai-stack/fastapi-fundamentals/' | relative_url }}) | Invoices CRUD with separate in and out models |
| 2 | [Dependency injection]({{ '/ai-stack/fastapi-dependency-injection/' | relative_url }}) | Settings, session and tenant dependencies, plus overrides in tests |
| 3 | [Async and performance]({{ '/ai-stack/fastapi-async-and-performance/' | relative_url }}) | Reproduce the blocking demo and add the loop-lag monitor |
| 4 | [Databases with FastAPI and SQLAlchemy 2]({{ '/ai-stack/fastapi-databases/' | relative_url }}) | SQLAlchemy 2 models, Alembic, GL entries on PostgreSQL |
| 5 | [Security in FastAPI]({{ '/ai-stack/fastapi-security/' | relative_url }}) | JWT auth, roles, tenant isolation tests |
| 6 | [Streaming LLM APIs]({{ '/ai-stack/fastapi-streaming-llm-apis/' | relative_url }}) | SSE endpoint streaming a fake model |
| 7 | [Testing and production]({{ '/ai-stack/fastapi-testing-and-production/' | relative_url }}) | Dockerfile and CI, then the week's build |

**Build: the ledger API.** Invoices and journal entries on PostgreSQL, JWT auth, a tenant dependency, an `/ask` endpoint that streams over SSE, pytest with dependency overrides, and CI running on every push. Done means a fresh clone runs `docker compose up`, then `pytest`, and everything is green.

## Week 2: Pandas, then LangChain core

| Day | Read | Do |
|---|---|---|
| 8 | [Pandas fundamentals]({{ '/ai-stack/pandas-fundamentals/' | relative_url }}) | Load a GL export with explicit dtypes |
| 9 | [Cleaning and transformation]({{ '/ai-stack/pandas-cleaning-and-transformation/' | relative_url }}) | Normalize vendors, dates and amounts |
| 10 | [Groupby, merge, reshape]({{ '/ai-stack/pandas-groupby-merge-reshape/' | relative_url }}) | Trial balance and vendor spend pivots |
| 11 | [Pandas performance]({{ '/ai-stack/pandas-performance/' | relative_url }}) | Profile a 1M-row job and use Arrow dtypes |
| 12 | [Pandas for AI and finance]({{ '/ai-stack/pandas-for-ai-and-finance/' | relative_url }}) | Validate with pandera, turn rows into LLM context |
| 13 | [LangChain architecture]({{ '/ai-stack/langchain-architecture/' | relative_url }}), [models and structured output]({{ '/ai-stack/langchain-models-prompts-structured-output/' | relative_url }}) | Typed output from fake and real models |
| 14 | Build day | Reconciliation job |

**Build: the reconciliation job.** Match a bank statement CSV against GL cash entries (exact match, then amount-and-date tolerance), output matched, unmatched and suspicious rows, validate the inputs with pandera, and add one LLM step that writes a structured explanation for each unmatched item. Expose it as `POST /reconciliations`, which returns 202 and a job ID. Done means a test with known exceptions passes, and the LLM step runs against a fake model in CI.

## Week 3: LangChain, RAG and evaluation

| Day | Read | Do |
|---|---|---|
| 15 | [LCEL and runnables]({{ '/ai-stack/langchain-lcel-and-runnables/' | relative_url }}) | A chain with fallbacks and retries |
| 16 | [Tools and agents]({{ '/ai-stack/langchain-tools-and-agents/' | relative_url }}) | Ledger lookup tools, `create_agent` |
| 17 | [LangChain RAG]({{ '/ai-stack/langchain-rag/' | relative_url }}) | Ingest policy PDFs, retrieve with citations |
| 18 | [Chat history]({{ '/ai-stack/langchain-chat-history/' | relative_url }}) | Multi-turn Q&A per user and tenant |
| 19 | [Observability and evals]({{ '/ai-stack/langchain-observability-and-evals/' | relative_url }}) | Tracing and a 30-question eval set |
| 20–21 | Build days | Tune and measure |

**Build: policy Q&A.** Take expense, travel and revenue recognition policies (public templates are fine), chunk them, embed them into pgvector, and answer with citations through FastAPI. Tune chunk size, k, hybrid search and reranking against your eval set, and record recall@k and faithfulness before and after. Done means a results table in the README and a CI job that fails when faithfulness drops below the threshold you set.

## Week 4: LangGraph, capstone, interview prep

| Day | Read | Do |
|---|---|---|
| 22 | [Core concepts]({{ '/ai-stack/langgraph-core-concepts/' | relative_url }}), [control flow]({{ '/ai-stack/langgraph-control-flow/' | relative_url }}) | State, nodes, conditional edges |
| 23 | [Tool-calling agents]({{ '/ai-stack/langgraph-tool-calling-agents/' | relative_url }}) | Rebuild the week 3 agent as a graph |
| 24 | [Persistence]({{ '/ai-stack/langgraph-persistence-and-memory/' | relative_url }}), [human in the loop]({{ '/ai-stack/langgraph-human-in-the-loop/' | relative_url }}) | Checkpointer, an approval interrupt for journal entries |
| 25 | [Multi-agent]({{ '/ai-stack/langgraph-multi-agent/' | relative_url }}), [streaming and deployment]({{ '/ai-stack/langgraph-streaming-and-deployment/' | relative_url }}) | Stream graph events through the SSE endpoint |
| 26–27 | [Capstone]({{ '/ai-stack/capstone-finance-rag-agent/' | relative_url }}) | Wire the API, data, RAG and agent together |
| 28 | [Question bank]({{ '/ai-stack/interview-question-bank/' | relative_url }}), [system design]({{ '/ai-stack/finance-ai-system-design/' | relative_url }}) | Two timed mock interviews |

**Build: the finance agent.** One graph that answers policy questions through RAG, queries the ledger through tools, proposes adjusting journal entries, and pauses for human approval before posting them, with every step traced and audited. Done means you can demo it in five minutes and draw its architecture from memory.

**Three-week version.** Merge days 1–2 and days 8–9, skip pandas performance and multi-agent on the first pass, and make the capstone a one-day build. Do not cut the eval set: it is the strongest signal you can show.

## Talking about your existing experience

**Lead with what transfers, then show the new stack.** Instead of "I haven't used FastAPI in production", say: "My production APIs were in DRF. I rebuilt the same patterns in FastAPI (tenant isolation through dependencies, async LLM calls under a semaphore, SSE streaming), and here is the repo." The repo turns a gap into evidence.

**Use your custom platform as an advantage.** You built the primitives that LangChain and LangGraph package: provider routing, the tool-calling loop, retrieval, conversation memory. Map them out loud:

| What you built | Framework equivalent |
|---|---|
| Provider router | `init_chat_model`, `with_fallbacks` |
| Tool registry and execution loop | `@tool`, `create_agent`, `ToolNode` |
| Agent state machine | LangGraph `StateGraph` |
| Conversation store | LangGraph checkpointer |
| Retrieval pipeline | Retriever, vector store, reranker |

Then say where each approach wins: frameworks give tracing, streaming and checkpointing for free, while custom code gives control over prompts, retries and cost. Interviewers trust a candidate who knows what the abstraction hides.

**Prepare three stories with numbers.** One on latency or cost (before and after), one on grounding or hallucination (what failed, how you measured it, what fixed it), and one on access control or multi-tenancy. Use situation, action, result and what you would change. Leave out client names, internal service names and any open weaknesses of past products.

**Close the finance gap with vocabulary and one artifact.** Learn the terms you will hear: general ledger, journal entry, debit and credit, trial balance, accruals, accounts payable and receivable, bank and intercompany reconciliation, month-end close, variance analysis, audit trail, segregation of duties, SOX controls. The reconciliation job is your proof that you can work with them.

## Common mistakes

- **Reading without building.** Notes you haven't coded fade in a week. Every day ends with a commit.
- **Learning old APIs.** `initialize_agent`, `AgentExecutor`, `on_event` and pandas 2 habits show up in older tutorials. The notes here target LangChain 1.x, LangGraph 1.x, pandas 3 and current FastAPI.
- **Demos with no numbers.** "It works" is weak; "recall@5 went from 0.62 to 0.81 with hybrid search" is strong. Measure everything you tune.
- **Skipping security.** Regulated finance roles probe access control and audit trails. Have tenant isolation and approval steps in the capstone.
- **Over-scoping the capstone.** One graph, three tools, one approval step. Polish beats breadth.

## Pre-interview checklist

- The repo is public, the README has an architecture diagram, setup takes one command, and CI is green.
- You can explain `async def` vs `def`, dependency `yield` scope, and why the HTTP client lives in the lifespan.
- You can write a groupby, merge and pivot from memory and explain copy-on-write in pandas 3.
- You can sketch LCEL composition, `create_agent`, and a LangGraph `StateGraph` with a checkpointer and an interrupt.
- You can state your retrieval and faithfulness numbers and how you got them.
- You have three stories rehearsed out loud, two minutes each.
- You have drawn the [AI system design for finance data]({{ '/ai-stack/finance-ai-system-design/' | relative_url }}) twice on a blank page.
- You have gone through the [interview question bank]({{ '/ai-stack/interview-question-bank/' | relative_url }}) and marked weak answers to revisit.
- You know the finance terms above well enough to use them in a sentence.
- You have questions ready for them: data sources, evaluation practice, how the platform handles PII and model approvals.

## Further reading

- Every note in this section, in order. The section index lists them by phase.
- Deeper background in the LLM section: [Agents]({{ '/llm/agents/' | relative_url }}), [Agent frameworks]({{ '/llm/agent-frameworks/' | relative_url }}), [LLM evaluation]({{ '/llm/llm-evaluation/' | relative_url }}), [Observability and tracing]({{ '/llm/observability-and-tracing/' | relative_url }}).
- Official docs to keep open: FastAPI Tutorial - User Guide, pandas User Guide, LangChain docs (Agents, Retrieval), LangGraph docs (Graph API, Persistence, Interrupts).
