---
title: Tokenization
section: llm
order: 3
phase: "Phase 1 · Foundations"
summary: How BPE, WordPiece, Unigram and byte-level tokenizers work, how chat templates use special tokens, and the tokenization bugs that reach production.
---

Tokenization turns text into the integer ids a model actually sees, and its effects leak into everything above it: billing, context limits, latency, multilingual cost, why models struggle to count letters, why a trailing space changes an answer, and why a fine-tuned model degrades when served with the wrong chat template. You will rarely implement a tokenizer, but you will debug its consequences often. This note covers the algorithms well enough to predict their behavior, then the special tokens, counting, cost and bugs that matter in production.

## Why subwords

A tokenizer has to choose units. Characters and whole words both fail at scale:

| Unit | Vocabulary | Sequence length | Unknown words | Problem |
|---|---|---|---|---|
| Characters or bytes | Tiny (256 bytes) | Very long | Never | Attention cost and context fill up fast |
| Words | Huge (millions) | Short | Common (typos, names, code) | Out-of-vocabulary words, giant embedding table |
| Subwords | 30k–260k | Medium | Never with byte fallback | The practical middle |

Subword tokenizers keep frequent strings ("the", " function", "ing") as single tokens and break rare strings into smaller pieces, down to bytes if needed. Frequent text is cheap; any text is representable.

## How it works

A modern tokenizer is a pipeline. Decoding runs it in reverse and must reproduce the original text exactly.

```text
"Hello  wörld!"
     │  normalize       Unicode normalization; most LLMs do little or none
     ▼
     │  pre-tokenize    regex split into chunks: words, numbers,
     ▼                  punctuation, whitespace runs
["Hello", " ", " wörld", "!"]
     │  model           BPE merges / WordPiece / Unigram on each chunk
     ▼
["Hello", " ", " w", "ör", "ld", "!"]
     │  post-process    add BOS, wrap in chat-template special tokens
     ▼
[BOS, id, id, id, id, id, id]
```

Pre-tokenization matters more than it looks: merges never cross chunk boundaries, so the regex decides whether a space attaches to the following word, how digits are grouped, and whether punctuation can fuse with letters.

### Byte-pair encoding (BPE)

BPE (Sennrich et al., 2016, adapted from a compression algorithm) learns a vocabulary bottom-up:

1. Start with a base vocabulary of single symbols (characters, or all 256 byte values).
2. Count every adjacent pair of symbols across the training corpus, weighted by word frequency.
3. Merge the most frequent pair into a new symbol and record the merge with its rank.
4. Repeat until the vocabulary reaches the target size.

Encoding new text replays the merges: within each chunk, repeatedly apply the earliest-learned merge that is present until none apply. The output depends only on the merge list, so encoding is deterministic.

This runnable toy is a real byte-level BPE trainer and encoder, the same algorithm production tokenizers use, minus the performance work:

```python
import re
from collections import Counter

# Pre-tokenizer: letters, 1-3 digit groups, punctuation, whitespace,
# underscores. Merges never cross these chunks.
PAT = re.compile(r" ?[^\W\d_]+| ?\d{1,3}| ?[^\w\s]+|\s+|_+")


def chunks(text: str) -> list[bytes]:
    return [c.encode("utf-8") for c in PAT.findall(text)]


def merge(parts: tuple, pair: tuple) -> tuple:
    out, i = [], 0
    while i < len(parts):
        if i + 1 < len(parts) and (parts[i], parts[i + 1]) == pair:
            out.append(parts[i] + parts[i + 1])
            i += 2
        else:
            out.append(parts[i])
            i += 1
    return tuple(out)


def train(text: str, vocab_size: int) -> dict:
    """Learn merges until 256 byte tokens + merges reach vocab_size."""
    words = Counter(tuple(bytes([b]) for b in c) for c in chunks(text))
    merges = {}  # (left, right) -> rank; lower rank = learned earlier
    while 256 + len(merges) < vocab_size:
        pairs = Counter()
        for word, freq in words.items():
            for pair in zip(word, word[1:]):
                pairs[pair] += freq
        if not pairs:
            break
        best = max(pairs, key=pairs.get)
        merges[best] = len(merges)
        merged = Counter()
        for word, freq in words.items():
            merged[merge(word, best)] += freq
        words = merged
    return merges


def encode(text: str, merges: dict) -> list[bytes]:
    tokens = []
    for c in chunks(text):
        parts = [bytes([b]) for b in c]
        while len(parts) > 1:
            # apply the earliest-learned merge present, as in training
            rank, i = min((merges.get(p, float("inf")), i)
                          for i, p in enumerate(zip(parts, parts[1:])))
            if rank == float("inf"):
                break
            parts[i:i + 2] = [parts[i] + parts[i + 1]]
        tokens.extend(parts)
    return tokens


def to_ids(tokens: list[bytes], merges: dict) -> list[int]:
    ids = {bytes([b]): b for b in range(256)}
    ids.update({a + b: 256 + rank for (a, b), rank in merges.items()})
    return [ids[t] for t in tokens]


if __name__ == "__main__":
    corpus = ("low lower lowest newer newest wider widest " * 20
              + "the lowest price is the newest price. " * 10)
    merges = train(corpus, vocab_size=256 + 30)
    print("first merges:", [(a + b).decode() for a, b in list(merges)[:8]])

    tokens = encode(" lowest newest", merges)
    print([t.decode() for t in tokens], to_ids(tokens, merges))
    assert [t.decode() for t in tokens] == [" lowest", " newest"]

    # Byte-level: nothing is out of vocabulary and decoding is exact.
    for s in ["the newest widest", "Hello, wörld! 日本語 123456 🙂",
              "tabs\tand_under  scores\n"]:
        assert b"".join(encode(s, merges)).decode("utf-8") == s

    english = "the newest price is the lowest"
    hindi = "नमस्ते दुनिया"
    for s in (english, hindi):
        print(f"{s!r}: {len(s)} chars, {len(s.encode())} bytes, "
              f"{len(encode(s, merges))} tokens")
    assert len(encode(english, merges)) < len(english) / 3  # in-domain
    assert len(encode(hindi, merges)) == len(hindi.encode())  # 1 per byte
```

Two lessons from running it. Text that resembles the training corpus compresses to a handful of tokens. Text in a script the tokenizer never learned merges for costs one token per UTF-8 byte, and Devanagari is 3 bytes per character. Real tokenizers have merges for many scripts, but the effect is the same in degree: languages underrepresented in the tokenizer's training data pay more.

### Byte-level BPE

GPT-2 made byte-level BPE standard: the base vocabulary is the 256 byte values, so any input can be encoded and there is no unknown token. GPT-2 maps raw bytes to printable characters for its vocabulary files, which is why you see `Ġ` for a leading space and `Ċ` for a newline when you inspect them. OpenAI's tiktoken encodings (`cl100k_base` at ~100k tokens, `o200k_base` at ~200k) and Llama 3's 128k vocabulary are byte-level BPE with a regex pre-tokenizer.

### WordPiece

WordPiece (BERT and its descendants) builds a vocabulary like BPE but chooses the merge that most increases the likelihood of the training data, commonly approximated as `count(ab) / (count(a) × count(b))`. That favors pairs whose parts rarely appear apart. Encoding is greedy longest-match-first within each word, and continuation pieces carry a `##` prefix: `tokenization` becomes `token`, `##ization`. A word with no matching pieces becomes `[UNK]`. BERT-base uses a 30,522-entry vocabulary. You meet WordPiece today mostly in encoder models used for embeddings, classification and reranking.

### Unigram language model

Unigram (Kudo, 2018) works top-down. It starts from a large candidate vocabulary, assigns each piece a probability, and repeatedly prunes the pieces whose removal hurts the corpus likelihood least, until it reaches the target size. Encoding finds the most probable segmentation with the Viterbi algorithm. Because it is probabilistic, it can sample alternative segmentations during training (subword regularization), which makes models more robust to segmentation noise. T5 and many multilingual models use Unigram.

