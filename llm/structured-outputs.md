---
title: Structured outputs
section: llm
order: 9
phase: "Phase 2 · Prompting and context"
summary: JSON mode vs schema-constrained decoding, grammars, function calling, Pydantic validation, repair loops and streaming partial JSON for reliable extraction.
---

A backend can only use an LLM response it can parse. Treat the model like an unreliable upstream service: decide what guarantee you buy from the provider (valid JSON, schema conformance or nothing), design a schema the model can fill accurately, and own a validation layer for what the guarantee doesn't cover. Writing the instructions is covered in [Prompt engineering]({{ '/llm/prompting-and-structured-outputs/' | relative_url }}). Tool arguments are the same problem in another form, covered in [Tool calling]({{ '/llm/tool-calling/' | relative_url }}).

## Levels of guarantee

| Level | Mechanism | Guarantees | Does not guarantee |
|---|---|---|---|
| Prompt-only | "Return JSON with keys..." | Nothing | Valid JSON, keys, types |
| JSON mode | Decoder restricted to JSON | Parseable JSON (unless truncated) | Your keys, types, enums |
| Function calling, non-strict | Trained to emit schema-shaped args | Usually the right shape | Conformance |
| Strict schema | Constrained decoding on your schema | Conforms to the supported subset | Correct values, business rules, completion |
| Local grammar | Regex or CFG on your own server | Any grammar you can write | Same as above |

Use the strongest level available and still validate. A JSON-mode trap: told nothing about JSON, a constrained model can emit whitespace until the token limit, which is why one provider requires the word "JSON" in the messages.

## How constrained decoding works

At each step the model produces logits over the vocabulary (100k+ tokens). A constraint engine tracks the position in the grammar and computes which tokens may legally come next. Every other token gets logit `-inf`, so sampling renormalizes over the allowed tokens. See [Decoding and sampling]({{ '/llm/decoding-and-sampling/' | relative_url }}) for the sampling step itself.

```text
 output so far --+--> model forward ------> logits over vocab (V)
                 |                                  |
                 +--> grammar state (FSM or PDA)    v
                        |                 logit = -inf if not allowed,
                        +-- allowed ids --> renormalize, sample token
                                                    |
          append token, advance grammar state <-----+
```

Details that matter in practice:

- **Schema to automaton.** A non-recursive JSON Schema compiles to a regex, i.e. a finite-state machine. Outlines precomputes, per FSM state, the vocabulary tokens that keep it valid, so each step is a lookup (Willard and Louf, 2023). Recursive structures need a context-free grammar and a pushdown automaton; XGrammar (Dong et al., 2024) makes that fast by precomputing masks for context-independent tokens. vLLM, SGLang and TensorRT-LLM ship such engines; llama.cpp uses GBNF grammars.
- **Tokens don't align with grammar symbols.** One token can be `"},`. Engines must check whole token strings, or they distort probabilities.
- **Compile cost.** New schemas are compiled (milliseconds to seconds) and cached, so the first request with a new schema is slower. Per-request schemas with dynamic enums pay it every time.
- **The mask removes options, it doesn't add knowledge.** If the honest answer is illegal, the model is pushed into the closest legal output: valid, confident and wrong. Give enums an `other` or `unknown` member.
- **Field order is generation order.** Put evidence or reasoning fields before the conclusions that depend on them.
- **Format pressure can cost accuracy.** Strict formats have been measured to reduce reasoning performance on some tasks (Tam et al., 2024). Reason in free text first and extract in a second call, or put a reasoning field first.

A toy version of the mechanism. The fake model prefers chatty tokens, and the mask forces a valid label:

```python
import math

LOGITS = {"Sure": 3.0, "!": 2.5, "Pos": 2.0, "pos": 1.5, "itive": 1.4,
          "neg": 0.5, "ative": 0.4, "neutral": 0.3, " ": 0.1}
CHOICES = ["positive", "negative", "neutral"]


def allowed(prefix: str) -> list[str]:
    """Tokens that keep the output a prefix of some valid choice."""
    return [t for t in LOGITS if any(c.startswith(prefix + t) for c in CHOICES)]


def softmax(scores: dict[str, float]) -> dict[str, float]:
    z = sum(math.exp(v) for v in scores.values())
    return {k: math.exp(v) / z for k, v in scores.items()}


def decode(constrained: bool, max_steps: int = 5) -> str:
    out = ""
    for _ in range(max_steps):
        options = allowed(out) if constrained else list(LOGITS)
        probs = softmax({t: LOGITS[t] for t in options})
        out += max(probs, key=probs.get)  # greedy
        if out in CHOICES:
            break
    return out


if __name__ == "__main__":
    free = decode(constrained=False)
    forced = decode(constrained=True)
    assert free not in CHOICES          # "SureSureSure..."
    assert forced == "positive"         # pos + itive
    step1 = softmax({t: LOGITS[t] for t in allowed("")})  # renormalized
    assert set(step1) == {"pos", "neg", "neutral"}
    print(f"free={free!r} constrained={forced!r} p(pos)={step1['pos']:.2f}")
```

