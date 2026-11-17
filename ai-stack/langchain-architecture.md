---
title: LangChain architecture and packages
section: ai-stack
order: 14
phase: "Phase 3 · LangChain"
summary: Which LangChain package does what in 1.x, what moved to LangGraph and langchain-classic, and when to pick LangChain, LangGraph or a plain SDK.
---
After this note you can draw the LangChain 1.x package map from memory, say what changed from the 0.x tutorials most people learned on, and explain where LangChain, LangGraph and a plain provider SDK each belong in a finance AI service. Interviewers for "LangChain + LangGraph" roles open with exactly this: why the framework, which part of it, and what you would not use it for.

## Prerequisites

Comfortable Python and Pydantic, plus [Pandas for AI and finance]({{ '/ai-stack/pandas-for-ai-and-finance/' | relative_url }}). For the conceptual comparison of frameworks, see [agent frameworks]({{ '/llm/agent-frameworks/' | relative_url }}).

## The mental model: one protocol, many packages

LangChain is not one library. It is a small core of interfaces plus a ring of packages that implement them. Everything that can be called (a prompt, a model, a parser, a retriever, an agent) implements the same `Runnable` interface: `invoke`, `batch`, `stream` and their async twins. Integrations are thin adapters that make a provider look like one of those interfaces.

```text
  your code: FastAPI route, Celery task, notebook
        |
        v
  langchain 1.x ......... create_agent, middleware, init_chat_model
        |                 (an agent is a compiled LangGraph graph)
        v
  langgraph ............. StateGraph, checkpointers, interrupts
        |
        v
  langchain-core ........ Runnable, messages, prompts, tools, parsers
        ^
        |  implement BaseChatModel / Embeddings / VectorStore
        |
  partner packages ...... langchain-openai, langchain-anthropic,
                          langchain-postgres, ...
  langchain-community ... long tail of third-party integrations
  langchain-text-splitters  chunking for RAG ingestion
  langsmith ............. tracing and evals, usable from any layer
```

The dependency direction matters: `langchain` depends on `langgraph`, and both depend on `langchain-core`. Partner packages depend only on `langchain-core`. That is why you can use `ChatOpenAI` in a pure LangGraph app without the `langchain` package at all.

## The package map

| Package | What lives there | Install when |
|---|---|---|
| `langchain-core` | `Runnable`, messages, prompt templates, `BaseTool`, output parsers, callbacks, fake models | Always (everything pulls it in) |
| `langchain` | `create_agent`, middleware, `init_chat_model`, `init_embeddings` | You build agents or want one factory for all providers |
| `langgraph` | `StateGraph`, checkpointers, `interrupt`, stream modes | Custom control flow, persistence, approvals |
| `langchain-openai` | `ChatOpenAI`, `AzureChatOpenAI`, `OpenAIEmbeddings` | You call OpenAI or Azure OpenAI |
| `langchain-anthropic` | `ChatAnthropic` | You call Claude |
| `langchain-community` | Community-maintained loaders, smaller vector stores, misc tools | Only when no partner package exists |
| `langchain-text-splitters` | `RecursiveCharacterTextSplitter` and friends | Chunking documents |
| `langsmith` | Tracing client, datasets, evaluations | Observability and offline evals |
| `langchain-classic` | Legacy chains, `AgentExecutor`, old memory classes | Only while migrating 0.x code |

Partner packages are versioned and released independently by LangChain and the provider. Prefer them over `langchain-community` whenever both exist: they track provider API changes faster and have fewer transitive dependencies.

```bash
# core agent stack plus two providers; pin these together in requirements
pip install "langchain[openai,anthropic]" langgraph langchain-text-splitters langsmith
```

The `langchain` package declares extras for most providers (`openai`, `anthropic`, `azure-ai`, `aws`, `ollama`, ...), so the extra installs the matching partner package.

## What changed in 1.x

LangChain 1.0 shipped in late 2025 and cut the package down to agent building blocks. Most blog posts and Stack Overflow answers you will find still show 0.x code, so you need to translate on sight.

