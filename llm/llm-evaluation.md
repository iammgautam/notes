---
title: Evaluating LLM systems
section: llm
order: 28
phase: "Phase 5 · Evaluation"
summary: Build golden datasets, pick graders, calibrate LLM judges, put error bars on small eval sets and gate CI so every change ships on evidence.
---

An LLM feature is a function whose behavior you cannot read from its code. A one-line prompt edit, a provider's model update, a new retrieval parameter or a different temperature can shift outputs across thousands of inputs you never looked at. Evaluation is how you find out what changed: a fixed set of inputs, a way to grade outputs, and numbers you trust enough to decide whether to ship. The teams that iterate fastest are rarely the ones with the cleverest prompts. They are the ones that can tell, within minutes, whether a change made things better or worse.

## Why evals are the core skill

- **Outputs are open-ended and stochastic.** There is rarely one correct string, so you need graders, not string equality.
- **Changes have non-local effects.** A prompt tweak that fixes one failure quietly breaks five other cases.
- **Models change under you.** Aliases move to new snapshots; you need a cheap way to requalify.
- **Every other decision is an eval question:** cheaper model, fine-tuning, a new reranker.

Start with **error analysis**, not metrics. Read 50–100 real traces, write a one-line note on what went wrong in each, and group the notes into failure categories ("invents a refund exception", "ignores the customer's plan", "calls search twice"). Then build graders for the categories that matter. Generic scores ("helpfulness 4.2/5") rarely move a decision; failure rates ("3% of answers cite a policy that does not exist") do.

## Eval types

| Type | What it checks | Best for |
|---|---|---|
| Unit-style assertions | Deterministic properties: valid JSON, required field, banned phrase, right tool | Contracts, formats, safety tripwires; every commit |
| Reference-based | Output vs a known answer: exact match, numeric tolerance, similarity | Extraction, classification, short-answer QA |
| Rubric / model-graded | An LLM judge applies written criteria | Open-ended text: faithfulness, completeness, tone |
| Human review | Domain experts label outputs | Ground truth, judge calibration, high-stakes launches |
| Pairwise | Which of two outputs is better | Comparing versions when absolute scores are noisy |

Cost rises down the table: assertions take milliseconds, judges cost a model call, humans cost expert time. Push every check to the cheapest type that can make it. If a regex can catch it, do not pay a judge.

## Building golden datasets

A golden dataset is a versioned list of inputs with expectations and metadata, stored as JSONL next to the prompts it tests:

```json
{
  "id": "billing-0042",
  "input": {"message": "Cancel my plan but keep my data export", "plan": "pro"},
  "expected_tool": "cancel_subscription",
  "must_include": ["export"],
  "reference": "Cancels at period end; the export stays available for 30 days.",
  "tags": ["billing", "multi-intent"],
  "source": "prod-2026-10",
  "critical": true
}
```

Where cases come from, most valuable first:

1. **Production traces:** sampled, redacted, used with consent; oversample failures (negative feedback, escalations, retries). See [Online evaluation and A/B testing]({{ '/llm/online-evaluation/' | relative_url }}).
2. **Expert-written cases** for each requirement and policy rule.
3. **Edge cases:** empty or very long inputs, other languages, typos, multi-intent, out-of-scope and unanswerable requests.
4. **Adversarial cases:** prompt injection in input and retrieved documents, jailbreaks, PII extraction. See [Security and guardrails]({{ '/llm/security-and-guardrails/' | relative_url }}).
5. **Synthetic cases** for coverage gaps. Review a sample: they share the generator's blind spots and run easier than real traffic.

Size by purpose: a **smoke set** of 20–50 critical cases that must all pass, a **regression set** of 100–300 that detects changes of roughly 8–10 points, and a **decision set** of 300–1,000+ for model swaps and launches, where few-point differences and per-slice numbers matter.

- **Stratify.** Tag cases by slice (intent, tier, language, difficulty) and report per slice; a slice needs about 30 cases before its number means much. Overrepresent rare but costly cases (refunds, legal, deletion) and report a traffic-weighted overall separately.
- **Hold out a test split.** Tuning prompts against the same 100 cases for weeks overfits them.
- **Version the set** and rerun the baseline whenever cases change. If two experts disagree on a case, fix the rubric or drop the case.

## Graders