## Function calling as structured output

A tool's arguments are defined by a JSON Schema. Define one tool such as `record_invoice`, have the model call it, and the arguments are your structured output: the standard extraction trick before native structured outputs. Strict tool modes constrain the arguments. Some newer models reject forcing a tool via `tool_choice`, so for pure extraction prefer the native structured-output parameter.

## Provider and library options

As of 2025 to 2026 (check current docs, these surfaces move):

| Where | How you ask for a schema | Notes |
|---|---|---|
| OpenAI | `response_format` with `type: "json_schema"`, `strict: true`; SDK `parse()` with Pydantic | All properties `required`, `additionalProperties: false` |
| Anthropic | `output_config.format` with a JSON Schema; `strict: true` on tools | SDK `messages.parse()` validates into Pydantic |
| Google Gemini | `response_mime_type="application/json"` plus `response_schema` | Subset of OpenAPI schema |
| vLLM, SGLang | OpenAI-compatible `response_format`; regex, choice, grammar | xgrammar or outlines underneath |
| llama.cpp | GBNF grammar (`--grammar-file`), or JSON Schema converted to GBNF | Local models |
| outlines | `generate.json`, `generate.regex`, `generate.choice` | 1.x renamed the entry points |
| instructor | Wraps a client; `response_model=` plus `max_retries=` | Validate-and-retry built in |

Illustrative OpenAI call with a Pydantic model (older SDKs expose `parse` under `client.beta.chat.completions`):

```python
from typing import Literal
from openai import OpenAI
from pydantic import BaseModel

class Ticket(BaseModel):
    category: Literal["billing", "bug", "account", "other"]
    priority: Literal["low", "normal", "high", "urgent"]
    summary: str

client = OpenAI()
completion = client.chat.completions.parse(
    model="gpt-4o-mini",
    messages=[{"role": "system", "content": TRIAGE_PROMPT},
              {"role": "user", "content": ticket_body}],
    response_format=Ticket,
)
message = completion.choices[0].message
if message.refusal:
    handle_refusal(message.refusal)       # not a validation error, don't retry
else:
    ticket = message.parsed               # a Ticket instance
```

A GBNF grammar for llama.cpp that only admits one JSON shape:

```text
root  ::= "{" ws sent "," ws score ws "}"
sent  ::= "\"sentiment\":" ws label
score ::= "\"score\":" ws [1-5]
label ::= "\"positive\"" | "\"negative\"" | "\"neutral\""
ws    ::= [ \t\n]*
```

## Schema design

The schema is part of the prompt. Models read field names and descriptions as instructions.

- **Nullable, not optional.** Strict modes usually require every property in `required`, so "may be absent" becomes `"type": ["string", "null"]`. In Pydantic, `Optional[str]` without a default is required but nullable; adding `= None` makes it optional, which strict modes may reject unless the SDK rewrites it.
- **Enums for closed sets.** `Literal[...]` becomes `enum`. Add `other` or `unknown`. A nullable enum lists `null` in both `enum` and `type`.
- **An explicit escape hatch.** `is_invoice: bool` or a `status` enum with `not_applicable`. Without one the model fills fields for documents that have none.
- **Descriptions and names carry the rules.** "Grand total in cents. Null if no total is printed." `total_amount_cents` beats `amt`.
- **Integers in minor units, ISO strings for dates.** No floats for money. Check dates in code.
- **Flat, small, bounded.** Every field is a chance to hallucinate. Deep nesting hurts accuracy and can exceed provider limits. Cap list lengths, or outputs hit `max_tokens`.

### Provider schema limitations

Strict modes support a subset of JSON Schema that varies by provider and changes over time. Common gaps: many `format` values, `pattern`, numeric and length bounds (`minimum`, `maxLength`), `oneOf`/`allOf`, recursive `$ref`, conditionals, `default`, and limits on nesting depth, property count and enum size. Unsupported keywords are rejected at request time or **silently ignored**, so test each one you rely on. Whatever the provider can't enforce, your validator does.