| 0.x habit | 1.x replacement |
|---|---|
| `AgentExecutor`, `initialize_agent` | `langchain.agents.create_agent` |
| `langgraph.prebuilt.create_react_agent` | `create_agent` (the prebuilt one is deprecated) |
| `LLMChain(llm, prompt)` | LCEL: prompt, model and parser piped together |
| `RetrievalQA`, `ConversationalRetrievalChain` | LCEL chain or an agent with a retriever tool |
| `ConversationBufferMemory` and friends | LangGraph checkpointer keyed by `thread_id` |
| `pre_model_hook` / `post_model_hook` | Agent middleware |
| `from langchain.chat_models import ChatOpenAI` | `from langchain_openai import ChatOpenAI` or `init_chat_model` |
| Provider-specific `message.content` shapes | `message.content_blocks` (standard block types) |
| `message.text()` | `message.text` (a property now) |

Everything in the left column that still exists lives in `langchain-classic` with imports like `from langchain_classic.chains import ...`. Treat it as a migration crutch, not a place to write new code. The installed `langchain` 1.5 has no `chains` module at all, and it requires Python 3.11 or newer.

The biggest structural change: `create_agent` returns a compiled LangGraph graph. The agent loop is a graph with a `model` node and a `tools` node, so everything LangGraph offers (checkpointing, streaming, interrupts, deployment) applies to it directly.

```python
import importlib.util

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langgraph.graph.state import CompiledStateGraph


class FakeToolModel(GenericFakeChatModel):
    """Fake chat model that accepts tools so create_agent can bind them."""

    def bind_tools(self, tools, **kwargs):
        return self


def lookup_vendor(name: str) -> str:
    """Return payment terms and currency for a vendor."""
    return f"{name}: NET30, INR"


model = FakeToolModel(messages=iter([AIMessage("Acme is on NET30 terms.")]))
agent = create_agent(model, tools=[lookup_vendor], system_prompt="You answer AP questions.")

if __name__ == "__main__":
    # the agent is a LangGraph graph, and every graph is a Runnable
    assert isinstance(agent, CompiledStateGraph)
    assert isinstance(agent, Runnable)

    nodes = set(agent.get_graph().nodes)
    print(sorted(nodes))  # ['__end__', '__start__', 'model', 'tools']
    assert {"model", "tools"} <= nodes

    # legacy chains are not part of the langchain package any more
    assert importlib.util.find_spec("langchain.chains") is None

    out = agent.invoke({"messages": [{"role": "user", "content": "Terms for Acme?"}]})
    assert out["messages"][-1].content == "Acme is on NET30 terms."
```

`GenericFakeChatModel` replays the messages you give it, which makes agent code testable without API keys. Its base class has no `bind_tools`, so the four-line subclass adds a no-op one. You will reuse this pattern across the next notes.

### Middleware in one paragraph

Middleware is the 1.x extension point for agents. A middleware can run before or after the model call, wrap the model call, or wrap each tool call. Built-ins in the installed version include `HumanInTheLoopMiddleware`, `SummarizationMiddleware`, `PIIMiddleware`, `ModelCallLimitMiddleware`, `ToolCallLimitMiddleware`, `ModelFallbackMiddleware`, `ModelRetryMiddleware`, `ToolRetryMiddleware` and `ToolErrorMiddleware`. [Tools and agents in LangChain]({{ '/ai-stack/langchain-tools-and-agents/' | relative_url }}) shows them in use.

## How LangChain maps to a custom platform

If you have built a RAG and agent platform with your own abstractions, you already have most of these concepts. The interview skill is naming the equivalents and saying what you gain or lose.

| Your platform | LangChain / LangGraph equivalent |
|---|---|
| Provider abstraction or LLM router | `BaseChatModel`, `init_chat_model("provider:model")` |
| Provider failover | `with_fallbacks`, `ModelFallbackMiddleware` |
| Internal message dicts | `SystemMessage`, `HumanMessage`, `AIMessage`, `ToolMessage` |
| Tool registry with JSON schemas | `@tool` / `BaseTool`, schema from type hints or Pydantic |
| Agent loop (model, tools, repeat) | `create_agent` |
| Pre/post hooks: PII scrub, limits | Agent middleware |
| Conversation store | LangGraph checkpointer per `thread_id` |
| Prompt files with placeholders | `ChatPromptTemplate` |
| Token and cost logging | `usage_metadata`, callbacks, LangSmith |
| Retrieval layer | `VectorStore`, retrievers |

