---
title: Transformer architecture
section: llm
order: 4
phase: "Phase 1 · Foundations"
summary: How a decoder-only transformer turns token ids into next-token logits, from attention and masking to positions, GQA, MoE and parameter math.
---

Nearly every model you will call or serve is a decoder-only transformer. You do not need to implement one, but the architecture explains the numbers you deal with: why long prompts cost more than linearly, why KV cache memory decides how many users a GPU can serve, why a "47B" mixture-of-experts model runs about as fast as a 13B dense one, and how to estimate memory from a config file. This note walks the forward pass from token ids to logits, then the variants that matter for choosing and serving models.

## The big picture

```text
token ids [t1 ... tn]
     │
     ▼
embedding lookup (vocab × d_model)          x: n × d_model
     │
     ▼
┌──────────────────────── block × N ─────────────────────────┐
│  x = x + Attention(Norm(x))   tokens exchange information  │
│  x = x + FFN(Norm(x))         each token transformed alone │
└────────────────────────────────────────────────────────────┘
     │
     ▼
final Norm ─► LM head (d_model × vocab)     logits: n × vocab
     │
     ▼
softmax of the last row ─► distribution over the next token
```

Think of `x` as the residual stream: one `d_model`-wide vector per token. Each block reads it, computes an update and adds it back. Attention is the only place information moves between positions; the feed-forward network processes each position independently.

For scale, Llama 3 8B has `d_model = 4,096`, 32 blocks, 32 query heads of 128 dimensions, 8 key-value heads, a feed-forward width of 14,336 and a vocabulary of 128,256.

## Core ideas

### Embeddings

The embedding layer is a `vocab × d_model` table; token id `i` selects row `i`. The rows are learned (see [The machine learning you need for LLMs]({{ '/llm/ml-primer/' | relative_url }})). At the other end, the LM head projects the final hidden state back to one logit per vocabulary entry. Some models tie the two matrices (share weights), which saves `vocab × d_model` parameters and is common in small models. Embeddings alone carry no information about position.

### Self-attention

Each token is projected three ways: a query (what am I looking for), a key (what do I contain) and a value (what do I pass along).

```text
Q = X W_Q      K = X W_K      V = X W_V
Attention(Q, K, V) = softmax(Q K^T / sqrt(d_k) + mask) V
```

Row `i` of `Q K^T` scores how relevant every token is to token `i`. Softmax turns those scores into weights that sum to 1, and the output for token `i` is the weighted average of the value vectors. It is a soft dictionary lookup: instead of fetching one value by exact key, every value is blended by similarity.

The `sqrt(d_k)` divisor keeps scores in a reasonable range. Dot products of random `d_k`-dimensional vectors have variance proportional to `d_k`; without scaling, large scores push softmax into near one-hot outputs with vanishing gradients.

Cost is the catch. The score matrix is `n × n`, so attention compute grows with the square of sequence length (`O(n² d)`) while the rest grows linearly. At tens of thousands of tokens, attention dominates prefill time.

This runnable demo implements single-head and multi-head causal attention in pure Python and checks the two properties that matter: each row of weights is a distribution, and no position can see the future.