| Grader | Good for | Watch out for |
|---|---|---|
| Exact match (normalized) | Labels, ids, short answers | Case, punctuation, number formats |
| Regex / contains | Required ids, banned phrases, formats | Brittle to paraphrase; best for absence checks |
| Schema (Pydantic, JSON Schema) | Structured outputs, tool arguments | Valid shape is not correct values |
| Code execution | Code generation, text-to-SQL, math | Sandboxing, timeouts, flaky tests |
| Embedding similarity | Paraphrase-tolerant reference match | Blind to negation and numbers |
| LLM-as-judge | Open-ended quality, faithfulness | Cost, bias, needs calibration |

Schema checks pair with [structured outputs]({{ '/llm/structured-outputs/' | relative_url }}). For text-to-SQL, compare result sets from a fixture database, not SQL strings. The deterministic graders in standard-library Python (bag-of-words cosine stands in for embeddings):

```python
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from math import sqrt
from pathlib import Path


def normalize(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", s.lower()).split())


def exact_match(output: str, expected: str) -> bool:
    return normalize(output) == normalize(expected)


def regex_check(output: str, pattern: str, must_match: bool = True) -> bool:
    return (re.search(pattern, output, re.IGNORECASE) is not None) == must_match


def schema_check(output: str, fields: dict[str, type]) -> bool:
    try:
        obj = json.loads(output)
    except json.JSONDecodeError:
        return False
    return isinstance(obj, dict) and all(isinstance(obj.get(k), t) for k, t in fields.items())


def code_check(code: str, tests: str, timeout: float = 5.0) -> bool:
    # A subprocess is not a sandbox: use a container with no network in production.
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "candidate.py"
        path.write_text(code + "\n\n" + tests)
        try:
            proc = subprocess.run([sys.executable, "-I", str(path)],
                                  capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        return proc.returncode == 0


def bow_cosine(a: str, b: str) -> float:
    va, vb = Counter(normalize(a).split()), Counter(normalize(b).split())
    dot = sum(va[t] * vb[t] for t in va)
    norm = sqrt(sum(v * v for v in va.values())) * sqrt(sum(v * v for v in vb.values()))
    return dot / norm if norm else 0.0


if __name__ == "__main__":
    assert exact_match("  Paris. ", "paris") and not exact_match("Paris, France", "Paris")
    assert regex_check("Order A-1042 shipped", r"\bA-\d{4}\b")
    assert regex_check("Here is your answer", r"as an ai", must_match=False)
    assert schema_check('{"intent": "refund", "amount": 49.99}', {"intent": str, "amount": float})
    assert not schema_check('{"intent": "refund"}', {"intent": str, "amount": float})
    add = "def add(a, b):\n    return a + b"
    assert code_check(add, "assert add(2, 3) == 5") and not code_check(add, "assert add(2, 3) == 6")
    assert not code_check("while True: pass", "", timeout=1.0)
    ref = "annual plans are refundable within 30 days"
    # Similarity misses negation: the opposite claim still scores above 0.9.
    assert bow_cosine("annual plans are not refundable within 30 days", ref) > 0.9
    print("graders ok")
```

## LLM-as-judge done right

### Rubrics

- **Binary verdicts per criterion**, not 1–10 scores, which invite leniency and drift. One criterion per call (or per output field) keeps failure modes separate.
- **Reasoning before the verdict:** a short critique quoting the evidence, then the label. Temperature 0 and structured output; majority of 3 on borderline cases.
- **Give the judge what it needs:** the input, the output, and the reference, sources or policy, plus a few labeled borderline examples. A judge checking facts it cannot see is guessing.

```text
You are grading a customer-support answer. Use only the policy excerpt and
the criterion below. Do not use outside knowledge.

Criterion: POLICY_ACCURACY
PASS if every policy statement in the answer matches the excerpt.
FAIL if the answer states a policy that is absent from or contradicts the
excerpt, including invented exceptions, fees or time limits.
Tone, length and formatting do not affect this criterion.

<policy>...</policy>
<question>...</question>
<answer>...</answer>

Write 1-3 sentences of reasoning that quote the relevant text, then the
verdict. Respond as JSON with keys "reasoning" and "verdict" (PASS or FAIL).
```

### Pairwise vs pointwise

