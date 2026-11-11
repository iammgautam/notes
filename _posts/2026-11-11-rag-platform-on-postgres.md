---
date: 2026-11-11 13:00:00 +0530
title: "The tool boundary is a trust boundary: inside our LLM platform"
summary: "How our LLM and RAG platform on Postgres is built, and the 'no suppliers in Texas' bug I fixed where model output meets the database."
tags: [llm, agents, pgvector, postgresql]
---

A user typed "show me suppliers in Texas" into our in-app assistant. It called the right tool and told them, politely and with full confidence, that there were no suppliers in Texas. There were plenty.

Nothing crashed and no error was logged. The model, the tool and the database each did their job; the bug lived in the gap between them. Fixing it was my job, and it's a small change with a bigger lesson than the diff. To explain why the fix belongs where it does, I first need to explain the platform it lives in.

## What the assistant is for

Gainfront makes supplier lifecycle management software for procurement teams: supplier onboarding, risk, contracts, spend, and diversity and sustainability tracking. Procurement people spend their day asking questions of that data ("which certified diverse suppliers do we have in Texas?") and filling in long questionnaires from their own customers, where the same questions come back again and again.

Our team built an AI platform for both jobs: a chat assistant that reads real data through tools and can drive the UI, a retrieval system that drafts questionnaire answers from the company's own documents, and a "prompt plus JSON Schema in, validated JSON out" API that a couple of dozen backend features use for summaries, extraction and classification. I work on and extend this platform end to end.

## Constraints

- **More than one model vendor.** Prices and quality shift every few months. Changing models must be a config change.
- **Permission-controlled data.** Users see only what their role allows. The assistant must not become a side door.
- **A human confirms actions.** Reading is safe. Changing data or acting in the browser needs the user to see it.
- **Visible cost** per feature, per user and per model.
- **Grounded answers.** In a compliance questionnaire, an invented fact is worse than no answer.
- **No new infrastructure.** Each customer runs in an isolated environment, so anything extra is multiplied by the number of customers.

These ruled things out. A framework like LangChain would bring abstractions we didn't need and upgrades to chase; our model layer and agent loop are around 1,900 lines we own. A dedicated vector database would be one more stateful service per customer. WebSockets would need a new connection type, while Server-Sent Events pass through the existing proxy and auth untouched.

## Four layers, dependencies pointing down

```text
+------------------------------------------------------------------+
| D  Retrieval    LLM fact extraction -> embeddings -> pgvector    |
+------------------------------------------------------------------+
| C  Chat         planner, orchestrator, permission-checked tools, |
|                 browser tools, SSE streaming, rolling memory     |
+------------------------------------------------------------------+
| B  Agent        bounded tool loop, then validated JSON output    |
+------------------------------------------------------------------+
| A  Models       one message format, "provider:model" routing,    |
|                 one adapter per wire protocol, token accounting  |
+------------------------------------------------------------------+
        |                     |                      |
   OpenAI-style         Anthropic-style         Gemini-style
```

Layer A knows nothing about chat or Django. Layer B is plain Python. Layer C adds users, permissions, the browser and the message log. Each changes for a different reason (a vendor SDK release, a new agent algorithm, a chat feature), so one kind of change doesn't ripple into the others.

## One message format for every model

Every vendor shapes a conversation differently. OpenAI puts tool calls in an array with the arguments as a JSON string, and a tool result is its own message. Anthropic uses content blocks and puts tool results inside a user message. Gemini uses "parts", and a function response must carry the tool's name, not just a call id.

So every layer above the adapters speaks one format: a role plus a list of typed blocks. It has to be a list, because a real model turn is mixed: "let me check" followed by two tool calls is one assistant turn. A simplified version:

```python
from typing import Annotated, Literal, Union
from pydantic import BaseModel, Field

class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str

class ToolUse(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    call_id: str
    name: str
    args: dict              # always a dict; adapters serialize if needed

class ToolOutput(BaseModel):
    type: Literal["tool_output"] = "tool_output"
    call_id: str
    tool_name: str          # Gemini-style APIs need the name back
    output: object
    failed: bool = False

Part = Annotated[Union[TextPart, ToolUse, ToolOutput],
                 Field(discriminator="type")]

class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    parts: list[Part] = []
```

The discriminator matters more than it looks, because this is also the storage format. The chat log keeps the parts in a JSON column, and on the way out pydantic reads `type` and rebuilds the right class. One format in memory, on the wire to the browser and on disk, with no mapping layer to drift. Supporting images means adding an image block; old rows still parse and the column needs no migration.

