---
title: Tool calling
section: llm
order: 9
summary: How models call your functions, how to design tool schemas, validate arguments, return errors, run calls in parallel and keep side effects idempotent.
---

Tool calling (function calling) lets a model request that your code run a function: search orders, query a database, create a ticket. The model never executes anything. It emits a structured request; your server validates it, runs it with the user's permissions, and returns the result as another message. That makes tool calling an RPC boundary where the caller is a probabilistic, injectable client. Design it like a public API.

## How it works

### The request-response cycle

```text
1. you send:   messages + tool definitions (name, description, JSON Schema)
2. model says: finish_reason = tool_calls
               [{id: "call_1", name: "get_order", arguments: "..."}]
3. you:        validate args ──► authorize ──► execute ──► serialize result
4. you send:   messages + assistant tool-call turn
               + tool result message (tool_call_id = "call_1")
5. model:      either more tool calls (go to 3) or a final text answer
```

Details that matter:

- **Arguments are generated text.** With strict schemas, providers constrain them to your JSON Schema; without, expect malformed JSON, missing fields and invented enum values.
- **Every tool call has an id.** The result must reference it. Multiple calls in one turn each need a matching result before the next model call; missing results usually cause an API error.
- **`tool_choice`** controls behavior: `auto` (model decides), `required`/`any` (must call some tool), a specific tool (forced), or `none`. Forcing a single tool is a clean way to get structured output.
- **Tool definitions cost tokens on every request.** Twenty verbose tools can add thousands of input tokens per call and degrade selection accuracy.

### Parallel tool calls

Models can emit several independent calls in one turn ("get weather in Paris and in Tokyo"). Execute them concurrently and return all results together. Disable parallel calls (most APIs expose a flag) when tools have ordering dependencies or side effects that should not race.

## In practice

### Schema design

```json
{
  "name": "search_orders",
  "description": "Search the current customer's orders. Use for questions about order status, shipping or past purchases. Returns at most 10 orders, newest first.",
  "parameters": {
    "type": "object",
    "properties": {
      "status": {
        "type": ["string", "null"],
        "enum": ["pending", "shipped", "delivered", "cancelled", null],
        "description": "Filter by status. null for any status."
      },
      "placed_after": {
        "type": ["string", "null"],
        "description": "ISO date, e.g. 2025-01-31. null for no lower bound."
      }
    },
    "required": ["status", "placed_after"],
    "additionalProperties": false
  }
}
```

Guidelines:

- **Descriptions are prompts.** Say when to use the tool, when not to, and what it returns. This drives selection more than the name.
- **Few, coarse-grained tools** beat many tiny ones. `search_orders(status, placed_after)` beats `get_pending_orders`, `get_shipped_orders`, and so on.
- **Enums over free text**, nullable over optional, explicit formats in descriptions.
- **No identity parameters.** Never expose `customer_id` or `tenant_id` as arguments. Take identity from the authenticated request context. Otherwise a prompt injection can ask for someone else's data.
- **Bounded outputs.** Paginate and cap results; return compact JSON with only the fields the model needs. A 50 KB tool result is 10k+ tokens on every subsequent turn.

### Dispatch: validate, authorize, execute