The cheapest adoption path is usually not a rewrite. Wrap your existing gateway client as a `BaseChatModel` and the rest of LangChain (prompts, parsers, structured output via tools, agents, tracing) works on top of it. You implement `_generate` and `_llm_type`; `invoke`, `batch`, `stream` (falls back to one chunk), async variants and callbacks come from the base class.

```python
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.prompts import ChatPromptTemplate


def gateway_complete(payload: dict) -> dict:
    """Stand-in for your platform's existing LLM gateway client."""
    question = payload["messages"][-1]["content"]
    usage = {"in": 42, "out": 9}
    return {"text": f"[{payload['route']}] answer to: {question}", "usage": usage}


ROLES = {"system": "system", "human": "user", "ai": "assistant", "tool": "tool"}


class GatewayChatModel(BaseChatModel):
    route: str = "finance-default"

    @property
    def _llm_type(self) -> str:
        return "internal-gateway"

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        payload = {
            "route": self.route,
            "messages": [{"role": ROLES[m.type], "content": m.text} for m in messages],
        }
        resp = gateway_complete(payload)
        used = resp["usage"]
        msg = AIMessage(
            resp["text"],
            usage_metadata={
                "input_tokens": used["in"],
                "output_tokens": used["out"],
                "total_tokens": used["in"] + used["out"],
            },
        )
        return ChatResult(generations=[ChatGeneration(message=msg)])


prompt = ChatPromptTemplate.from_messages([
    ("system", "You explain GL variances to controllers."),
    ("human", "{question}"),
])
chain = prompt | GatewayChatModel(route="gl-variance") | StrOutputParser()

if __name__ == "__main__":
    answers = chain.batch([{"question": "Why is 6100 up?"}, {"question": "Why is 7200 down?"}])
    print(answers)
    assert answers[0] == "[gl-variance] answer to: Why is 6100 up?"
    assert len(answers) == 2

    raw = (prompt | GatewayChatModel()).invoke({"question": "Q"})
    assert raw.usage_metadata["total_tokens"] == 51
```

What you give up by adopting the framework: some control over request shapes, an extra layer in stack traces, and upgrade churn (the 0.x to 1.x jump broke many imports). What you gain: tested provider adapters, one tool schema format across providers, structured output, LangGraph persistence and LangSmith tracing without writing them.

## LangChain vs LangGraph vs plain SDK

```text
  Do you need tools or multi-step reasoning?
     no  -> one call, one provider? -> plain SDK
            several providers, prompts, parsing, RAG -> LangChain LCEL
     yes -> standard loop (model, tools, repeat until done)?
              yes -> create_agent (+ middleware for guardrails)
              no  -> branches, approvals, retries per step,
                     resumable state, several agents -> LangGraph
```

| Situation | Pick | Why |
|---|---|---|
| Single classification call, strict p95 latency, one provider | Plain SDK | Fewest layers, direct access to new provider features |
| Invoice extraction to a Pydantic model across OpenAI and Claude | LangChain | `with_structured_output` hides provider differences |
| RAG answer over policy documents | LangChain LCEL | Linear pipeline: retrieve, format, generate, parse |
| Analyst assistant that queries GL data with tools | `create_agent` | Standard tool loop plus limits and PII middleware |
| Reconciliation workflow with an approval step and resume after hours | LangGraph | Explicit state, checkpointer, `interrupt` |
| Planner plus specialist agents | LangGraph | Custom routing between nodes |

These are not exclusive. A typical service uses LangChain models and tools inside LangGraph nodes, and a plain SDK call for one latency-critical path. The LangGraph side is covered from [LangGraph core concepts]({{ '/ai-stack/langgraph-core-concepts/' | relative_url }}) onward.

## Coming from Django

| Django | LangChain world |
|---|---|
| Django core vs `contrib` vs PyPI apps | `langchain-core` vs `langchain` vs partner and community packages |
| Database backends behind the ORM | Partner packages behind `BaseChatModel` |
| `MIDDLEWARE` wrapping request and response | Agent middleware wrapping model and tool calls |
| Serializers validating input | Pydantic schemas for tools and structured output |
| Sessions table | Checkpointer keyed by `thread_id` |
| Major-version upgrade guides | The v1 migration guide and `langchain-classic` |

The Django instinct to "use the framework's way" helps here only up to a point. LangChain is closer to a toolkit than a framework: you can take `langchain-core` and one partner package and ignore the rest.