Token usage gets the same treatment: input, output, thinking and cache tokens in one shape every adapter maps into, so a ten-step run reports one total and cost reports sum across vendors.

## Routing by role, with a string

Callers never import an SDK. Each role has a model, written as `provider:model` in the deployment's config. The planner that picks tools runs on every user turn, so it gets a small, fast model. The agent does the reasoning, so it gets the budget. The summarizer that compresses old chat history gets the cheapest model that writes clean prose. A simplified resolver:

```python
import threading

MODEL_FOR_ROLE = {                      # loaded from config per deployment
    "planner":    "vendor_a:small-fast",
    "agent":      "vendor_b:large",
    "summarizer": "vendor_a:small-fast",
}
_backends, _ready, _lock = {}, False, threading.Lock()

def _load_backends():
    global _ready
    if _ready:                          # fast path: no lock after startup
        return
    with _lock:
        if not _ready:                  # another thread may have won
            from .backends import all_backends   # heavy SDK imports
            _backends.update({b.name: b for b in all_backends()})
            _ready = True

def client_for(role):
    _load_backends()
    name, _, model = MODEL_FOR_ROLE[role].partition(":")  # first colon only
    if name not in _backends:
        raise ValueError(f"unknown provider {name!r}; have {sorted(_backends)}")
    return _backends[name].client(model)  # fresh client on every call
```

Startup is lazy because vendor SDKs are heavy imports and most requests never touch AI. It uses double-checked locking because the first two AI requests can arrive together. Clients are deliberately not cached: cloud credentials expire, and a cached client with a stale token fails in confusing ways. Building one is cheap once the SDK is loaded.

## Five providers, three adapters

We reach models five ways: three vendors' direct APIs, plus two vendors' models served through a cloud platform with service-account auth. Same wire protocol, different login. So a provider answers "how do I connect?", an adapter answers "how do I talk?", and the cloud-hosted providers reuse the adapter for their protocol. That's the bridge pattern: each protocol's conversion code exists once.

Each adapter hides its vendor's quirks from everything above it:

| Quirk | What the adapter does |
|---|---|
| Tools and a response schema rarely work in one request | Two methods: chat with tools, or force JSON |
| OpenAI strict mode needs every property required | Rewrite optional fields as required and nullable; send non-strict if the schema has open objects |
| Claude via the cloud platform may reject the output-format field | Catch that exact error; retry with one forced tool whose input is the target schema |
| Gemini rejects `additionalProperties` | Strip it recursively |
| Gemini can say "stop" while returning tool calls | Any reply with a tool call is a tool turn |

The fallbacks catch the one known error and nothing else. A broad `except` would also swallow a bad API key or a quota error, and those should fail loudly.

## Tools: a definition plus a handler

A tool is one object that is both what the model sees (name, description, JSON Schema) and what the backend runs, so there's no second list to keep in sync. The base class owns the fixed steps and the tool author writes one method:

```python
import time

class ToolInputError(Exception):
    """A message written for the model, e.g. 'give at least one filter'."""

class AgentTool:
    name: str
    description: str
    parameters: dict              # JSON Schema shown to the model

    def run(self, **args): ...    # the only method a tool author writes

    def __call__(self, **args):
        started = time.monotonic()
        try:
            return {"ok": True, "output": self.run(**args)}
        except ToolInputError as e:
            return {"ok": False, "output": str(e)}
        except Exception as e:    # deliberate: an error becomes data
            return {"ok": False, "output": f"{type(e).__name__}: {e}"}
        finally:
            record_timing(self.name, time.monotonic() - started)
```

Catching every exception looks like a smell and isn't one here. A model can recover from an error it can read: it sees "unknown sort key 'newest', allowed: name, created" and calls again correctly. If the exception crashed the request, the user would get "something went wrong" and the model would learn nothing. The failure flag marks the output as an error, not data.

## The agent loop, and why it always answers

The generic agent runs in two phases because of the "tools or schema, not both" rule. Phase one lets the model reason and call tools. Phase two makes one more call with no tools and a forced schema, then validates:

```python
def run_agent(client, messages, tools, Schema, max_steps=15):
    for step in range(max_steps + 1):
        last = step == max_steps
        if last:
            messages.append(system("Do not call tools. Answer now "
                                   "with what you already have."))
        reply = client.chat(messages,
                            tools=None if last else list(tools.values()))
        messages.append(reply)
        if not reply.tool_uses():
            break
        messages.append(tool_message(
            [tools[c.name](**c.args) for c in reply.tool_uses()]))

    for _ in range(4):                    # one try, three corrections
        raw = client.force_json(messages, schema=Schema.model_json_schema())
        try:
            return Schema.model_validate_json(raw)
        except ValidationError as err:
            messages.append(user(f"That reply did not match the schema: "
                                 f"{err}. Reply with JSON only."))
    raise StructuredOutputFailed()
```

The real stop conditions also cover too many tool messages, repeated empty replies and a truncated reply. When the budget runs out the loop doesn't throw: it takes the tools away, so the model can only write text and the user gets the best answer the gathered data allows. In phase two a plain retry usually repeats the mistake, so the retry sends back the exact validation error.

## The chat assistant is a workflow, not a call

One chat message can become a dozen model and tool steps, some of them in the browser. Here is "find diverse suppliers in Texas and open the first one":

```text
browser                  server                            models
-------                  ------                            ------
send message ----------> save user turn
start run (SSE) -------> no plan for this turn yet
                         planner picks tools ------------> small model
                         save plan, stream "thinking"
                         loop:
                           ask agent ---------------------> agent model
                           save + stream assistant turn
                           server tool? run, save, stream
                           browser tool? save empty marker,
                             stream it, end the request
runs "open page"
post tool result ------> attach result to the marker
start run (SSE) -------> plan found in log, skip planner
                           ask agent ---------------------> agent model
                           save + stream final answer, "done"
```

The planner is a cheap router in front of the expensive model. It returns strict JSON (in scope or not, which tools, which pages), treated like user input: invented tool names are dropped by a whitelist of the user's enabled tools, the list is capped, and an "ask the user" tool is always added. Out-of-scope requests get a fixed polite decline and never reach the agent model.

The orchestrator is a generator. It yields each message the moment it exists, and the view saves and streams it over SSE before asking for the next. Close the tab halfway and every finished step is already in the log. A browser tool pauses the run by saving an empty tool message. "Paused" is never stored as a status; it's derived from the log as "a browser tool call with no result yet", so it can't drift after a crash. If the user ignores a question and types something new, the view first writes a "cancelled" result for the open call, because vendors reject a history with an unanswered tool call. The browser caps its re-runs, then asks for a forced final answer.

## Keeping the model inside the user's permissions

The rule: the model never sees a row the user couldn't open in the UI. Customers are already isolated from each other, so the work is between roles inside one customer. Each tool declares the permissions and product modules it needs, and a tool the user can't use is never shown to the model. You can't ask for what you can't see. Inside the handler, every query starts from "the suppliers this user may see", never the whole table. Tool output returns to the model as a data block, never as system text, which blunts prompt injection through stored data.

## Retrieval on pgvector

For questionnaires, the system drafts each answer from three sources: curated knowledge-base entries, facts from uploaded policy documents, and past answers. Instead of fixed-size chunks, an LLM at temperature 0 lists each document's facts verbatim, one per line, and each fact becomes a retrieval unit with one sharp meaning. Questions and answers rarely share words ("what is your uptime SLA?" vs "we guarantee 99.9% availability"), so stored text is embedded in "document" mode and the question in "query" mode, which places a question near the passages that answer it.

The vectors live in the same Postgres as everything else. Re-ingesting a document builds the new vectors first, then swaps them in one short transaction, so a search never sees half old facts and half new. Past answers enter the index only once a questionnaire is completed, so drafts can't become evidence. Retrieval is plain SQL:

```sql
-- The question is embedded once and reused for all three sources.
SELECT fact_id
FROM   document_facts
WHERE  (embedding <=> %(question)s) <= 0.5   -- cosine distance cut-off
ORDER  BY embedding <=> %(question)s
LIMIT  10;
```

The threshold keeps answers honest. Top-k alone always returns k rows, even for an off-topic question, and weak context pushes the model to invent. With the cut-off, an off-topic question gets zero context, and the prompt then requires one fixed refusal sentence that the UI can detect exactly.

There's no ANN index yet. The embeddings have 3,072 dimensions, above pgvector's 2,000-dimension HNSW limit for `vector`, and at one customer's size an exact scan is fast enough.

## The Texas bug

Back to the opening. The supplier search tool took free-text `state` and `country` arguments and matched them case-insensitively against the supplier's address. The database stores codes. The model sent words.