## Validation and repair

Even with constrained decoding you validate: schemas can't express business rules (totals add up, ids exist), output can be truncated, the model can refuse, and keywords may be ignored. The order of checks matters:

```text
 raw response
     |
     +-- finish_reason == length --> Truncated: paginate, no repair
     +-- refusal -------------------> refusal path, no retry
     |
 local repair: strip code fences, cut to the outermost {...}
     |
 json.loads ---- fails -----+
     |                      |
 schema check -- fails -----+--> send the exact errors back to the
     |                      |    model, bounded attempts, then a
 business rules - fail -----+    fallback (bigger model, human queue)
     |
 accept
```

Runnable: a minimal JSON Schema validator, business rules (including an evidence quote that must exist in the source) and a repair loop against a scripted fake model.

```python
import json
import re

SCHEMA = {
    "type": "object",
    "properties": {
        "vendor": {"type": "string"},
        "currency": {"type": ["string", "null"], "enum": ["USD", "EUR", "INR", None]},
        "total_cents": {"type": "integer", "minimum": 0},
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "amount_cents": {"type": "integer", "minimum": 0},
                },
                "required": ["description", "amount_cents"],
                "additionalProperties": False,
            },
        },
        "evidence": {"type": "string"},
    },
    "required": ["vendor", "currency", "total_cents", "line_items", "evidence"],
    "additionalProperties": False,
}

TYPES = {"object": dict, "array": list, "string": str, "integer": int,
         "number": (int, float), "boolean": bool, "null": type(None)}


def type_ok(value, name):
    if name in ("integer", "number") and isinstance(value, bool):
        return False                      # bool is a subclass of int
    return isinstance(value, TYPES[name])


def validate(value, schema, path="$"):
    """Subset of JSON Schema: type, enum, minimum, properties, items."""
    names = schema.get("type", [])
    names = names if isinstance(names, list) else [names]
    if names and not any(type_ok(value, n) for n in names):
        return [f"{path}: expected {' or '.join(names)}, got {type(value).__name__}"]
    errors = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if type_ok(value, "number") and value < schema.get("minimum", value):
        errors.append(f"{path}: below minimum {schema['minimum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        errors += [f"{path}: missing required field '{k}'"
                   for k in schema.get("required", []) if k not in value]
        if schema.get("additionalProperties") is False:
            errors += [f"{path}: unexpected field '{k}'"
                       for k in sorted(value.keys() - props.keys())]
        for key, sub in props.items():
            if key in value:
                errors += validate(value[key], sub, f"{path}.{key}")
    if isinstance(value, list):
        for i, item in enumerate(value):
            errors += validate(item, schema.get("items", {}), f"{path}[{i}]")
    return errors


def business_errors(data, source):
    errors = []
    items_total = sum(i["amount_cents"] for i in data["line_items"])
    if data["line_items"] and items_total != data["total_cents"]:
        errors.append(f"$.total_cents: {data['total_cents']} does not equal "
                      f"the sum of line items ({items_total})")
    if data["evidence"] not in source:
        errors.append("$.evidence: must be an exact quote from the document")
    return errors


FENCE = re.compile(r"`{3}(?:json)?\s*(.*?)`{3}", re.S)


def extract_json(text):
    """Local repair before asking the model: fences, surrounding prose."""
    match = FENCE.search(text)
    if match:
        text = match.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object found")
    return json.loads(text[start:end + 1])


class Truncated(Exception):
    pass


def extract_with_repair(model, document, max_attempts=3):
    messages = [{"role": "user", "content":
                 f"Extract the invoice as JSON.\n<doc>\n{document}\n</doc>"}]
    for attempt in range(1, max_attempts + 1):
        reply = model(messages)
        if reply["finish_reason"] == "length":
            raise Truncated("hit max_tokens: raise the limit or paginate")
        try:
            data = extract_json(reply["text"])
            errors = validate(data, SCHEMA) or business_errors(data, document)
        except ValueError as exc:         # JSONDecodeError is a ValueError
            errors = [f"not parseable as JSON: {exc}"]
        if not errors:
            return data, attempt
        messages += [
            {"role": "assistant", "content": reply["text"]},
            {"role": "user", "content": "Your output failed validation:\n- "
             + "\n- ".join(errors) + "\nReturn only the corrected JSON object."},
        ]
    raise ValueError(f"gave up after {max_attempts} attempts: {errors}")


DOC = """ACME Cloud Ltd. Invoice 2291
Hosting (March) .......... $120.00
Support plan ............. $30.00
Total due: $150.00 USD"""

GOOD = {
    "vendor": "ACME Cloud Ltd.", "currency": "USD", "total_cents": 15000,
    "line_items": [{"description": "Hosting (March)", "amount_cents": 12000},
                   {"description": "Support plan", "amount_cents": 3000}],
    "evidence": "Total due: $150.00 USD",
}


def scripted(replies):
    """Fake model: returns canned replies, records what it was told."""
    seen = []
    def model(messages):
        seen.append(messages[-1]["content"])
        return replies[len(seen) - 1]
    return model, seen


if __name__ == "__main__":
    ticks = "`" * 3
    bad_enum = {**GOOD, "currency": "usd"}
    bad_total = {**GOOD, "total_cents": 1500}
    model, seen = scripted([
        {"finish_reason": "stop", "text":
         f"Sure! Here it is:\n{ticks}json\n{json.dumps(bad_enum)}\n{ticks}\nThanks"},
        {"finish_reason": "stop", "text": json.dumps(bad_total)},
        {"finish_reason": "stop", "text": json.dumps(GOOD)},
    ])
    data, attempts = extract_with_repair(model, DOC)
    assert data == GOOD and attempts == 3
    assert "'usd' is not one of" in seen[1]          # schema error fed back
    assert "sum of line items (15000)" in seen[2]    # business rule fed back

    cut = scripted([{"finish_reason": "length", "text": '{"vendor": "AC'}])[0]
    try:
        extract_with_repair(cut, DOC)
        raise AssertionError("expected Truncated")
    except Truncated:
        pass

    assert validate(True, {"type": "integer"})       # bool is not an integer
    assert validate({**GOOD, "notes": "x"}, SCHEMA) == ["$: unexpected field 'notes'"]
    print("repaired in", attempts, "attempts")