```python
import asyncio, json
from pydantic import BaseModel, ValidationError

class SearchOrdersArgs(BaseModel):
    status: str | None
    placed_after: str | None

TOOLS = {
    "search_orders": (SearchOrdersArgs, search_orders),     # handler(ctx, args)
    "create_refund": (CreateRefundArgs, create_refund),
}

async def run_tool(ctx, call) -> dict:
    spec = TOOLS.get(call.name)
    if spec is None:
        return error(call, f"Unknown tool '{call.name}'. Available: {list(TOOLS)}")
    schema, handler = spec
    try:
        args = schema.model_validate_json(call.arguments)
    except ValidationError as e:
        return error(call, f"Invalid arguments: {e.errors(include_url=False)}")
    try:
        call_ctx = ctx.with_tool_call(call.id)              # for idempotency keys
        result = await asyncio.wait_for(handler(call_ctx, args), timeout=10)
    except PermissionDenied:
        return error(call, "Not permitted for this user.")
    except asyncio.TimeoutError:
        return error(call, "Tool timed out. Try a narrower query.")
    except Exception:
        log.exception("tool failed", extra={"tool": call.name})
        return error(call, "Internal error. Do not retry this call.")
    return {"tool_call_id": call.id, "content": json.dumps(result)[:8000]}

def error(call, msg: str) -> dict:
    return {"tool_call_id": call.id, "content": json.dumps({"error": msg}),
            "is_error": True}

async def run_all(ctx, calls):
    return await asyncio.gather(*(run_tool(ctx, c) for c in calls))
```

`ctx` carries the authenticated user, tenant and permissions; handlers use it, not model arguments, to scope queries.

### Errors go back to the model

Do not raise tool errors to the user on the first failure. Return them as tool results with a clear, actionable message. Models are good at correcting a bad date format or picking a different tool when told what went wrong. Distinguish:

| Error | Message to model | Retry? |
|---|---|---|
| Validation | Exact field and expected format | Yes, model fixes args |
| Not found | "No order ORD-123 for this customer" | Model should ask user |
| Permission | "Not permitted" (no details) | No |
| Timeout / transient | "Temporarily unavailable" | Maybe once, or your code retries |
| Internal | Generic message; details only in logs | No |

Never put stack traces, SQL or secrets in tool results. They go into the context and possibly to the user.

### Idempotency of side effects

The model, your retry logic, or a user resubmitting a conversation can repeat a call. Read-only tools are safe; mutating tools need protection:

```python
async def create_refund(ctx, args: CreateRefundArgs):
    # Same conversation + same tool call id => same refund, never two.
    key = f"{ctx.conversation_id}:{ctx.tool_call_id}"
    existing = await Refund.objects.filter(idempotency_key=key).afirst()
    if existing:
        return existing.as_tool_result()
    ...
```

Back it with a unique constraint on `idempotency_key`. Also consider whether a tool call id is stable across your own retries of the whole model turn (it usually is not), and add a semantic key (`order_id + amount`) for operations that must happen at most once.

> **Rule of thumb:** Any tool that moves money, sends messages, deletes data or changes permissions gets an idempotency key, an audit log entry, and either a confirmation step or a hard policy limit outside the model.

## Pitfalls

- **Trusting model-provided identifiers.** Symptom: users can access other accounts via crafted prompts. Fix: identity from context; authorize every object access.
- **Too many tools.** Symptom: wrong tool chosen, high input token counts. Fix: consolidate, or select a relevant subset of tools per request.
- **Huge tool results.** Symptom: cost and latency grow every turn. Fix: cap, paginate, summarize.
- **Missing tool results for a call id.** Symptom: provider 400 errors on the next turn. Fix: always return a result, even an error, for every call.
- **Racing parallel side effects.** Fix: disable parallel calls for mutating tools, or serialize them in your dispatcher.
- **Retrying non-idempotent tools on timeout.** Fix: idempotency keys and unique constraints.

## Checklist

- Small set of well-described, coarse tools with strict schemas.
- Identity and tenant from request context, never from arguments.
- Validate with Pydantic; return actionable errors as tool results.
- Timeouts per tool; output size caps.
- Idempotency keys and audit logs on every mutating tool.
- Parallel execution only for independent, read-only calls.

## Interview angle

- **Who executes the tool?** Your server. The model only proposes a call; you validate, authorize and run it.
- **How do you handle a bad tool call?** Return a structured error as the tool result so the model can correct itself; cap retries.
- **How do you make tool side effects safe?** Least-privilege handlers scoped by the user's identity, idempotency keys with unique constraints, audit logs, and confirmation for destructive actions.