Pointwise grading ("is this good enough?") gives an absolute pass rate you can track and gate on, but scores compress near the top. Pairwise grading ("is B better than A?") is a more sensitive judgment, so it suits choosing between prompts or models, but it is only relative. Use pointwise binary criteria for CI and dashboards, pairwise for version decisions, and in pairwise mode judge both orders, `(A, B)` and `(B, A)`, counting a win only when both agree.

### Known biases

| Bias | Symptom | Mitigation |
|---|---|---|
| Position | Winner flips when you swap order | Judge both orders; disagreement is a tie |
| Verbosity | Longer answers win regardless of content | Rubric says length is not a criterion; check win rate vs length |
| Self-preference | Favors its own model family | Judge from another family, or a panel |
| Leniency, style | Confident, well-formatted wrong answers pass | Explicit fail conditions; provide the reference; grade facts separately |

### Calibration against human labels

A judge is a classifier, so measure it like one. Have experts label 100–200 outputs PASS/FAIL, tune the judge prompt on half, and measure on the other half: **true positive rate** (judge passes what humans pass), **true negative rate** (judge fails what humans fail) and **Cohen's kappa**. Recalibrate when the judge model, rubric or system under test changes.

Kappa corrects agreement for chance: `kappa = (p_o - p_e) / (1 - p_e)`, where `p_o` is observed agreement and `p_e` the agreement expected from each rater's PASS rate. On a 90%-pass set, an always-PASS judge agrees 90% of the time with kappa 0. Rough reading (Landis and Koch): 0.41–0.60 moderate, 0.61–0.80 substantial, above 0.80 almost perfect. Measure human-human kappa too; if two experts reach only 0.6, the rubric is ambiguous.

The true negative rate matters most, because the judge exists to catch failures. With known rates you can correct an observed pass rate (Rogan–Gladen): `true_pass ≈ (observed_pass + TNR - 1) / (TPR + TNR - 1)`.

```python
from collections import Counter


def cohens_kappa(a: list[str], b: list[str]) -> float:
    assert a and len(a) == len(b)
    n = len(a)
    p_o = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    p_e = sum(ca[k] * cb[k] for k in ca.keys() | cb.keys()) / n**2
    return 1.0 if p_e == 1 else (p_o - p_e) / (1 - p_e)


def judge_rates(human: list[str], judge: list[str], pos: str = "PASS") -> tuple[float, float]:
    tp = sum(h == pos and j == pos for h, j in zip(human, judge))
    tn = sum(h != pos and j != pos for h, j in zip(human, judge))
    p = sum(h == pos for h in human)
    return tp / p, tn / (len(human) - p)


if __name__ == "__main__":
    # 100 outputs: both PASS 65, both FAIL 20, human PASS/judge FAIL 5, human FAIL/judge PASS 10
    human = ["PASS"] * 65 + ["FAIL"] * 20 + ["PASS"] * 5 + ["FAIL"] * 10
    judge = ["PASS"] * 65 + ["FAIL"] * 20 + ["FAIL"] * 5 + ["PASS"] * 10
    kappa = cohens_kappa(human, judge)
    assert abs(kappa - 0.625) < 1e-9          # 85% raw agreement, 60% expected by chance
    tpr, tnr = judge_rates(human, judge)
    observed = judge.count("PASS") / len(judge)                       # 0.75
    corrected = (observed + tnr - 1) / (tpr + tnr - 1)
    assert abs(corrected - 0.70) < 1e-9       # the true pass rate is 70%
    assert cohens_kappa(["PASS"] * 90 + ["FAIL"] * 10, ["PASS"] * 100) == 0.0
    print(f"kappa={kappa:.3f} tpr={tpr:.2f} tnr={tnr:.2f} corrected={corrected:.2f}")
```

This judge agrees with humans 85% of the time but passes a third of real failures (TNR 0.67). Fix that before trusting it in CI.

## Statistical significance for small eval sets

For a pass rate `p` over `n` independent cases, `SE = sqrt(p * (1 - p) / n)` and the 95% interval is about `p ± 1.96 * SE`. At `p = 0.8`: ±11 points with 50 cases, ±8 with 100, ±5.5 with 200, ±4 with 400, ±2.5 with 1,000.

Comparing two versions on the same cases is far more sensitive than comparing two independent scores, because the noise comes only from cases where they disagree. Use a **paired bootstrap** on per-case differences, or **McNemar's test** on the counts of cases fixed and broken.