```

The same contract in Pydantic, which is what you'd use in a real service (illustrative):

```python
from typing import Literal
from pydantic import BaseModel, Field, model_validator

class LineItem(BaseModel):
    description: str
    amount_cents: int = Field(ge=0)

class Invoice(BaseModel):
    vendor: str = Field(max_length=80, description="Legal name as printed")
    currency: Literal["USD", "EUR", "INR"] | None
    total_cents: int = Field(ge=0, description="Grand total in cents")
    line_items: list[LineItem] = Field(max_length=20)
    evidence: str = Field(description="Exact quote containing the total")

    @model_validator(mode="after")
    def totals_match(self):
        if self.line_items and sum(i.amount_cents for i in self.line_items) != self.total_cents:
            raise ValueError("total_cents must equal the sum of line_items")
        return self

schema = Invoice.model_json_schema()      # one source of truth for the provider
invoice = Invoice.model_validate_json(raw_text)  # ValidationError on failure
# evidence check: pass context={"source": doc} and read info.context
# inside a field_validator
```

`ValidationError` messages include field paths, which is what you feed back in a repair turn. instructor packages the same loop: pass `response_model=Invoice` and `max_retries=2` and it re-asks with the validation errors.

Repair loop rules: two or three attempts at most, the exact error text in every retry (a blind resend at temperature 0 repeats the mistake), local repair before model repair, and a defined fallback (stronger model, human queue, dead-letter table).

## Streaming partial JSON

Long extractions take seconds, and a UI can show fields as they arrive. Tool-call arguments stream the same way: OpenAI streams `function.arguments` deltas, Anthropic streams `input_json_delta` events with `partial_json` strings. Parse the concatenated buffer best-effort with `pydantic_core.from_json(buf, allow_partial=True)`, SDK helpers, or a closer like the one below. A field is final only once the parser has moved past it (`"ACME Cl"` becomes `"ACME Cloud Ltd."`, `12` becomes `120`): no side effects on partial values, and validate the final object.

```python
import json


def close_partial(buf: str) -> str:
    """Close open strings, arrays and objects in a JSON prefix."""
    stack, in_str, escaped = [], False, False
    for ch in buf:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            stack.pop()
    out = buf + ('"' if in_str else "")
    out = out.rstrip().rstrip(",")
    return out + "".join(reversed(stack))


def parse_partial(buf: str):
    """Longest prefix that parses once closed; None if nothing does."""
    for cut in range(len(buf), 0, -1):
        try:
            return json.loads(close_partial(buf[:cut]))
        except json.JSONDecodeError:
            continue
    return None