```text
user   : "suppliers in Texas"
model  : search_suppliers(state="Texas", country="United States")
tool   : WHERE lower(state) = 'texas' AND lower(country) = 'united states'
db     : state = 'TX', country = 'US'          -> 0 rows
model  : "There are no suppliers in Texas."
```

The damaging part is the last line. An empty result isn't an error, so nothing failed: the model got a valid empty page and reported it faithfully. A wrong answer in a confident tone is the worst failure a data assistant has.

The easy fix was a prompt rule: "always send two-letter state codes". I didn't take it. A prompt rule works most of the time, for the model you tested, and the router means the model behind this tool can change with a config value. It also does nothing for the next tool with a location field. Model arguments are untrusted input, like a form field from a browser, and you normalize untrusted input where it enters your code. So I normalized at the tool boundary, with lookups built from reference data we already had:

```python
def code_lookup(code_to_label, aliases=None):
    by_label = {label.casefold(): code for code, label in code_to_label.items()}
    by_label |= {a.casefold(): c for a, c in (aliases or {}).items()}

    def normalize(raw):
        value = raw.strip()
        if value.upper() in code_to_label:       # already a code
            return value.upper()
        return by_label.get(value.casefold(), value)  # unknown: pass through
    return normalize

state_code = code_lookup(US_STATES)              # {"TX": "Texas", ...}
country_code = code_lookup(COUNTRIES, aliases={"United States": "US"})

assert state_code(" texas ") == "TX" and state_code("tx") == "TX"
assert country_code("United States") == "US"
assert state_code("Bavaria") == "Bavaria"        # non-US data still works
```

The tool runs its arguments through these before building the query. Three decisions in there are the actual work:

- **Unknown values pass through.** Not every supplier is in the US. "Bavaria" or "Ontario" must reach the query unchanged, so the normalizer only rewrites what it recognizes.
- **The alias table.** Our country reference data labels the US by its long form, "United States of America". A lookup built only from labels would still miss "United States", which is exactly what the model sent. Real model output is the test data, not the reference list.
- **It lives in the tool, not the agent.** The tool is the one place every model, every prompt version and every caller passes through. One guard there covers all of them.

The same tool already applied this idea in two places: it raises a model-readable error if every filter is empty, and rejects an unknown sort key with the allowed values. The question I now ask of every tool argument is "what will a model plausibly send here, and what does the database actually store?"

## Two smaller changes of mine

**A cheaper model for a summarizer.** A feature that writes a short company profile for each supplier from its structured fields was running on a mid-tier model. Turning structured fields into a paragraph needs little reasoning, so I moved it to the lite tier of the same model family. It was a one-line change to the model that summarizer pins, and the token log records the effect per feature.

**Bulk 1:1 supplier chat rooms.** When a buyer emails a batch of suppliers, I made the background job that sends the emails also open a 1:1 chat room between that buyer and each supplier, so replies have somewhere to land. It runs only when the customer has chat enabled, and skips suppliers that already have a room with exactly this user and this supplier, found with one grouped count. My first version created rooms in a loop: one insert for the room and two for its memberships, three queries per supplier. The final version does one bulk insert for the rooms and one per membership table; Postgres returns the new ids from the first, which is what makes the other two possible. Three inserts, whatever the batch size.

## Results

The normalization shipped as about thirty lines across two files. "Suppliers in Texas" and "suppliers in TX" now return the same list, and non-US searches behave as before. I have no dashboard number for "confidently wrong answers avoided", and I'd distrust one, but it closed that failure for state and country filters on every model behind the router.

On the platform side, role-based routing has held up: for every role it covers, a model change is a config change, and the shared token log shows what each feature costs on each vendor.

## What I'd do next

- **A golden set for tools.** A few dozen real phrasings ("Texas", "TX", "the Lone Star State") run through the tools in CI would have caught this bug before a user did.
- **Normalization beyond US states.** The state lookup is US-only on purpose; other regions pass through untouched.
- **Hybrid retrieval.** Supplier search uses Postgres full-text search, questionnaires use vectors, and the two are never combined. Reciprocal rank fusion of both lists is a small SQL change.
- **halfvec and HNSW.** Storing embeddings as `halfvec` raises the index limit to 4,000 dimensions, so an HNSW index becomes possible when exact scans slow down.
- **Everything through the router.** The questionnaire path and that supplier summarizer still name their models in code. Giving each a role would make every model on the platform a config change.