- **The case is the unit.** If you sample each case several times, average per case first, then bootstrap over cases.
- **Correlated cases inflate confidence.** Twenty questions from one document are not twenty independent observations; bootstrap over documents.
- **Many slices, many comparisons.** With 20 slices, one will "regress significantly" by chance. Treat slice alarms as a reason to read cases.

```python
import random
from math import comb


def bootstrap_ci(scores: list[float], n_boot: int = 10_000, alpha: float = 0.05,
                 seed: int = 0) -> tuple[float, float, float]:
    rng = random.Random(seed)
    n = len(scores)
    means = sorted(sum(rng.choices(scores, k=n)) / n for _ in range(n_boot))
    return sum(scores) / n, means[int(n_boot * alpha / 2)], means[int(n_boot * (1 - alpha / 2)) - 1]


def paired_diff_ci(a: list[float], b: list[float], **kw) -> tuple[float, float, float]:
    # Same cases, two systems: resample the per-case differences.
    return bootstrap_ci([y - x for x, y in zip(a, b, strict=True)], **kw)


def mcnemar_exact(fixed: int, broken: int) -> float:
    # Two-sided exact p-value from the discordant cases only.
    n, k = fixed + broken, min(fixed, broken)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2**n)


if __name__ == "__main__":
    a = [1.0] * 80 + [0.0] * 20               # system A: 80/100
    b = [0.0] * 2 + [1.0] * 84 + [0.0] * 14   # B breaks 2 of A's passes, fixes 6 failures
    mean_a, lo_a, hi_a = bootstrap_ci(a)
    assert mean_a == 0.8 and lo_a < 0.74 and hi_a > 0.86      # about ±8 points
    delta, lo, hi = paired_diff_ci(a, b)
    assert abs(delta - 0.04) < 1e-9 and lo <= 0 <= hi         # +4 points: not significant
    assert mcnemar_exact(6, 2) > 0.05
    assert mcnemar_exact(30, 10) < 0.01       # same ratio, five times the evidence
    print(f"A={mean_a:.2f} [{lo_a:.2f}, {hi_a:.2f}]  B-A={delta:+.2f} [{lo:+.2f}, {hi:+.2f}]")
```

## Public benchmarks and contamination

Common benchmarks: MMLU and MMLU-Pro (knowledge), GPQA (graduate-level science), GSM8K, MATH and AIME (math), HumanEval and MBPP (function-level code), SWE-bench Verified (resolving real GitHub issues), IFEval (verifiable instruction following), Chatbot Arena, now LMArena (crowd pairwise preferences), and LiveBench and LiveCodeBench (refreshed regularly to limit contamination). Read them with four caveats:

- **Contamination.** Questions and solutions leak into pretraining data, so a score can measure memory. Signs: better accuracy on items older than the training cutoff than on newer ones, verbatim completion of question text, large drops on perturbed versions (changed numbers, renamed variables).
- **Harness sensitivity.** Few-shot count, prompt format, chain-of-thought and answer extraction move scores by several points; numbers from different reports are rarely comparable.
- **Saturation and Goodhart's law.** Once a benchmark is a target, releases optimize for it.
- **Not your distribution.** Shortlist models with benchmarks; decide with your own eval set. See [Model landscape]({{ '/llm/model-landscape/' | relative_url }}).

## Eval-driven development

```text
  production traces ──► error analysis ──► new eval cases
         ▲                                       │
         │                                       ▼
  monitor + canary ◄── CI gate ◄── offline eval ◄── change: prompt,
  (online evals)     (thresholds,  (graders,         model, retrieval,
                      critical)     judges)          tools, params
```

Write the failing case first, make the change, read the per-case diff (what flipped, which way), ship through the gate and a canary, and turn each production failure into a new case.

## CI gating

| Tier | Trigger | What runs |
|---|---|---|
| Smoke | Every PR, under 2 minutes | Assertions and critical cases; cached outputs for unchanged inputs |
| Regression | PRs touching prompts, models, retrieval or tools; nightly | Full dev set, LLM judges, 1–3 samples per case |
| Release | Model swaps, major launches | Decision set, pairwise vs current, human review of diffs |