### SentencePiece

SentencePiece is a library and an approach rather than a separate algorithm; it implements both BPE and Unigram. It treats input as a raw Unicode stream, replaces spaces with `▁` (U+2581) so spaces are ordinary symbols, and needs no language-specific pre-tokenizer. That matters for languages written without spaces, such as Chinese, Japanese and Thai. With byte fallback enabled, characters missing from the vocabulary are encoded as byte tokens like `<0xE0>` instead of `<unk>`. Llama 1 and 2 used SentencePiece BPE with byte fallback and a 32,000-entry vocabulary.

| Algorithm | Builds vocab by | Encodes by | Unknowns | Typical users |
|---|---|---|---|---|
| BPE | Merging frequent pairs | Replaying merges by rank | None if byte-level | GPT family, Llama 3, most decoder LLMs |
| WordPiece | Merging likelihood-maximizing pairs | Greedy longest match, `##` | `[UNK]` | BERT-style encoders |
| Unigram | Pruning a large candidate set | Viterbi best segmentation | Byte fallback optional | T5, multilingual models |
| SentencePiece | Library running BPE or Unigram | As above, spaces as `▁` | Byte fallback optional | Llama 1–2, Gemma, T5 |

## Special tokens and chat templates

Special tokens are single ids with structural meaning: beginning and end of sequence (BOS, EOS), padding, unknown, role delimiters, tool-call markers, image placeholders, and fill-in-the-middle markers in code models. They should only ever be produced by your formatting code, never parsed out of user text.

A chat template renders a message list into one string using those tokens. Two widely used shapes:

```text
<|im_start|>system
You are a support assistant.<|im_end|>
<|im_start|>user
Where is my order?<|im_end|>
<|im_start|>assistant
```

```text
<|begin_of_text|><|start_header_id|>system<|end_header_id|>

You are a support assistant.<|eot_id|><|start_header_id|>user<|end_header_id|>

Where is my order?<|eot_id|><|start_header_id|>assistant<|end_header_id|>
```

The first is ChatML, used by several model families; the second is Llama 3's format. The trailing assistant header is the generation prompt: it tells the model that the next tokens are its reply. Open models ship the template as a Jinja string in `tokenizer_config.json`. A simplified ChatML template looks like this:

{% raw %}
```text
{% for m in messages %}<|im_start|>{{ m['role'] }}
{{ m['content'] }}<|im_end|>
{% endfor %}{% if add_generation_prompt %}<|im_start|>assistant
{% endif %}
```
{% endraw %}

Hosted APIs apply the template for you. With open models you apply it, or your serving engine does (vLLM's OpenAI-compatible server uses the model's template). Illustrative usage with `transformers`:

```python
from transformers import AutoTokenizer

tok = AutoTokenizer.from_pretrained("meta-llama/Llama-3.1-8B-Instruct")
messages = [
    {"role": "system", "content": "You are a support assistant."},
    {"role": "user", "content": "Where is my order?"},
]
prompt = tok.apply_chat_template(messages, tokenize=False,
                                 add_generation_prompt=True)
ids = tok.apply_chat_template(messages, add_generation_prompt=True)
print(prompt)
print(len(ids), "prompt tokens")
```

What goes wrong with templates:

- **Wrong or missing template.** Fine-tuning with one format and serving with another produces a model that rambles, ignores the system prompt or never stops. Nothing errors; quality just drops.
- **Missing generation prompt.** Without the assistant header, the model may continue the user's message instead of answering.
- **Double BOS.** The template adds BOS and the tokenizer adds another when you tokenize the rendered string. Usually a small quality hit, and hard to spot without printing ids.
- **Special-token injection.** If user text containing `<|im_end|>` is converted to the real special id, a user can close their turn and forge a system or assistant turn. tiktoken raises an error by default when input text contains special-token strings; Hugging Face tokenizers have options controlling whether such strings are matched. Check the behavior in your stack and test it.

