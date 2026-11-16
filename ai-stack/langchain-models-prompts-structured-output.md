---
title: Models, prompts and structured output
section: ai-stack
order: 15
phase: "Phase 3 · LangChain"
summary: Call any chat model through one interface, build prompts with history and examples, get validated Pydantic objects, add retries and token tracking.
---
After this note you can call OpenAI, Azure OpenAI or Claude through the same LangChain interface, build prompt templates with chat history and few-shot examples, extract a validated `Invoice` object from free text, and make the call resilient and cost-tracked. This is the layer under every RAG answer and agent step in a finance data platform: most production bugs at this level are a prompt variable that went missing, a schema that the model half-followed, or a retry policy that tripled the bill.

## Prerequisites

[LangChain architecture and packages]({{ '/ai-stack/langchain-architecture/' | relative_url }}) for the package map. Background on why structured output matters: [structured outputs]({{ '/llm/structured-outputs/' | relative_url }}).

## Chat models

A chat model takes a list of messages and returns one `AIMessage`. Every provider class (`ChatOpenAI`, `AzureChatOpenAI`, `ChatAnthropic`) subclasses `BaseChatModel`, so code written against that interface works with any of them. `init_chat_model` builds the right class from a `"provider:model"` string, which keeps provider choice in configuration instead of imports.

```python
# illustrative: needs langchain-openai / langchain-anthropic and API keys
from langchain.chat_models import init_chat_model

gpt = init_chat_model("openai:gpt-4.1-mini", temperature=0, max_tokens=800, timeout=30, max_retries=2)
claude = init_chat_model("anthropic:claude-sonnet-4-5", temperature=0, max_tokens=800)
azure = init_chat_model(
    "azure_openai:gpt-4.1",
    azure_deployment="gpt41-finance",  # your deployment name, not the model name
    api_version="2024-10-21",
)

# provider and model chosen per request (e.g. from tenant settings)
switchable = init_chat_model(configurable_fields=("model", "model_provider", "temperature"))
```

The parameters that matter in production:

| Parameter | What it controls | Finance default |
|---|---|---|
| `temperature` | Sampling randomness | `0` for extraction and classification |
| `max_tokens` | Output cap, also a cost cap | Set it on every model |
| `timeout` | Seconds before the HTTP call fails | 20 to 60, below your API gateway timeout |
| `max_retries` | Retries inside the provider SDK | 2; do not stack with a second retry layer blindly |
| `stop` | Stop sequences | Rarely needed with chat models |
| `rate_limiter` | Client-side throttle (`InMemoryRateLimiter`) | Batch jobs that hit provider rate limits |

Per-call overrides go through `model.bind(...)`, which returns a new runnable with those kwargs fixed, for example `model.bind(max_tokens=200)` for a short summary step.

## Messages

| Class | Role | Typical content |
|---|---|---|
| `SystemMessage` | Instructions | Policy, persona, output rules |
| `HumanMessage` | User input | The question, the invoice text |
| `AIMessage` | Model output | Text, `tool_calls`, `usage_metadata` |
| `ToolMessage` | Tool result | Result tied to a `tool_call_id` |

Anywhere LangChain accepts messages it also accepts `("human", "text")` tuples and `{"role": "user", "content": "text"}` dicts. Read text with `message.text` (a property in 1.x) and provider-neutral content with `message.content_blocks`, which normalizes text, reasoning, images and tool calls into typed blocks.

## invoke, batch and stream

The three calls you use every day. `invoke` returns one `AIMessage`. `batch` runs a list of inputs concurrently on a thread pool and keeps input order. `stream` yields `AIMessageChunk` objects that you can add together.

```python
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage

# real model: init_chat_model("openai:gpt-4.1-mini", temperature=0)
model = FakeListChatModel(responses=["Vendor spend rose 12 percent, driven by cloud hosting."])

messages = [
    SystemMessage("You are a finance analyst. Answer in one sentence."),
    HumanMessage("Summarize Q3 vendor spend."),
]

if __name__ == "__main__":
    reply = model.invoke(messages)
    assert isinstance(reply, AIMessage)
    print(reply.text)

    replies = model.batch([[("human", "Q3 spend?")], [("human", "Q4 spend?")]], config={"max_concurrency": 4})
    assert len(replies) == 2 and all(isinstance(r, AIMessage) for r in replies)

    full = None
    for chunk in model.stream(messages):
        assert isinstance(chunk, AIMessageChunk)
        full = chunk if full is None else full + chunk  # chunks merge, including tool call chunks
    assert full.text == reply.text
```