- **Critical cases must pass.** Any flip from pass to fail blocks the merge.
- **Block on significant drops, not any drop.** Fail when the paired interval for the delta lies entirely below a tolerance (say −2 points), or the drop exceeds the noise you measured by running main twice.
- **Per-slice floors** plus guardrails on cost per case and p95 latency.
- **Pin what moves scores:** judge model snapshot, judge prompt, temperature, dataset version. A judge change invalidates the baseline.
- **Store per-case results** as build artifacts and diff them against main.

Cost math: 300 cases × 3 samples × (1 system call + 2 judge calls) = 2,700 calls. At an assumed $0.002 per call, a run costs about $5.40, or about $160 a month nightly.

An illustrative promptfoo config (check the docs for your version):

```yaml
# promptfooconfig.yaml, run with: npx promptfoo@latest eval -c promptfooconfig.yaml
description: Support assistant regression suite
prompts:
  - file://prompts/support_v7.txt
providers:
  - openai:gpt-4o-mini          # any configured provider id
tests:
  - vars:
      question: Can I get a refund on an annual plan after 45 days?
    assert:
      - type: contains
        value: 30 days
      - type: llm-rubric
        value: Says no refund is available and cites the 30-day policy. Invents no exceptions.
```

## Tools

- **promptfoo:** open-source CLI with YAML test cases and assertions across prompts and providers; CI suites and red teaming.
- **OpenAI Evals:** open-source eval framework and registry; OpenAI also offers hosted evals.
- **DeepEval:** pytest-style LLM test cases with built-in metrics (G-Eval, faithfulness).
- **Ragas:** RAG and agent metrics plus synthetic test sets; see [Evaluating RAG]({{ '/llm/rag-evaluation/' | relative_url }}).
- **Langfuse** (open source) and **LangSmith** (hosted): tracing plus datasets, scores, experiments and annotation queues.

All wrap the same ingredients: dataset, task, grader, report. Keep your cases and graders portable (JSONL plus plain Python) so switching tools is cheap.

## Pitfalls

- **Vibe checks:** shipping the prompt that felt better. No prompt or model change merges without a suite run.
- **Averages hiding regressions:** +2 overall while every multi-intent case broke. Report per slice; diff per case.
- **Overfitting the eval set:** prompts accrete special cases. Keep a held-out split; refresh from production.
- **Uncalibrated or drifting judges:** measure TPR, TNR and kappa; pin judge versions.
- **No error bars:** +3 points on 80 cases is noise until a paired test says otherwise.
- **BLEU/ROUGE on open-ended answers:** weak correlation with quality.
- **Eval config differs from production** (system prompt, temperature, tools). Call the production code path.

## Checklist

- Failure categories named and counted from real traces.
- Golden set in git with ids, tags, sources, critical flags and a held-out split.
- Deterministic graders first; calibrated binary judges for the rest.
- Every number has an interval; comparisons are paired.
- CI tiers with critical gates, significance-aware thresholds and pinned judges.
- Production failures flow back into the set weekly.

## Interview questions

**How would you set up evals for a new LLM feature?** Categorize failures from real or pilot traces. Build a tagged golden set from production, expert, edge and adversarial cases. Grade deterministically where possible, with calibrated judges elsewhere. Run a smoke tier per PR and a full tier nightly, and feed production failures back.

**How do you know an LLM judge is trustworthy?** Treat it as a classifier: TPR, TNR and Cohen's kappa against expert labels on a held-out set, with human-human agreement as the ceiling. Recalibrate when the judge or rubric changes.

**B scores 84% and A 80% on 100 cases. Ship B?** Not on that alone. Each score is ±8 points. Compare paired: if B fixed 6 and broke 2, McNemar's p is about 0.29. Read the broken cases and gather more evidence.

**Pairwise or pointwise?** Pointwise binary criteria for tracking and gates; pairwise, in both orders, for choosing between versions.

**Why kappa instead of percent agreement?** With imbalanced labels, an always-PASS judge reaches high agreement. Kappa subtracts chance agreement.

## Further reading

- Zheng et al., "Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena" (2023).
- Liu et al., "G-Eval: NLG Evaluation using GPT-4 with Better Human Alignment" (2023).
- Liang et al., "Holistic Evaluation of Language Models" (HELM, 2022).
- Miller, "Adding Error Bars to Evals: A Statistical Approach to Language Model Evaluations" (2024).
- Cohen, "A Coefficient of Agreement for Nominal Scales" (1960).
- Efron and Tibshirani, "An Introduction to the Bootstrap" (1993).