```python
import math
import random


def matmul(A, B):
    cols = list(zip(*B))
    return [[sum(a * b for a, b in zip(row, col)) for col in cols] for row in A]


def transpose(M):
    return [list(r) for r in zip(*M)]


def softmax(row):
    m = max(row)
    e = [math.exp(v - m) for v in row]  # exp(-inf) == 0.0 for masked slots
    s = sum(e)
    return [v / s for v in e]


def attention(Q, K, V, causal=True):
    """softmax(Q K^T / sqrt(d_k) + mask) V for one head. Q, K, V: n x d."""
    d_k = len(Q[0])
    scores = matmul(Q, transpose(K))
    weights = []
    for i, row in enumerate(scores):
        masked = [s / math.sqrt(d_k) if (j <= i or not causal) else -math.inf
                  for j, s in enumerate(row)]
        weights.append(softmax(masked))
    return matmul(weights, V), weights


def multi_head(X, Wq, Wk, Wv, Wo, n_heads):
    """Project, split the model dim into heads, attend per head, concat, mix."""
    Q, K, V = matmul(X, Wq), matmul(X, Wk), matmul(X, Wv)
    d_head = len(Q[0]) // n_heads
    heads = []
    for h in range(n_heads):
        cols = slice(h * d_head, (h + 1) * d_head)
        out, _ = attention([r[cols] for r in Q], [r[cols] for r in K],
                           [r[cols] for r in V])
        heads.append(out)
    concat = [sum((head[i] for head in heads), []) for i in range(len(X))]
    return matmul(concat, Wo)


def rand_matrix(rows, cols, rng):
    return [[rng.gauss(0, 1 / math.sqrt(rows)) for _ in range(cols)]
            for _ in range(rows)]


if __name__ == "__main__":
    rng = random.Random(0)
    n, d_model, n_heads = 5, 8, 2
    X = rand_matrix(n, d_model, rng)            # 5 token vectors
    W = [rand_matrix(d_model, d_model, rng) for _ in range(4)]

    _, weights = attention(X, X, X)
    for i, row in enumerate(weights):
        assert abs(sum(row) - 1) < 1e-9            # a distribution per row
        assert all(w == 0.0 for w in row[i + 1:])  # nothing from the future
    assert weights[0][0] == 1.0                    # token 0 sees only itself

    out = multi_head(X, *W, n_heads=n_heads)
    X2 = [row[:] for row in X]
    X2[-1] = [v + 5.0 for v in X2[-1]]             # change only the last token
    out2 = multi_head(X2, *W, n_heads=n_heads)
    for i in range(n - 1):                         # earlier outputs unchanged
        assert all(abs(a - b) < 1e-12 for a, b in zip(out[i], out2[i]))
    assert any(abs(a - b) > 1e-6 for a, b in zip(out[-1], out2[-1]))
    print("last token's attention weights:",
          [round(w, 3) for w in weights[-1]])
```

### Multi-head attention

One attention pattern per layer is limiting: a token may need to track syntax, a referenced entity and the previous line of code at once. Multi-head attention splits `d_model` into `h` heads of size `d_head = d_model / h`, runs attention independently in each, concatenates the outputs and mixes them with an output matrix `W_O`. The parameter count stays at `4 × d_model²` for Q, K, V and O regardless of the head count; heads change what can be represented, not the size.

### Causal masking

A decoder must not see tokens it is supposed to predict. The causal mask sets scores for positions `j > i` to negative infinity before softmax, so their weights are exactly zero (the demo checks this). The mask is what makes training efficient: one forward pass over a sequence of `n` tokens produces `n` next-token predictions, each conditioned only on its prefix, so every position is a training example.

At inference, prefill runs the same masked pass over the whole prompt. Decoding then adds one token at a time; its query attends to keys and values from all earlier positions, which are cached rather than recomputed. That cache is the KV cache, and its size is a core serving constraint. See [KV cache and long context]({{ '/llm/kv-cache-and-long-context/' | relative_url }}).

### Feed-forward network

After attention, every token passes through the same two-layer MLP independently. The classic form is `FFN(x) = W_2 act(W_1 x)` with a hidden width of `4 × d_model`. Most current models use SwiGLU, a gated variant:

```text
FFN(x) = W_down ( SiLU(W_gate x) ⊙ (W_up x) )
```

SwiGLU has three matrices instead of two, so the hidden width shrinks to about `(8/3) × d_model` to keep parameters similar (Llama 2 7B: 11,008 for 4,096). The FFN holds roughly two thirds of a dense model's parameters. A useful mental model from interpretability work: FFN layers act like key-value memories storing much of the model's knowledge, while attention routes information between positions.

### Residual connections and normalization

Each sublayer's output is added to its input (`x + f(x)`), so the stream carries information and gradients through dozens of layers without degrading. Normalization keeps the scale of the stream stable:

```text
LayerNorm(x) = (x - mean(x)) / sqrt(var(x) + eps) * g + b
RMSNorm(x)   =  x / sqrt(mean(x^2) + eps) * g
```

