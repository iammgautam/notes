---
title: Tool calling
section: llm
order: 21
phase: "Phase 4 · Agents"
summary: How function calling works under the hood, plus tool schema design, argument validation, errors as data, parallel calls and side-effect safety.
---

Tool calling (function calling) lets a model ask your code to run a function: search orders, query a database, issue a refund. The model never executes anything. It emits a structured request; your server validates it, authorizes it against the real user, runs it, and sends the result back as another message. Every tool is therefore an RPC endpoint whose caller is a probabilistic client that can hallucinate and can be steered by prompt injection. Design tools like a public API and treat the dispatcher as a trust boundary.

## How it works

### The model only emits tokens

There is no separate function-calling mechanism inside the network. Three pieces make it look like one:

1. **The chat template renders tool definitions into the prompt.** Your JSON Schemas become text in the system section. Every request pays input tokens for them.
2. **The model was fine-tuned to emit a call format.** Hermes-style and Qwen models wrap calls in `<tool_call>...</tool_call>` tags, Mistral models emit a `[TOOL_CALLS]` token, Llama 3.1 uses an `ipython` role for tool results.
3. **The inference server parses the output.** It detects the marker, parses the JSON, and returns structured `tool_calls` with a "tool use" stop reason.

```text
<|im_start|>system
<tools>
{"name": "search_orders", "description": "Search the current
 customer's orders...", "parameters": {"type": "object", ...}}
</tools><|im_end|>
<|im_start|>user
Where is my March order?<|im_end|>
<|im_start|>assistant
<tool_call>
{"name": "search_orders", "arguments": {"placed_after": "2025-03-01"}}
</tool_call><|im_end|>
<|im_start|>tool
{"orders": [{"id": "A2", "status": "shipped"}]}<|im_end|>
```

That is an illustrative ChatML/Hermes-style rendering; exact tokens vary by model. Two consequences: tool descriptions are prompt text, so they steer selection like instructions do; and tool results are input the model reads, so a fetched web page or customer-written ticket can carry injected instructions. See [Security and guardrails]({{ '/llm/security-and-guardrails/' | relative_url }}).

With open-weight models you can print this: transformers' `apply_chat_template(messages, tools=[fn])` turns a typed, docstringed Python function into a schema and renders it. Serving with vLLM, parsing is a flag: `--enable-auto-tool-choice --tool-call-parser hermes`. A mismatched parser is a common reason an open model "doesn't support tools": it emits calls that nothing parses.

### Strict mode is constrained decoding