Each of these has an async twin (`ainvoke`, `abatch`, `astream`). Inside FastAPI async routes, use the async ones; the sync versions block the event loop.

## Prompt templates

`ChatPromptTemplate` turns a dict of variables into a list of messages. Variables use single braces, `{entity}`, and are checked at invoke time: a missing key raises `KeyError` instead of sending a half-filled prompt. `MessagesPlaceholder` splices in a list of messages (chat history, retrieved context as messages, earlier tool calls). `partial` fixes variables up front, useful for values from settings. `FewShotChatMessagePromptTemplate` renders examples as alternating human and AI turns, which models follow better than examples pasted into the system prompt.

```python
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, FewShotChatMessagePromptTemplate, MessagesPlaceholder

examples = [
    {"line": "AWS invoice 4471 cloud hosting", "category": "IT infrastructure"},
    {"line": "Uber rides Bangalore client visit", "category": "Travel"},
]
example_prompt = ChatPromptTemplate.from_messages([("human", "{line}"), ("ai", "{category}")])
few_shot = FewShotChatMessagePromptTemplate(examples=examples, example_prompt=example_prompt)

prompt = ChatPromptTemplate.from_messages([
    ("system", "Classify expense lines for entity {entity}. Allowed: {categories}."),
    few_shot,
    MessagesPlaceholder("history", optional=True),
    ("human", "{line}"),
]).partial(categories="IT infrastructure, Travel, Professional fees, Other")

if __name__ == "__main__":
    # categories is fixed by partial; history is optional
    assert prompt.input_variables == ["entity", "line"]
    assert prompt.optional_variables == ["history"]

    value = prompt.invoke({
        "entity": "IN01",
        "line": "Deloitte audit fee Q2",
        "history": [HumanMessage("Use our GL category names."), AIMessage("Understood.")],
    })
    msgs = value.to_messages()
    for m in msgs:
        print(f"{m.type:>6}: {m.text}")
    assert msgs[0].type == "system" and "IN01" in msgs[0].text
    assert [m.type for m in msgs[1:5]] == ["human", "ai", "human", "ai"]  # few-shot turns
    assert msgs[-1].text == "Deloitte audit fee Q2"

    try:
        prompt.invoke({"entity": "IN01"})
    except KeyError as exc:
        print("missing variable:", exc)
    else:
        raise AssertionError("expected KeyError")
```

For long example sets, swap the fixed list for an example selector that picks the most similar examples per input. Keep prompts in code next to their tests, not in a database, unless non-engineers need to edit them; then version them like migrations.

## Structured output with Pydantic