## Counting tokens

Count with the model's own tokenizer, through the full chat template, before sending. Illustrative:

```python
import tiktoken

enc = tiktoken.get_encoding("o200k_base")
n = len(enc.encode(document_text))
pieces = [enc.decode([t]) for t in enc.encode("Unbelievable, 12345!")]
print(n, pieces)   # inspect splits when debugging
```

- **OpenAI models:** tiktoken encodings are public. Add a few tokens per message for template overhead.
- **Open models:** the Hugging Face tokenizer plus `apply_chat_template` gives exact counts.
- **Providers with unpublished tokenizers:** as of 2025–2026, Anthropic and Google expose token-counting endpoints. Use those for budgeting rather than another vendor's tokenizer.
- **Tools and schemas count.** Tool definitions and JSON schemas are rendered into the prompt and can add hundreds to thousands of tokens per call.
- **The usage field is ground truth.** Reconcile your estimates against the `usage` returned with each response.

## Cost differences by language and content

UTF-8 uses 1 byte for ASCII, 2 for most accented Latin, Greek, Cyrillic, Arabic and Hebrew characters, 3 for Devanagari, Thai and CJK, and 4 for most emoji (more for emoji sequences joined with zero-width joiners). A byte-level tokenizer with few merges for a script falls back toward one token per byte.

Petrov et al. (2023) measured the same content translated into many languages and found token counts differing by large factors, up to roughly an order of magnitude for some language and tokenizer pairs. For users of those languages, that means higher cost, less effective context, slower responses and tighter rate limits for the same work. Newer, larger vocabularies with more multilingual merges narrow the gap but do not close it.

Code and structured data have their own patterns:

- **Whitespace.** Indentation can cost a token per few spaces; newer vocabularies include tokens for runs of spaces. Minify embedded code and JSON when the model does not need the formatting.
- **JSON.** Quotes, braces and repeated keys per record add up. For large tables in a prompt, CSV or one record per line with a header is usually far cheaper than an array of objects.
- **Identifiers and UUIDs.** Random strings tokenize poorly, often one token per 2–3 characters. Long hashes and base64 blobs are expensive and useless to the model; replace them with short references.

## Vocabulary size trade-offs

| | Smaller vocabulary (~32k) | Larger vocabulary (~128k–260k) |
|---|---|---|
| Tokens per text | More | Fewer, especially non-English and code |
| Context capacity, cost, decode steps | Worse | Better |
| Embedding and output matrices | Smaller | Larger: `vocab × d_model` each |
| Softmax cost per step | Lower | Higher |
| Rare-token training | Most tokens seen often | Long tail of rarely seen tokens |

Reference points: GPT-2 50,257; BERT-base 30,522; Llama 2 32,000; `cl100k_base` ~100k; Llama 3 128,256; `o200k_base` ~200k; Gemma about 256k. The matrix cost is real: Llama 3 8B's embedding table is `128,256 × 4,096 ≈ 525M` parameters, and the untied output head is another 525M, about 13% of the model. For a ~2B model with a 256k vocabulary and `d_model` of 2,048, one embedding matrix alone is about 0.5B parameters, which is why small models usually tie input and output embeddings.

## Pitfalls