Unconstrained arguments are free-form text: expect malformed JSON, missing fields, invented enum values. Strict modes (OpenAI's `strict: true`, and similar options elsewhere) compile your schema into a grammar and mask tokens that would break it during sampling, the same mechanism as [Structured outputs]({{ '/llm/structured-outputs/' | relative_url }}). The price is a restricted schema dialect: OpenAI strict mode requires every property in `required` and `additionalProperties: false`, so optional fields become nullable (`"type": ["string", "null"]`).

Strict mode guarantees shape, not meaning. A well-formed `order_id` can still belong to another customer. Validation and authorization stay in your code.

### The request-response cycle

```text
 1. you  -> model : messages + tool definitions + tool_choice
 2. model -> you  : stop_reason = tool use
                    [{id: "call_1", name: "search_orders",
                      arguments: {...}}, {id: "call_2", ...}]
 3. you           : for each call
                    parse -> validate -> normalize -> authorize
                    -> execute (timeout) -> result or error
 4. you  -> model : history + assistant tool-call turn
                    + one tool result per call id
 5. model         : more calls (go to 3) or a final answer
```

Wire formats differ by provider (as of 2025–2026):

| | OpenAI Chat Completions | Anthropic Messages |
|---|---|---|
| Define | `tools[].function.parameters` | `tools[].input_schema` |
| Call | `tool_calls[]`, arguments as a JSON string | `tool_use` block, `input` as an object |
| Stop signal | `finish_reason: "tool_calls"` | `stop_reason: "tool_use"` |
| Result | `role: "tool"` with `tool_call_id` | `tool_result` block with `tool_use_id`, optional `is_error` |

Gemini uses `functionCall` and `functionResponse` parts. An internal message format with one adapter per provider keeps the dispatcher neutral; see [Multi-provider routing]({{ '/llm/multi-provider-routing/' | relative_url }}).

Every call id needs exactly one result in the next request, or most providers return a 400. The dispatcher must answer every call, including ones it refused.

## Designing tool schemas

Names and descriptions drive selection; parameter schemas drive argument quality. They are your only levers before execution. The demo below shows full schemas.

| Element | Do | Avoid |
|---|---|---|
| Name | `verb_noun`, namespaced at scale: `billing_refund_order` | `do_action`, near-duplicates |
| Description | What, when to use, when not to, what it returns | Restating the name |
| Granularity | Few coarse tools with filters | `get_pending_orders`, `get_shipped_orders` |
| Types | Enums, integer ranges, explicit formats | Free text where the set is known |
| Identity | From the session | `user_id` or `tenant_id` parameters |
| Output | Compact JSON, stable IDs, "10 of 240 shown" | ORM dumps, stack traces, 50 KB blobs |

Output design matters as much as input: a result is re-sent every later turn, so a 40 KB result costs about 10k tokens per step for the rest of the run. Project fields, paginate, and say how to narrow.

## Validation and normalization at the trust boundary

Arguments are untrusted input. Every call goes through the same pipeline:

1. **Parse** JSON; reject non-objects.
2. **Validate** required fields, types, ranges, enums; reject unknown fields (Pydantic with `extra="forbid"`).
3. **Normalize** unambiguous variants: trim whitespace, case-fold enums (`"Shipped"` to `"shipped"`), dates to ISO, `"5"` to `5`, units, known synonyms.
4. **Resolve references.** Names to IDs by lookup. Zero or several matches go back as an error listing candidates; never pick silently.
5. **Authorize** the object for the authenticated user, on every call.
6. **Clamp** to your own limits even if the schema already says so.

`"last week"` in a date field has no single right answer, so it gets an error with the expected format, not a guess.

## Errors are data

A tool failure is information for the model, not an exception for the user. Return it as the tool result, saying what to do next. Models reliably fix a date format or switch tools once told precisely what was wrong.

| Error class | Message to the model | Who retries |
|---|---|---|
| Invalid arguments | Field, expected format, value received | Model, with fixed args |
| Unknown tool | "Unknown tool X. Available: [...]" | Model |
| Not found | "No order ORD-9 for this customer" | Model asks user or searches |
| Permission | Same text as not found, or bare "not permitted" | Nobody |
| Transient | "Temporarily unavailable" | Your code once with backoff, then model |
| Internal bug | "Internal error, do not retry" | Nobody; details in logs |

Never put stack traces, SQL or secrets in a result: it enters the context and may be echoed to the user. Transient retries are covered in [Reliability patterns]({{ '/llm/reliability-patterns/' | relative_url }}).

## Parallel tool calls

Models can emit several independent calls in one turn. Run reads concurrently and return all results together; each avoided round trip saves 1–3 seconds. Run writes one at a time in emitted order, since two refunds on one order must not race. When calls depend on each other, disable parallelism: OpenAI's `parallel_tool_calls: false`, Anthropic's `disable_parallel_tool_use` inside `tool_choice`.

## Tool choice

| Mode | OpenAI | Anthropic | Use for |
|---|---|---|---|
| Model decides | `"auto"` | `{"type": "auto"}` | Normal agent turns |
| Must call a tool | `"required"` | `{"type": "any"}` | Mandatory lookup; agents that end via `submit` |
| Specific tool | `{"type": "function", "function": {"name": ...}}` | `{"type": "tool", "name": ...}` | Extraction, fixed workflow steps |
| No tools | `"none"` | `{"type": "none"}` | Forcing a final answer at budget exhaustion |

> **Gotcha:** Forcing `required` on every turn means the model can never stop with text. Pair it with an explicit `finish` tool or switch to `auto` after step one. Some reasoning modes restrict forced tool choice; check your provider.

## Side-effect safety

Tools that move money, send messages, delete data or change permissions need protection that does not depend on the model behaving.

- **Idempotency keys.** The same call arrives twice when the model repeats itself, your code retries a timeout, or the user resubmits. Back a key with a unique constraint. A provider call id is stable within one response but not across a regenerated turn, so use a semantic key (conversation, tool, canonical arguments) for at-most-once operations. See [Idempotency and retries]({{ '/hld/idempotency-and-retries/' | relative_url }}).
- **Two-phase confirmation.** The tool first returns a dry-run preview with the exact arguments. Your UI shows it; on approval your code executes the stored arguments rather than asking the model to call again.
- **Policy limits in code.** "Refunds above $100 need a human" is an `if` in the handler, not a sentence in the prompt.
- **Separate read and write tools** so writes can be exposed narrowly; audit-log every write; prefer reversible operations (drafts over sends, holds over charges).

## Permissions per user

You need both layers:

- **Visibility.** Filter the tool list per user and role before sending it. A customer never sees `set_order_status`; hidden tools cannot be chosen and cost no tokens.
- **Authorization.** Every handler re-checks access using identity from the request context. If the model can pass `customer_id`, an injection can request someone else's data.

Run handlers with the user's scoped credentials (delegated OAuth tokens, row-level security), not a superuser account, so a confused model can do no more than the user could. The same holds for [MCP]({{ '/llm/mcp/' | relative_url }}) tools.

## How many tools

A definition is typically 100–400 tokens. Forty tools at 250 tokens is 10k input tokens per call, 100k over a ten-step run. Selection accuracy drops as the set grows, especially with overlapping descriptions, long before any API limit.

| Strategy | How | When |
|---|---|---|
| Consolidate | Merge near-duplicates into one tool with filters | Always first |
| Static subsets | A router picks a toolset per intent | Clear product areas |
| Tool retrieval | Embed descriptions plus example queries; top-k per request; core tools always in | Dozens to hundreds |
| Tool search tool | A `search_tools(query)` tool loads definitions on demand | Large catalogs |
| Hierarchy | Sub-agents own small toolsets | Broad domains; see [Agent architectures]({{ '/llm/agent-architectures/' | relative_url }}) |

> **In production:** Tool definitions are at the start of the prompt, so they are part of the cached prefix. Changing or reordering the list mid-run invalidates prompt caching. Keep it stable per run and sorted. See [LLM caching]({{ '/llm/llm-caching/' | relative_url }}).

## In practice: a tool dispatcher

Runnable, stdlib only. It shows per-role visibility, validation with normalization, identity from context, errors as data, concurrent reads with sequential writes, idempotency, and confirmation with a dry-run preview. A scripted fake model plays the LLM, including a malformed date and a tool the user cannot see.

```python
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date

ORDERS = {"A2": {"user": "u1", "status": "shipped", "placed": "2025-03-02", "total": 1999},
          "B1": {"user": "u2", "status": "pending", "placed": "2025-03-05", "total": 9900}}
REFUNDS = []


class ToolError(Exception):
    """Shown to the model: actionable, no internals."""


@dataclass(frozen=True)
class Ctx:  # from the authenticated session, never from model output
    user_id: str
    role: str
    conversation_id: str


@dataclass
class Tool:
    fn: object
    params: dict  # JSON Schema subset; also what the model sees
    roles: tuple = ("customer", "staff")
    mutating: bool = False
    confirm: bool = False


def validate(schema, raw):
    try:
        args = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ToolError(f"arguments are not valid JSON ({e.msg})") from None
    props = schema["properties"]
    if not isinstance(args, dict) or set(args) - set(props):
        raise ToolError(f"arguments must be an object with fields from {sorted(props)}")
    out = {}
    for name, spec in props.items():
        v = args.get(name)
        if v is None:
            if name in schema["required"]:
                raise ToolError(f"'{name}' is required")
            out[name] = spec.get("default")
        elif spec["type"] == "integer":
            v = int(v) if isinstance(v, str) and v.isdigit() else v  # "5" -> 5
            if type(v) is not int or not spec["minimum"] <= v <= spec["maximum"]:
                raise ToolError(f"'{name}' must be an integer in [{spec['minimum']}, {spec['maximum']}]")
            out[name] = v
        else:
            if not isinstance(v, str):
                raise ToolError(f"'{name}' must be a string")
            v = v.strip().lower() if "enum" in spec else v.strip()  # "Shipped" -> "shipped"
            if "enum" in spec and v not in spec["enum"]:
                raise ToolError(f"'{name}' must be one of {spec['enum']}, got {args[name]!r}")
            if spec.get("format") == "date":
                try:
                    v = date.fromisoformat(v).isoformat()
                except ValueError:
                    raise ToolError(f"'{name}' must be an ISO date like 2025-01-31, got {v!r}") from None
            out[name] = v
    return out


def search_orders(ctx, status, placed_after, limit):
    """Search the signed-in customer's orders."""
    rows = [{"id": oid, "status": o["status"]} for oid, o in ORDERS.items()
            if o["user"] == ctx.user_id  # scope from ctx, not arguments
            and status in (None, o["status"])
            and (placed_after is None or o["placed"] > placed_after)]
    return {"orders": rows[:limit], "total": len(rows)}


def refund_order(ctx, order_id, amount_cents, dry_run=False):
    """Refund part or all of one order. Needs user confirmation."""
    o = ORDERS.get(order_id)
    if o is None or o["user"] != ctx.user_id:  # one message: no existence leak
        raise ToolError(f"no order {order_id} for this customer")
    if amount_cents > o["total"]:
        raise ToolError(f"amount exceeds order total of {o['total']} cents")
    if dry_run:
        return {"would_refund_cents": amount_cents}
    REFUNDS.append((order_id, amount_cents))
    return {"refund_id": f"R{len(REFUNDS)}"}


def set_order_status(ctx, order_id, status):
    """Staff only: override an order's status."""
    ORDERS[order_id]["status"] = status
    return {"ok": True}


def obj(required, **props):
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


STR = {"type": "string"}
TOOLS = {t.fn.__name__: t for t in [
    Tool(search_orders, obj([], status={"type": "string", "enum": ["pending", "shipped"]},
                            placed_after={"type": "string", "format": "date"},
                            limit={"type": "integer", "minimum": 1, "maximum": 20, "default": 10})),
    Tool(refund_order, obj(["order_id", "amount_cents"], order_id=STR,
                           amount_cents={"type": "integer", "minimum": 1, "maximum": 100_000}),
         mutating=True, confirm=True),
    Tool(set_order_status, obj(["order_id", "status"], order_id=STR,
                               status={"type": "string", "enum": ["cancelled"]}),
         roles=("staff",), mutating=True),
]}


class Dispatcher:
    def __init__(self, tools):
        self.tools, self.done, self.pending = tools, {}, {}  # done: idempotency key -> result

    def schemas(self, ctx):  # per-role visibility: this list is sent to the model
        return [{"name": n, "description": t.fn.__doc__, "parameters": t.params}
                for n, t in self.tools.items() if ctx.role in t.roles]

    def run_one(self, ctx, call):
        tool = self.tools.get(call["name"])
        try:
            if tool is None or ctx.role not in tool.roles:  # hidden == nonexistent
                names = [s["name"] for s in self.schemas(ctx)]
                raise ToolError(f"unknown tool {call['name']!r}; available: {names}")
            args = validate(tool.params, call["arguments"])
            key = f"{ctx.conversation_id}:{call['name']}:{json.dumps(args, sort_keys=True)}"
            if key in self.done:
                out = {**self.done[key], "note": "already done; not repeated"}
            elif tool.confirm:
                preview = tool.fn(ctx, dry_run=True, **args)  # every check, no side effect
                self.pending[key] = (ctx.user_id, tool, args)
                out = {"status": "needs_confirmation", "action_key": key, "preview": preview}
            else:
                out = tool.fn(ctx, **args)
                if tool.mutating:
                    self.done[key] = out
        except ToolError as e:
            out = {"error": str(e)}
        except Exception:  # log.exception(...): details to logs, never to the model
            out = {"error": "internal error; do not retry"}
        return {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(out)}

    def run_all(self, ctx, calls):
        """Reads run concurrently; writes run one at a time, in emitted order."""
        writes = [c for c in calls if getattr(self.tools.get(c["name"]), "mutating", False)]
        reads = [c for c in calls if c not in writes]
        with ThreadPoolExecutor(8) as pool:
            results = dict(zip([c["id"] for c in reads], pool.map(lambda c: self.run_one(ctx, c), reads)))
        results |= {c["id"]: self.run_one(ctx, c) for c in writes}
        return [results[c["id"]] for c in calls]  # one result per call id

    def confirm(self, ctx, key):
        """User clicked Confirm: execute the stored arguments, not a new model call."""
        user, tool, args = self.pending.pop(key)
        if user != ctx.user_id:
            raise PermissionError("action belongs to another user")
        self.done[key] = tool.fn(ctx, **args)
        return self.done[key]


def call(cid, name, **args):
    return {"id": cid, "name": name, "arguments": json.dumps(args)}


if __name__ == "__main__":
    d, alice = Dispatcher(TOOLS), Ctx("u1", "customer", "c1")
    assert [s["name"] for s in d.schemas(alice)] == ["search_orders", "refund_order"]

    script = [  # scripted fake model: one entry per assistant turn
        [call("t1", "search_orders", status="Shipped", placed_after="March 1"),
         call("t2", "set_order_status", order_id="A2", status="cancelled")],
        [call("t3", "search_orders", status="Shipped", placed_after="2025-03-01", limit="5")],
        [call("t4", "refund_order", order_id="B1", amount_cents=100),
         call("t5", "refund_order", order_id="A2", amount_cents=1999)],
        "Order A2 has shipped. Confirm below and I will refund it.",
    ]
    messages = []
    for turn in script:
        if isinstance(turn, str):
            messages.append({"role": "assistant", "content": turn})
            break
        messages += [{"role": "assistant", "tool_calls": turn}, *d.run_all(alice, turn)]
    out = {m["tool_call_id"]: json.loads(m["content"]) for m in messages if m["role"] == "tool"}

    assert "ISO date" in out["t1"]["error"]
    assert "unknown tool" in out["t2"]["error"] and ORDERS["A2"]["status"] == "shipped"
    assert out["t3"] == {"orders": [{"id": "A2", "status": "shipped"}], "total": 1}
    assert out["t4"]["error"] == "no order B1 for this customer"
    assert out["t5"]["status"] == "needs_confirmation" and REFUNDS == []

    d.confirm(alice, out["t5"]["action_key"])  # executes the stored args
    again = d.run_all(alice, [call("t6", "refund_order", order_id="A2", amount_cents=1999)])
    assert "already done" in again[0]["content"] and REFUNDS == [("A2", 1999)]
    print("tool dispatcher demo: ok")
```

In a real service: Pydantic instead of the hand-rolled validator, `done` and `pending` as tables with unique keys, per-tool timeouts, and a trace span per call ([Observability and tracing]({{ '/llm/observability-and-tracing/' | relative_url }})).

## Pitfalls

- **Trusting model-provided identifiers.** Symptom: a crafted prompt reads another account. Fix: identity from context, object-level checks, row-level security underneath.
- **Too many overlapping tools.** Symptom: wrong-tool rate and input tokens climb. Fix: consolidate, route, or retrieve tools; measure selection accuracy in [agent evaluation]({{ '/llm/agent-evaluation/' | relative_url }}).
- **Oversized results.** Symptom: cost and latency grow every step; details get lost in long JSON. Fix: project, paginate, cap.
- **Racing writes.** Symptom: duplicate refunds when two writes arrive in one turn. Fix: sequential writes, idempotency keys, constraints.

## Checklist

- Few coarse tools, strict schemas, descriptions that say when not to use them.
- Parse, validate, normalize, resolve, authorize, clamp on every call.
- Identity from the session; per-role visibility plus per-call authorization.
- Classified errors as results; one result per call id; sequential writes.
- Idempotency keys, confirmation and code-level limits on destructive actions.
- Stable, sorted tool list per run.

## Interview questions

**Does the model execute the function?** No. It generates a structured request; your code validates, authorizes, executes and returns the result as a message.

**How does a model know how to call tools?** The chat template renders schemas into the prompt, fine-tuning taught the model a call format (special tokens or tagged JSON), and the server parses that output into structured calls. Strict mode adds constrained decoding.

**What does strict mode not protect you from?** Semantic errors and abuse: a valid ID for the wrong customer, an unrequested refund.

**The model sends invalid arguments. What happens?** The dispatcher returns a tool result naming the field and expected format; the model usually fixes it next turn. Cap repeated failures.

**How do you make a refund tool safe?** Session identity, ownership and amount checks, an idempotency key on a unique constraint, a dry-run preview with user confirmation that executes stored arguments, code-level limits, an audit log.

**You have 300 internal APIs. How do you expose them?** Consolidate, then select per request: route by intent to small toolsets, retrieve top-k definitions by embedding, or offer a tool-search tool. Keep the per-run set stable for caching.

## Further reading

- Toolformer: Language Models Can Teach Themselves to Use Tools (Schick et al., 2023).
- Gorilla: Large Language Model Connected with Massive APIs (Patil et al., 2023), and the Berkeley Function Calling Leaderboard.
- ToolLLM: Facilitating Large Language Models to Master 16000+ Real-world APIs (Qin et al., 2023).
- OpenAI function calling guide and Anthropic tool use documentation.
- Writing effective tools for agents (Anthropic engineering blog, 2025).