RMSNorm drops the mean subtraction and bias, is cheaper, and works as well in practice; Llama, Mistral, Qwen and most recent models use it. The placement matters more than the variant. The original transformer normalized after adding the residual (post-norm), which is hard to train at depth without careful warmup. Modern models normalize the input to each sublayer (pre-norm), as in the diagram above, which trains stably.

### Positional information

Attention by itself treats its input as a set: shuffle the tokens and each output is unchanged apart from moving with its token. Position has to be injected.

| Scheme | How | Length behavior | Seen in |
|---|---|---|---|
| Sinusoidal absolute | Fixed `sin`/`cos` of position added to embeddings | Weak beyond trained length | Original transformer |
| Learned absolute | A trained vector per position | Hard maximum (512, 1,024) | BERT, GPT-2 |
| RoPE | Rotate pairs of query and key dimensions by `position × θ_i` | Extendable with scaling plus fine-tuning | Llama, Mistral, Qwen, most current LLMs |
| ALiBi | Add `-slope × distance` to attention scores, per-head slopes | Extrapolates somewhat past trained length | BLOOM, MPT |

RoPE is the default today. Rotating queries and keys means their dot product depends only on the distance between positions, so the model learns relative position without extra parameters. Frequencies follow `θ_i = base^(-2i/d)`; the base (`rope_theta`) is 10,000 in early models and larger in long-context ones (Llama 3 uses 500,000). Context extension methods such as position interpolation, NTK-aware scaling and YaRN rescale those frequencies and are usually followed by long-context fine-tuning. They appear as `rope_scaling` in a model's config.

The practical rule: usable context is set by what the model was trained or fine-tuned on at length, not by the position scheme. An extended window without long-context training degrades quietly.

## Architecture families

| Family | Attention | Objective | Good at | Examples |
|---|---|---|---|---|
| Encoder-only | Bidirectional | Masked token prediction | Embeddings, classification, rerankers, NER | BERT, RoBERTa, most embedding models |
| Decoder-only | Causal | Next-token prediction | Generation, chat, tools, general use | GPT, Llama, Mistral, Qwen, Gemma |
| Encoder-decoder | Bidirectional encoder, causal decoder with cross-attention | Sequence to sequence | Translation, summarization, speech | T5, BART, Whisper |

Decoder-only won for general-purpose LLMs: one objective covers all text, every token is a training signal, KV caching fits naturally, and in-context learning removes task-specific heads. Encoders remain the right tool for embeddings and cross-encoder rerankers, where bidirectional attention over short inputs is cheap and accurate. See [Embeddings]({{ '/llm/embeddings/' | relative_url }}) and [Hybrid search and reranking]({{ '/llm/hybrid-search-and-reranking/' | relative_url }}).

## Variants that matter for serving

### MHA, MQA and GQA

During decoding, every layer stores keys and values for every past token. Sharing key-value heads across query heads shrinks that cache:

| Variant | KV heads | KV cache | Quality |
|---|---|---|---|
| Multi-head (MHA) | One per query head | Largest | Baseline |
| Multi-query (MQA) | One shared | Smallest (`1/h`) | Some loss |
| Grouped-query (GQA) | `g` groups, e.g. 8 for 32 query heads | `g/h` of MHA | Close to MHA |

```text
KV bytes per token = 2 (K and V) × layers × kv_heads × head_dim × bytes
```

Llama 2 7B (MHA, 32 KV heads) stores 512 KiB per token in bf16; Llama 3 8B (GQA, 8 KV heads) stores 128 KiB. Serving 32 concurrent 8k-token conversations needs 128 GiB of cache for the first and 32 GiB for the second. That 4x is the difference between needing several GPUs and fitting beside the weights on one. GQA is now standard. DeepSeek's multi-head latent attention goes further by caching a compressed low-rank latent instead of full keys and values.

### Other attention changes