- **Trailing whitespace in prompts.** Most BPE vocabularies attach the space to the following word (`" Paris"` is one token). A prompt ending in `"The capital of France is "` forces the model to produce `"Paris"` without its usual leading space, an unusual token sequence that can lower quality. Strip trailing spaces from prompts and few-shot prefixes. Some libraries implement "token healing" to back up over the last token and let the model regenerate it.
- **Leading-space variants.** `"Yes"`, `" Yes"` and `"yes"` are different ids. This bites logit bias, logprob-based classification and any code that maps labels to single tokens. Check the exact token the model would emit after your prompt.
- **Numbers.** Depending on the pre-tokenizer, numbers split into single digits, fixed groups of up to three digits, or arbitrary learned chunks. Inconsistent splits make arithmetic and digit-level comparisons unreliable. Do math in code and have the model call a tool.
- **Character-level questions.** Counting letters, reversing words or checking spelling fight the tokenizer because the model never sees characters. Use code for these.
- **Streaming split characters.** A multi-byte character can span two tokens. Decoding each token separately yields invalid UTF-8 or replacement characters (`�`). Buffer bytes and decode incrementally; most SDKs and servers handle this, custom stream parsers often do not.
- **Stop sequences across boundaries.** A stop string can be split across tokens. Servers match on decoded text; if you implement your own stopping, match on the accumulated text, not on single tokens.
- **Glitch tokens.** Tokens that appear in the tokenizer's corpus but rarely in model training data get poorly trained embeddings and can trigger bizarre output. Rarely an issue with hosted frontier models, more likely with custom vocabularies.
- **Unicode tricks.** Zero-width characters, homoglyphs and different normalization forms (NFC vs NFD) change tokenization and can slip text past keyword filters. Normalize and strip invisible characters at your trust boundary; see [Security and guardrails]({{ '/llm/security-and-guardrails/' | relative_url }}).
- **Tokenizer drift.** A new model version can ship a new tokenizer, changing costs and invalidating cached counts. Re-measure token counts on upgrades.

## Checklist

- Count tokens with the target model's tokenizer or count endpoint, through the chat template.
- Budget for template overhead, tool schemas and output, not just document text.
- Strip trailing whitespace from prompts and few-shot prefixes.
- Inspect token splits (`decode` each id) when a label, stop sequence or number behaves oddly.
- Prefer compact formats (minified JSON, CSV) for bulk context.
- Measure per-language token costs if you serve non-English users.
- Never let user text become special token ids; test it.
- For open models, print the rendered template once and diff it against the model card.

## Interview questions

- **How does BPE training work?** Start with bytes or characters, repeatedly merge the most frequent adjacent pair across the corpus, record merges in order, stop at the target vocabulary size. Encoding replays merges by rank.
- **Why byte-level BPE?** The base vocabulary covers all 256 bytes, so any string encodes without an unknown token and decoding is lossless.
- **BPE vs WordPiece vs Unigram?** BPE merges by frequency; WordPiece merges by likelihood gain and encodes greedily with `##` continuations; Unigram prunes a large vocabulary by likelihood and encodes with Viterbi, allowing sampled segmentations.
- **Why do models struggle to count the letters in a word?** They see subword tokens, not characters, so letter-level facts must be memorized per token rather than read off the input.
- **Why does the same prompt cost more in Hindi or Thai than in English?** Those scripts take 3 bytes per character in UTF-8 and have fewer merges in English-heavy vocabularies, so they need more tokens for the same content.
- **What is a chat template and what breaks if it is wrong?** The format that renders role-tagged messages into one token sequence with special tokens. A mismatch with the training format silently degrades instruction following and stopping behavior.
- **What are the trade-offs of a larger vocabulary?** Shorter sequences and better multilingual efficiency, at the cost of bigger embedding and output matrices, more softmax work per step and more rarely trained tokens.

## Further reading

- Sennrich, Haddow and Birch, 2016. "Neural Machine Translation of Rare Words with Subword Units."
- Schuster and Nakajima, 2012. "Japanese and Korean Voice Search" (WordPiece).
- Kudo, 2018. "Subword Regularization: Improving Neural Network Translation Models with Multiple Subword Candidates."
- Kudo and Richardson, 2018. "SentencePiece: A simple and language independent subword tokenizer and detokenizer for Neural Text Processing."
- Radford et al., 2019. "Language Models are Unsupervised Multitask Learners" (GPT-2, byte-level BPE).
- Petrov et al., 2023. "Language Model Tokenizers Introduce Unfairness Between Languages."