## Common mistakes

- **Copying 0.x tutorials.** `LLMChain`, `AgentExecutor` and `from langchain.llms import OpenAI` are signs the example is old. Translate to LCEL or `create_agent`.
- **Installing `langchain-community` for a provider that has a partner package.** Use `langchain-openai` or `langchain-anthropic`; the community version may lag or be removed.
- **Pinning `langchain` but not `langchain-core`.** Partner packages float on core. Pin every `langchain-*`, `langgraph*` and `langsmith` version in one lock file and upgrade them together.
- **Assuming LangSmith requires LangChain.** The `langsmith` SDK traces any Python function with `@traceable`; LangChain just emits traces automatically.
- **Adopting the whole stack for one call.** A single structured extraction behind a FastAPI route does not need an agent. Start with the smallest layer that covers the requirement.
- **Rewriting a working platform instead of wrapping it.** Wrap your gateway as a `BaseChatModel` and your tools as `BaseTool`, then migrate piece by piece.

## Practice tasks

1. **Map the imports.** In a fresh venv install the stack above. Write a script that imports `ChatPromptTemplate`, `create_agent`, `StateGraph`, `tool`, `InMemoryVectorStore` and `traceable` and prints each object's `__module__`. Done: you can say which package each comes from without looking.
2. **Wrap a gateway.** Extend `GatewayChatModel` so `stop` sequences are forwarded and the gateway's latency is stored in `response_metadata`. Done: a test asserts both fields after `chain.invoke`.
3. **Port legacy code.** Take any 0.x example that uses `LLMChain` or `AgentExecutor` and rewrite it with LCEL or `create_agent`. Done: it runs on `langchain` 1.5 with no `langchain_classic` import.
4. **Write a decision record.** For three features of a finance platform (invoice extraction, policy Q&A, month-end reconciliation assistant), choose SDK, LangChain or LangGraph. Done: one page with the choice, the deciding requirement and the exit plan if the choice turns out wrong.

## Interview questions

**What is in `langchain-core` versus `langchain`?**
Core holds the interfaces and primitives: `Runnable`, messages, prompts, tools, parsers, callbacks. The `langchain` package in 1.x holds agent building blocks: `create_agent`, middleware and the `init_chat_model` factory. Integrations live in separate partner packages.

**What changed in LangChain 1.0?**
The package was slimmed to agents. `create_agent` replaced `AgentExecutor` and LangGraph's `create_react_agent`, it runs on LangGraph, and middleware replaced ad hoc hooks. Legacy chains and memory moved to `langchain-classic`. Messages gained standard `content_blocks`.

**Why is `create_agent` built on LangGraph, and why does that matter?**
The agent loop is a graph (model node, tools node, conditional edge). Building it on LangGraph gives you checkpointing, streaming, interrupts for human approval and deployment for free, and you can drop down to a custom graph without changing runtimes.

**When would you not use LangChain?**
A single, latency-sensitive call to one provider, or when you need a brand new provider feature the adapter does not expose yet. A plain SDK call is fewer layers and easier to debug.

**When is `create_agent` not enough and you need LangGraph directly?**
When the flow is not "call model, run tools, repeat": fixed steps with branching, approvals mid-workflow, per-step retries, parallel fan-out, or several cooperating agents with explicit state.

**How would you introduce LangChain into an existing in-house LLM platform?**
Wrap the existing gateway as a `BaseChatModel` and existing tools as `BaseTool`, so nothing upstream changes. Then adopt structured output, tracing and agents incrementally behind feature flags, comparing outputs on an eval set.

**Partner package or `langchain-community`?**
Partner package when it exists: maintained with the provider, released independently, fewer dependencies. Community only for integrations with no partner package.

**How do you keep LangChain upgrades from breaking production?**
Pin all `langchain-*`, `langgraph*` and `langsmith` versions together, run an eval suite plus unit tests with fake models in CI, and read the release notes for deprecations before bumping.

## Further reading

- LangChain docs: "Overview", "Install" and "What's new in v1"
- LangChain docs: "LangChain v1 migration guide"
- LangChain docs: "Agents" and "Middleware"
- LangChain docs: "Models" (the `init_chat_model` section)
- LangGraph docs: "Overview" and "Thinking in LangGraph"
- LangSmith docs: "Observability quickstart"