- **Sliding-window attention** limits each layer to the last `W` tokens (Mistral 7B used 4,096), capping cache growth. Information still travels further through stacked layers.
- **FlashAttention** computes exact attention in tiles in on-chip memory instead of materializing the `n × n` matrix: same math, far less memory traffic, used by default in serving engines.
- **State-space models** (Mamba) and attention hybrids scale linearly with length; worth knowing, not yet mainstream.

### Mixture of experts

A mixture-of-experts (MoE) layer replaces the single FFN with `E` expert FFNs and a small router. For each token, the router picks the top `k` experts, runs only those, and combines their outputs weighted by router scores.

```text
           ┌─► expert 1 ─┐
 token ─► router ─► expert 2 ─┼─► weighted sum ─► stream
 (top-2)   └─► expert 3 ─┘     (other experts idle for this token)
```

- **Total vs active parameters.** Mixtral 8x7B has about 47B parameters in total but about 13B active per token, because attention and embeddings are shared and only 2 of 8 experts run. DeepSeek-V3 has 671B total and 37B active.
- **Compute follows active parameters; memory follows total.** You pay for the full model in GPU memory but get roughly the per-token compute of a much smaller dense model.
- **Serving needs batching.** Experts are efficient only when many tokens share them, so MoE suits high-throughput serving.
- **Training needs balancing.** Routers collapse onto a few experts without an auxiliary load-balancing loss or a similar mechanism.

## In practice

### Parameter and memory math from a config

You can compute parameter counts from five numbers in `config.json`. This runnable calculator reproduces the published sizes of two well-known models and their KV cache footprint:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    d_model: int
    n_layers: int
    n_heads: int
    n_kv_heads: int
    d_ff: int
    vocab: int
    tied_embeddings: bool = False

    @property
    def head_dim(self):
        return self.d_model // self.n_heads

    def params(self) -> int:
        d, kv = self.d_model, self.n_kv_heads * self.head_dim
        attn = d * d + 2 * d * kv + d * d    # W_Q, W_K + W_V (shared), W_O
        ffn = 3 * d * self.d_ff              # SwiGLU: gate, up, down
        norms = 2 * d                        # two RMSNorm gains per block
        embed = self.vocab * d * (1 if self.tied_embeddings else 2)
        return self.n_layers * (attn + ffn + norms) + embed + d  # + final norm

    def kv_bytes_per_token(self, bytes_per_value=2) -> int:
        return 2 * self.n_layers * self.n_kv_heads * self.head_dim * bytes_per_value


if __name__ == "__main__":
    llama2_7b = Config(4096, 32, 32, 32, 11008, 32_000)
    llama3_8b = Config(4096, 32, 32, 8, 14336, 128_256)
    for name, c in [("Llama 2 7B", llama2_7b), ("Llama 3 8B", llama3_8b)]:
        p, kv = c.params(), c.kv_bytes_per_token()
        print(f"{name}: {p / 1e9:.2f}B params, {p * 2 / 2**30:.1f} GiB bf16, "
              f"KV {kv // 1024} KiB/token, {kv * 8192 / 2**30:.2f} GiB at 8k")
    assert round(llama2_7b.params() / 1e9, 2) == 6.74
    assert round(llama3_8b.params() / 1e9, 2) == 8.03
    assert llama2_7b.kv_bytes_per_token() == 4 * llama3_8b.kv_bytes_per_token()

    # Shortcut for classic blocks (MHA, 4x MLP): ~12 * layers * d_model^2
    print(f"12 * L * d^2 = {12 * 32 * 4096**2 / 1e9:.2f}B non-embedding")