You want a validated object, not a string you regex. `with_structured_output(Schema)` does three things: converts the Pydantic class to a JSON schema, asks the model to fill it (through tool calling or the provider's native JSON schema mode), and parses the result back into the class. Validation errors raise instead of passing bad data downstream.

```text
  Invoice (Pydantic class)
     |  to JSON schema
     v
  model.bind_tools([Invoice], tool_choice="any")   (or native JSON mode)
     |
     v
  AIMessage.tool_calls[0]: name "Invoice", args {...}
     |  PydanticToolsParser
     v
  Invoice(vendor="Acme Cloud", total=Decimal("1180.00"))
```

The base implementation in `langchain-core` uses the tool-calling path, which is why a fake model that returns a tool call named after the schema is enough to test it.

```python
from datetime import date
from decimal import Decimal

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field, model_validator


class FakeToolModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class LineItem(BaseModel):
    description: str
    amount: Decimal = Field(description="Line amount before tax")


class Invoice(BaseModel):
    """Fields extracted from a vendor invoice."""

    vendor: str = Field(description="Vendor legal name as printed")
    invoice_number: str
    invoice_date: date
    currency: str = Field(pattern="^[A-Z]{3}$", description="ISO 4217 code")
    lines: list[LineItem]
    tax: Decimal
    total: Decimal

    @model_validator(mode="after")
    def totals_tie_out(self):
        if sum(item.amount for item in self.lines) + self.tax != self.total:
            raise ValueError("line amounts plus tax do not equal total")
        return self


def invoice_call(total: str) -> AIMessage:
    args = {
        "vendor": "Acme Cloud Pvt Ltd", "invoice_number": "INV-4471", "invoice_date": "2026-10-03",
        "currency": "INR", "lines": [{"description": "Hosting October", "amount": "1000.00"}],
        "tax": "180.00", "total": total,
    }
    return AIMessage("", tool_calls=[{"name": "Invoice", "args": args, "id": "call_1"}])


prompt = ChatPromptTemplate.from_messages([
    ("system", "Extract the invoice. Copy numbers exactly; never compute missing values."),
    ("human", "{document}"),
])

# real model: init_chat_model("openai:gpt-4.1-mini", temperature=0)
model = FakeToolModel(messages=iter([invoice_call("1180.00"), invoice_call("1200.00")]))
extract = prompt | model.with_structured_output(Invoice)
extract_safe = prompt | model.with_structured_output(Invoice, include_raw=True)

if __name__ == "__main__":
    inv = extract.invoke({"document": "Acme Cloud ... Total INR 1,180.00"})
    assert isinstance(inv, Invoice)
    assert inv.total == Decimal("1180.00") and inv.invoice_date == date(2026, 10, 3)
    print(inv.model_dump_json())

    # include_raw keeps the raw message and the error instead of raising
    result = extract_safe.invoke({"document": "Acme Cloud ... Total INR 1,200.00"})
    assert result["parsed"] is None
    assert "do not equal total" in str(result["parsing_error"])
    assert result["raw"].tool_calls[0]["name"] == "Invoice"
```

With real providers, the partner package chooses how to enforce the schema. You can pass `method="json_schema"` (provider-native structured output, with `strict=True` on OpenAI), `method="function_calling"` or `method="json_mode"`. Native JSON schema is the most reliable when the provider supports it; tool calling is the most portable.

> **Rule of thumb:** put business rules (totals tie out, dates not in the future, currency allow-list) in Pydantic validators, not in the prompt alone. The prompt reduces errors; the validator catches the rest, and `include_raw=True` gives you the raw output to log or send to a review queue.

## Output parsers

Parsers turn a model's text into Python values. With tool calling and native JSON modes available, you need them less, but three remain useful:

| Parser | Use it for |
|---|---|
| `StrOutputParser` | End of any text chain: `AIMessage` to `str` |
| `PydanticOutputParser` | Models or gateways without tool calling or JSON mode |
| `JsonOutputParser` | Streaming partial JSON to a UI as it builds |

```python
from typing import Literal

from langchain_core.exceptions import OutputParserException
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.output_parsers import JsonOutputParser, PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field


class Category(BaseModel):
    category: Literal["IT infrastructure", "Travel", "Professional fees", "Other"]
    confidence: float = Field(ge=0, le=1)


parser = PydanticOutputParser(pydantic_object=Category)
prompt = ChatPromptTemplate.from_messages([
    ("system", "Classify the expense line.\n{format_instructions}"),
    ("human", "{line}"),
]).partial(format_instructions=parser.get_format_instructions())

model = FakeListChatModel(responses=[
    '{"category": "Professional fees", "confidence": 0.93}',
    '{"category": "Entertainment", "confidence": 0.5}',
])
chain = prompt | model | parser

if __name__ == "__main__":
    first = chain.invoke({"line": "Deloitte audit fee Q2"})
    assert first == Category(category="Professional fees", confidence=0.93)

    try:
        chain.invoke({"line": "Team dinner"})  # category outside the allowed set
    except OutputParserException as exc:
        print("rejected:", str(exc).splitlines()[0])
    else:
        raise AssertionError("expected OutputParserException")

    # JsonOutputParser yields growing partial dicts while the model streams
    streamer = FakeListChatModel(responses=['{"category": "Travel", "confidence": 0.9}'])
    partials = list((streamer | JsonOutputParser()).stream("Cab to airport"))
    assert partials[-1] == {"category": "Travel", "confidence": 0.9}
    assert len(partials) > 2
```

The format instructions contain literal JSON braces. Passing them through `partial` (or as a variable) keeps the template from treating those braces as variables.

## Retries and fallbacks

Two different failures need two different tools. Transient errors (timeouts, 429s, 5xx) get `with_retry`. A provider outage or a model that keeps failing gets `with_fallbacks`, which tries the next runnable in the list.

```python
from langchain_core.language_models.fake_chat_models import FakeListChatModel, GenericFakeChatModel
from langchain_core.messages import AIMessage


class FlakyModel(GenericFakeChatModel):
    """Fails a set number of times before answering, like a provider under load."""

    failures_left: int = 2

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.failures_left > 0:
            self.failures_left -= 1
            raise TimeoutError("provider timed out")
        return super()._generate(messages, stop, run_manager, **kwargs)


primary = FlakyModel(messages=iter([AIMessage("primary answer")]), failures_left=2)
retrying = primary.with_retry(
    retry_if_exception_type=(TimeoutError,),
    stop_after_attempt=3,
    wait_exponential_jitter=False,  # keep the demo fast; leave jitter on in production
)

outage = FlakyModel(messages=iter([]), failures_left=100)
backup = FakeListChatModel(responses=["backup answer"])
resilient = outage.with_retry(stop_after_attempt=2, wait_exponential_jitter=False).with_fallbacks([backup])

if __name__ == "__main__":
    assert retrying.invoke("Close status?").text == "primary answer"
    assert primary.failures_left == 0
    assert resilient.invoke("Close status?").text == "backup answer"
    assert outage.failures_left == 98  # two attempts, then the fallback ran
```

Things to decide deliberately:

- **Where to retry.** Provider SDKs already retry (`max_retries`). Adding `with_retry(stop_after_attempt=3)` on top gives up to 9 HTTP calls. Pick one layer.
- **What to retry.** Retry timeouts and rate limits. Do not retry 400s or auth errors; they fail the same way every time.
- **Fallback scope.** Falling back from GPT to Claude may need a different prompt. Put the fallback on the whole chain (`chain_a.with_fallbacks([chain_b])`) when prompts differ.
- **Parse failures.** A fallback from a small model to a larger one on `OutputParserException` (`exceptions_to_handle=(OutputParserException,)`) is a cheap quality lever for extraction.

## Token usage and cost

Every `AIMessage` from a provider carries `usage_metadata` with `input_tokens`, `output_tokens` and `total_tokens`, plus details like cached input tokens when the provider reports them. `get_usage_metadata_callback` aggregates usage per model name across every call in a block, including calls nested deep inside chains and agents.

```python
from langchain_core.callbacks import get_usage_metadata_callback
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

# placeholder USD prices per million tokens: (input, output); load real ones from config
PRICES = {"fake-mini": (0.40, 1.60)}


def reply(text: str, tokens_in: int, tokens_out: int) -> AIMessage:
    return AIMessage(
        text,
        usage_metadata={"input_tokens": tokens_in, "output_tokens": tokens_out, "total_tokens": tokens_in + tokens_out},
        response_metadata={"model_name": "fake-mini"},
    )


model = GenericFakeChatModel(messages=iter([
    reply("Accruals for October look complete.", 1_200, 80),
    reply("Three vendors lack PO numbers.", 2_000, 120),
]))


def cost_usd(usage_by_model: dict) -> float:
    total = 0.0
    for name, used in usage_by_model.items():
        price_in, price_out = PRICES[name]
        total += (used["input_tokens"] * price_in + used["output_tokens"] * price_out) / 1_000_000
    return total


if __name__ == "__main__":
    with get_usage_metadata_callback() as cb:
        model.invoke("Check accruals")
        model.invoke("Check PO coverage")
    usage = cb.usage_metadata
    print(usage)
    assert usage["fake-mini"]["input_tokens"] == 3_200
    assert usage["fake-mini"]["output_tokens"] == 200
    assert round(cost_usd(usage), 6) == 0.00160
```

In a service, attach this per request and write the totals to your request log with tenant and route, so you can answer "what does the month-end assistant cost per entity" with a query. Some providers report usage on streams only when asked (for example `stream_usage=True` on `ChatOpenAI`). For deeper coverage see [latency and cost]({{ '/llm/latency-and-cost/' | relative_url }}).

## Coming from Django

| Django / DRF | LangChain |
|---|---|
| Template variables in double braces | `ChatPromptTemplate` with single-brace `{var}` |
| Missing template variable renders empty | Missing prompt variable raises `KeyError` |
| `Serializer.is_valid()` on request data | Pydantic validation in `with_structured_output` |
| `serializer.errors` | `parsing_error` with `include_raw=True` |
| `CACHES` / DB backends selected in settings | `init_chat_model("provider:model")` from settings |
| Retrying a Celery task with `autoretry_for` | `with_retry(retry_if_exception_type=...)` |

The closest analogy: treat model output like untrusted request data. Validate it with a schema, reject or quarantine what fails, and never write it to the ledger unchecked.

## Common mistakes

- **Formatting prompts with f-strings.** You lose missing-variable checks and tracing of inputs. Use templates and pass variables.
- **Literal braces in a template.** JSON examples inside a template string are read as variables. Pass them through `partial` or a variable, or double the braces in the template text.
- **Floats for money.** Use `Decimal` in schemas and compare totals exactly; floats make tie-out checks flaky.
- **Trusting the schema to enforce business rules.** A schema guarantees shape, not correctness. Add validators and a review path for failures.
- **Stacked retries.** SDK `max_retries` times `with_retry` attempts times a Celery retry can multiply cost and latency. Choose one layer per failure type.
- **Sync calls in async routes.** `model.invoke` inside `async def` blocks the event loop. Use `ainvoke`.
- **No `max_tokens`.** A runaway generation is a cost incident. Cap every model.

## Practice tasks

1. **Prompt with history.** Build a prompt for a GL assistant with a system message, a `MessagesPlaceholder` for history and a human question. Done: a test shows rendered messages in the right order and a `KeyError` when the question is missing.
2. **Invoice extractor.** Extend `Invoice` with `due_date` and a validator that rejects a due date before the invoice date. Done: two fake model responses, one valid and one invalid, with asserts on `parsed` and `parsing_error`.
3. **Fallback on parse failure.** Chain a cheap fake model that returns invalid JSON with a fallback model that returns valid JSON, falling back only on `OutputParserException`. Done: the test proves the second model ran and that a `TimeoutError` does not trigger the fallback.
4. **Per-tenant cost log.** Wrap a chain call in `get_usage_metadata_callback`, compute cost from a price table and return it alongside the answer from a FastAPI route. Done: a `TestClient` test asserts the cost field.
5. **Provider switch.** Use `init_chat_model` with `configurable_fields` and pick the provider from a tenant setting. Done: illustrative code plus a unit test that checks the config dict your code builds per tenant.

## Interview questions

**What does `init_chat_model` give you over importing `ChatOpenAI` directly?**
One factory that builds any provider's chat model from a `"provider:model"` string, so the provider is configuration. With `configurable_fields` you can switch model or provider per request through `config`.

**How does `with_structured_output` work?**
It converts the schema to JSON schema, makes the model fill it via tool calling or the provider's native structured output mode, and parses the result into the Pydantic class. The core default binds the schema as a tool with forced tool choice and parses the first tool call.

**Tool calling, JSON mode or native JSON schema for extraction?**
Native JSON schema with strict mode when the provider supports it, because decoding is constrained to the schema. Tool calling is the portable fallback. Plain JSON mode only guarantees valid JSON, not your fields.

**How do you handle a model output that fails validation?**
Use `include_raw=True` to get the raw message and `parsing_error`, log both, then either re-ask with the error, fall back to a stronger model, or route the document to human review. Never coerce silently.

**Why are few-shot examples better as message pairs than inside the system prompt?**
Models are trained on dialogue turns, so human/AI pairs show the exact input-output format. It also keeps examples swappable with an example selector.

**Difference between `with_retry` and `with_fallbacks`?**
Retry repeats the same runnable for transient errors. Fallbacks try a different runnable when the first one fails, for outages or persistent errors. Typically you retry inside and fall back outside.

**How do you track token cost per request?**
Read `usage_metadata` on each `AIMessage`, or wrap the request in `get_usage_metadata_callback` to aggregate across nested calls by model, multiply by a price table and log with tenant and route. LangSmith shows the same data per trace.

**What would you set on a model used for invoice extraction?**
`temperature=0`, a `max_tokens` cap, a timeout below the API gateway's, SDK retries for transient errors, structured output with a Pydantic schema containing `Decimal` amounts and business validators.

## Further reading

- LangChain docs: "Models" (invocation, parameters, `init_chat_model`)
- LangChain docs: "Messages" and "Standard content blocks"
- LangChain docs: "Structured output"
- LangChain API reference: `ChatPromptTemplate`, `MessagesPlaceholder`, `FewShotChatMessagePromptTemplate`
- LangChain API reference: `Runnable.with_retry`, `Runnable.with_fallbacks`
- LangChain how-to: "Track token usage"