if __name__ == "__main__":
    doc = json.dumps({"vendor": "ACME Cloud Ltd.", "total_cents": 15000,
                      "line_items": [{"description": "Hosting", "amount_cents": 12000}],
                      "note": "say \"hi\""})
    last_keys = 0
    for i in range(1, len(doc) + 1):
        partial = parse_partial(doc[:i])
        assert partial is None or isinstance(partial, dict)
        if partial:
            assert len(partial) >= last_keys      # fields never disappear
            last_keys = len(partial)
    assert parse_partial(doc) == json.loads(doc)
    assert parse_partial('{"vendor": "ACME Cl') == {"vendor": "ACME Cl"}
    assert parse_partial('{"vendor": "ACME", "total_cents": 15') == {
        "vendor": "ACME", "total_cents": 15}     # not final yet
    print("partial parsing ok over", len(doc), "prefixes")
```

Re-parsing per chunk is quadratic: fine for a few KB, use an incremental parser beyond that.

## Extraction tasks

- **Classify, then extract.** Route the document type first, then use a type-specific schema. One giant all-nullable schema fills fields that don't apply.
- **Long documents.** Extract per chunk and merge in code (dedupe by key, prefer values with evidence), or do one long-context pass if it fits. Evaluate both (see [Context engineering]({{ '/llm/context-engineering/' | relative_url }})).
- **Ground important fields.** Require an evidence quote or page number and verify it in code. It's the cheapest hallucination detector there is.
- **Copy in the model, compute in code.** The model transcribes; Python parses dates, converts currency and does arithmetic.
- **Ignore self-reported confidence.** It's poorly calibrated. Use sample agreement, evidence checks and business rules.
- **Evaluate per field.** Precision (invented values) and recall (missed values, wrong nulls) per field. Track the null rate over time.
- **Watch key overhead.** 100 records with 6 keys repeat 600 key strings, roughly 1,500 to 2,500 output tokens. For big tables, ask for a header row plus arrays of values and rebuild dicts in code.

## Pitfalls

- **Truncation.** Symptom: parse errors only on large inputs. Fix: check `finish_reason` first, cap lists, paginate. Never repair truncated output.
- **Refusals treated as validation errors.** Providers report them separately (a `refusal` field or stop reason). Route them to their own path.
- **Silently ignored keywords.** The provider dropped your `maxLength`. Fix: enforce every constraint in your own validator.
- **Schema drift.** Prompt and code disagree on the shape. Fix: generate the schema from the Pydantic model and version it with the prompt.
- **Valid treated as true or safe.** Schema-valid ids may not exist or may belong to another tenant. Authorize, parameterize SQL, escape HTML.

## Checklist

- Strict schema where available, generated from one Pydantic model, validated in code anyway.
- Nullable fields, enums with `other`, an escape hatch, descriptions.
- Check order: truncation, refusal, local repair, parse, schema, business rules.
- Repair with the exact error, 2 to 3 attempts, then a fallback. Monitor repair and null rates.
- Stream partial JSON for UX only. Act on the validated final object.

## Interview questions

**JSON mode vs structured outputs?** JSON mode guarantees parseable JSON. Structured outputs constrain decoding to your schema, so keys, types and enums conform. Neither guarantees correct values or completion within the token limit.

**How does constrained decoding work, and what does it cost?** The schema compiles to an automaton; at each step tokens that would leave the grammar get `-inf` logits. Costs: compile latency per new schema, small per-token overhead, and accuracy loss when the format fights the model's preferred answer.

**Why validate if the provider enforces the schema?** Business rules aren't expressible, unsupported keywords may be ignored, output can be truncated or refused, and valid structure says nothing about correct values.

**Your extraction returns valid JSON but wrong totals 3% of the time. What do you do?** Add a business-rule validator (line items sum to total) that triggers a repair turn, require evidence quotes, look for a pattern in the failures (layout, currency), add them to the eval set, and for scans give the model OCR text plus the image.

## Further reading

- Willard and Louf, "Efficient Guided Generation for Large Language Models" (2023)
- Dong et al., "XGrammar: Flexible and Efficient Structured Generation Engine for Large Language Models" (2024)
- Tam et al., "Let Me Speak Freely? A Study on the Impact of Format Restrictions on Performance of Large Language Models" (2024)
- OpenAI Structured Outputs guide and Anthropic structured outputs documentation
- Pydantic documentation (models, validators, JSON Schema generation) and the JSON Schema specification
- llama.cpp grammars README (GBNF syntax)