```

Worked numbers: Llama 3 8B is 8.03B parameters, about 15 GiB of bf16 weights, plus 1 GiB of KV cache per 8k-token sequence. An 80 GB GPU holds a few dozen concurrent 8k sequences after weights and overhead; Llama 2's MHA layout fits a quarter as many. Forward compute is about `2 × params` FLOPs per token (16 GFLOPs for 8B) plus an attention term that grows with context.

### Reading a config

The fields that matter when you size or choose a model, using Hugging Face names:

| Field | Meaning | Why you care |
|---|---|---|
| `hidden_size`, `num_hidden_layers` | `d_model`, blocks | Memory, compute, KV cache |
| `num_attention_heads`, `num_key_value_heads` | Query and KV heads | GQA ratio, KV cache size |
| `intermediate_size` | FFN width | Parameter count |
| `max_position_embeddings` | Configured max positions | Upper bound, not proof of quality |
| `rope_theta`, `rope_scaling` | RoPE base and extension | Long-context behavior |
| `num_local_experts`, `num_experts_per_tok` | MoE layout (Mixtral naming) | Total vs active size |

## Pitfalls

- **Trusting the advertised context length.** Quality often degrades before the configured maximum. Symptom: answers ignore facts from the middle of long prompts. Fix: evaluate at your real lengths; see [KV cache and long context]({{ '/llm/kv-cache-and-long-context/' | relative_url }}).
- **Forgetting the KV cache in capacity planning.** Weights fit, then the server runs out of memory under concurrency. Fix: compute cache per token times expected tokens in flight.
- **Comparing MoE models by total parameters.** A 47B MoE is not priced or served like a 47B dense model in compute, but it needs that memory. Compare active parameters for speed and total for memory.
- **Mismatched RoPE settings.** Loading weights with the wrong `rope_theta` or scaling (often an old library version) degrades output at longer lengths. Fix: use the model's own config and a library version that supports it.
- **Ignoring quadratic attention.** Doubling a 50k-token prompt more than doubles prefill time. Fix: trim context, cache shared prefixes, measure TTFT against prompt length.

## Rules of thumb

- Parameters ≈ `12 × layers × d_model²` for classic blocks; the FFN holds about two thirds.
- Forward FLOPs per token ≈ `2 × parameters`; training ≈ `6 × parameters` per token.
- KV cache per token = `2 × layers × kv_heads × head_dim × bytes`.
- MoE: memory scales with total parameters, compute with active parameters.
- Pre-norm, RMSNorm, SwiGLU, RoPE and GQA are the default recipe for current decoder-only models.

## Interview questions

- **Walk through scaled dot-product attention.** Project tokens to queries, keys and values; score each query against all keys with `Q K^T / sqrt(d_k)`; mask future positions; softmax each row; take the weighted sum of values. Scaling by `sqrt(d_k)` keeps dot products from saturating softmax.
- **Why does causal masking make training efficient?** One forward pass gives a prediction at every position, each depending only on its prefix, so a sequence of `n` tokens yields `n` training examples in parallel.
- **What problem does GQA solve?** KV cache size and memory bandwidth during decoding. Sharing each key-value head across a group of query heads cuts the cache by the group factor with little quality loss.
- **RoPE vs learned absolute positions?** Learned absolute embeddings add a trained vector per position and cap length. RoPE rotates queries and keys so attention depends on relative distance, adds no parameters, and can be extended with frequency scaling plus fine-tuning.
- **Where are most of a transformer's parameters?** In the feed-forward networks, about two thirds of a dense model, then attention projections, then embeddings (large for big vocabularies).
- **Explain total vs active parameters in MoE.** Total is every expert's weights, which must sit in memory. Active is what one token uses (shared layers plus the top-k experts), which sets per-token compute.
- **Estimate the parameters of a model with d_model 4,096, 32 layers, MHA, 4x MLP and a 32k vocabulary.** `12 × 32 × 4,096² ≈ 6.4B` plus embeddings of `32k × 4,096 ≈ 0.13B` each, so about 6.7B.

## Further reading

- Vaswani et al., 2017. "Attention Is All You Need."
- Su et al., 2021. "RoFormer: Enhanced Transformer with Rotary Position Embedding."
- Shazeer, 2020. "GLU Variants Improve Transformer."
- Ainslie et al., 2023. "GQA: Training Generalized Multi-Query Transformer Models from Multi-Head Checkpoints."
- Dao et al., 2022. "FlashAttention: Fast and Memory-Efficient Exact Attention with IO-Awareness."
- Jiang et al., 2024. "Mixtral of Experts."
