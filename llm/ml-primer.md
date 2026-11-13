---
title: The machine learning you need for LLMs
section: llm
order: 2
phase: "Phase 1 · Foundations"
summary: Parameters, loss, gradient descent, backprop, embeddings, softmax and cross-entropy, generalization and perplexity, with runnable demos.
---

You can ship LLM features without deriving backpropagation, but you cannot reason about fine-tuning, evaluation, embeddings, memory sizing or why a model behaves the way it does without a small core of machine learning. This note is that core: what a model is, how it learns, what the training numbers mean, and how to tell learning from memorizing. Every idea here reappears later, in fine-tuning (loss curves, learning rates, overfitting), in retrieval (embeddings), in decoding (softmax, logits) and in serving (parameter counts and bytes).

## Core ideas

### A model is a function with parameters

A model is a function `y = f(x; θ)` where `θ` is a large set of numbers called parameters (or weights). Training searches for values of `θ` that make `f` useful on the data. Nothing else is stored: no database of examples, no rules, just numbers that shape the function.

A "7B model" has about 7 billion parameters. That number tells you storage and compute directly:

- **Memory:** parameters × bytes per parameter. 7B in bf16 (2 bytes) is ~14 GB; in 4-bit it is ~3.5 GB plus overhead.
- **Inference compute:** roughly `2 × parameters` floating-point operations per generated token (one multiply and one add per weight).

### Neural networks

A neural network stacks layers. Each layer is a linear map followed by a nonlinearity:

```text
h = activation(W x + b)        W: matrix, b: vector, both learned
```

Without the nonlinearity (ReLU, GELU, SiLU), any stack of linear layers collapses into one linear layer, which can only draw straight boundaries. With it, a deep enough stack can approximate very complicated functions. Almost all of the compute is matrix multiplication, which is why GPUs, built for parallel multiply-add, are the hardware of choice.

A transformer is a specific arrangement of these pieces (attention plus feed-forward layers); see [Transformer architecture]({{ '/llm/transformer-architecture/' | relative_url }}).

### Loss: one number for "how wrong"

Training needs a single scalar that is low when the model is right. That scalar is the loss.

| Task | Typical loss | Formula |
|---|---|---|
| Predict a number | Mean squared error | `mean((pred - target)^2)` |
| Pick one class out of many | Cross-entropy | `-log p(correct class)` |
| Next-token prediction | Cross-entropy over the vocabulary | `-log p(actual next token)`, averaged over positions |

LLM pretraining and supervised fine-tuning both minimize cross-entropy on next tokens. When a training dashboard shows "loss 1.8", that is the average negative log-probability (in nats) the model assigned to the correct next token.

### Gradient descent

The gradient `∇L(θ)` is the vector of partial derivatives of the loss with respect to every parameter. It points in the direction that increases the loss fastest, so you step the other way:

```text
θ ← θ - η · ∇L(θ)          η = learning rate
```

In practice:

- **Minibatches.** You compute the gradient on a small random batch, not the whole dataset. This is stochastic gradient descent (SGD). The noise is acceptable and the speed is essential.
- **Learning rate** is the most important hyperparameter. Too high and the loss oscillates, spikes or becomes NaN; too low and training crawls or stalls.
- **Schedules.** LLM training typically warms the learning rate up over the first steps, then decays it (cosine or linear decay are common).
- **Adam and AdamW.** Modern training uses Adam-family optimizers, which keep a running mean of gradients (momentum) and of squared gradients (per-parameter step scaling). AdamW applies weight decay separately from the gradient update. Those two running statistics are extra per-parameter state, which matters for memory.
- **Vocabulary.** A *step* is one parameter update; *batch size* is the number of examples (or tokens) per step; an *epoch* is one pass over the dataset; *gradient accumulation* sums gradients over several small batches before one step to simulate a larger batch.

### Backpropagation

Backpropagation computes all the gradients efficiently by applying the chain rule backwards through the computation, reusing intermediate results. Frameworks such as PyTorch do it automatically (autograd): you write the forward pass, call `loss.backward()`, and every parameter gets a `.grad`.

Two engineering consequences:

- **The backward pass costs about twice the forward pass**, so a training step costs roughly three forward passes. This is where the `6 × parameters × tokens` training-compute rule comes from (2 for forward, 4 for backward). See [Pretraining and scaling laws]({{ '/llm/pretraining-and-scaling/' | relative_url }}).
- **Training needs far more memory than inference.** Backprop needs the activations from the forward pass, and the optimizer needs its state. A common accounting for mixed-precision AdamW is 16 bytes per parameter before activations:

| Item | Bytes per parameter |
|---|---|
| bf16 weights | 2 |
| bf16 gradients | 2 |
| fp32 master weights | 4 |
| Adam first moment (fp32) | 4 |
| Adam second moment (fp32) | 4 |
| **Total** | **16** |

A full fine-tune of a 7B model therefore needs on the order of 112 GB for weights, gradients and optimizer state alone, versus ~14 GB just to run it. That gap is why parameter-efficient methods exist; see [LoRA and PEFT]({{ '/llm/lora-and-peft/' | relative_url }}).

### Embeddings are learned vectors

Neural networks operate on vectors, but tokens are integer ids. An embedding layer is a lookup table: a matrix of shape `vocab_size × d_model` where row `i` is the vector for token `i`. Looking up a row is mathematically the same as multiplying a one-hot vector by the matrix.

Embedding rows are parameters like any others. They start random and are adjusted by gradient descent, so tokens that are used in similar contexts end up with similar vectors. Geometry emerges from training, not from anyone designing it. You compare vectors with cosine similarity: `cos(a, b) = a·b / (|a| |b|)`.

Two different things share the name:

- **Token embeddings** are the input layer of an LLM, one vector per vocabulary entry, consumed by the rest of the network.
- **Text embeddings** from embedding models (the ones you store in pgvector) are one vector per passage, produced by a whole network trained so that related texts land close together. See [Embeddings]({{ '/llm/embeddings/' | relative_url }}).

### Softmax and cross-entropy

The last layer of a classifier or language model produces one raw score per class, called a logit. Softmax turns logits into probabilities:

```text
softmax(z)_i = exp(z_i) / sum_j exp(z_j)
```

Subtracting `max(z)` from every logit before `exp` gives the same result and avoids overflow; every real implementation does this. Cross-entropy then scores the probability given to the right answer: `loss = -log p_correct`. A confident correct prediction (p = 0.95) costs 0.05; a confident wrong one (p = 0.01 on the right answer) costs 4.6.

The pair has a famously clean gradient: the derivative of cross-entropy with respect to the logits is `p - one_hot(correct)`. Raise the correct logit, lower the others in proportion to how much probability they took. The bigram demo below uses exactly this.

Logits are also what you manipulate at inference time: temperature divides them, `logit_bias` adds to them, and constrained decoding masks them. See [Decoding and sampling]({{ '/llm/decoding-and-sampling/' | relative_url }}).

### Generalization and overfitting

The goal is low loss on data the model has not seen. Overfitting means the model fits the training set, including its noise and quirks, at the expense of new data. You detect it by holding data out:

| Split | Used for | Rule |
|---|---|---|
| Train | Updating parameters | The only data gradients see |
| Validation | Choosing hyperparameters, early stopping | Look at it often, never train on it |
| Test | The final, honest number | Look at it rarely, report it once |

The signature of overfitting is training loss still falling while validation loss flattens and then rises. Remedies: more or more diverse data, fewer epochs (early stopping), weight decay, dropout, or a smaller trainable footprint (LoRA rank). For LLM work this matters most in fine-tuning: a few thousand examples for three to five epochs can memorize phrasing and degrade general ability. In evaluation, the equivalent sin is testing on data that leaked into training, which inflates scores; see [LLM evaluation]({{ '/llm/llm-evaluation/' | relative_url }}).

### Perplexity

Perplexity is the exponential of the average cross-entropy per token: `perplexity = exp(mean NLL)`. Read it as the effective number of equally likely choices the model is torn between at each step. A uniform guess over a 50k vocabulary has perplexity 50,000; a good language model on ordinary English text is in the single or low double digits.

Two cautions:

- **Perplexity depends on the tokenizer.** Different vocabularies split the same text into different numbers of tokens, so per-token perplexities across model families are not comparable. Bits per byte or per character normalizes this.
- **Lower perplexity is not a better assistant.** Perplexity measures how well a model predicts text, not whether it follows instructions, refuses safely or answers correctly. It is useful for comparing checkpoints of the same model, detecting domain shift, and scoring how "expected" a text is.

## In practice

### Gradient descent from scratch

Fitting a line by hand shows the whole loop: forward pass, loss, gradients by the chain rule, update. It also shows what a bad learning rate does.

```python
import random


def fit_line(xs, ys, lr=0.05, steps=2_000):
    w, b = 0.0, 0.0
    n = len(xs)
    for _ in range(steps):
        preds = [w * x + b for x in xs]                      # forward
        loss = sum((p - y) ** 2 for p, y in zip(preds, ys)) / n
        dw = sum(2 * (p - y) * x for p, y, x in zip(preds, ys, xs)) / n
        db = sum(2 * (p - y) for p, y in zip(preds, ys)) / n  # backward
        w -= lr * dw                                          # update
        b -= lr * db
    return w, b, loss


if __name__ == "__main__":
    rng = random.Random(0)
    xs = [rng.uniform(-2, 2) for _ in range(200)]
    ys = [3 * x + 2 + rng.gauss(0, 0.1) for x in xs]   # true w=3, b=2

    w, b, loss = fit_line(xs, ys)
    print(f"w={w:.3f} b={b:.3f} loss={loss:.4f}")
    assert abs(w - 3) < 0.05 and abs(b - 2) < 0.05

    # A learning rate past the stability limit diverges instead of learning.
    _, _, bad_loss = fit_line(xs, ys, lr=1.5, steps=50)
    print(f"lr=1.5 after 50 steps: loss={bad_loss:.3g}")
    assert bad_loss > 1e3
```

The same three lines (forward, backward, update) run inside every LLM training loop. The difference is scale: billions of parameters, gradients computed by autograd, and an optimizer more careful than `w -= lr * dw`.

### A tiny language model: softmax, cross-entropy, perplexity, overfitting

This trains a character bigram model: a table of logits where row `a` scores which character follows `a`. It is the smallest possible next-token predictor, trained with the same objective as an LLM. It prints train and validation perplexity as training runs.

```python
import math


def softmax(z):
    m = max(z)  # subtract the max so exp() cannot overflow
    e = [math.exp(v - m) for v in z]
    s = sum(e)
    return [v / s for v in e]


def mean_nll(W, pairs):
    """Mean cross-entropy (nats) of the next char given the previous char."""
    return sum(-math.log(softmax(W[a])[b]) for a, b in pairs) / len(pairs)


def train_epochs(W, pairs, lr, epochs):
    V = len(W)
    for _ in range(epochs):
        grad = [[0.0] * V for _ in range(V)]
        for a, b in pairs:
            p = softmax(W[a])
            for j in range(V):
                grad[a][j] += p[j] - (j == b)   # dLoss/dlogits = p - one_hot
        for a in range(V):
            for j in range(V):
                W[a][j] -= lr * grad[a][j] / len(pairs)


if __name__ == "__main__":
    train_text = ("the cat sat on the mat. the dog sat on the log. "
                  "a cat and a dog met on the mat. the cat saw the dog. ") * 5
    val_text = "the dog met a cat on the log. a cat saw the mat. the cow ran. "
    vocab = sorted(set(train_text + val_text))
    idx = {c: i for i, c in enumerate(vocab)}

    def pairs(text):
        ids = [idx[c] for c in text]
        return list(zip(ids, ids[1:]))

    train, val = pairs(train_text), pairs(val_text)
    V = len(vocab)
    W = [[0.0] * V for _ in range(V)]          # all-zero logits = uniform
    assert round(math.exp(mean_nll(W, val)), 6) == V

    history, done = [], 0
    for epoch in (10, 50, 200, 1000):
        train_epochs(W, train, lr=10.0, epochs=epoch - done)
        done = epoch
        tr, va = math.exp(mean_nll(W, train)), math.exp(mean_nll(W, val))
        history.append((epoch, tr, va))
        print(f"epoch {epoch:4d}  train ppl {tr:.2f}  val ppl {va:.2f}")

    best_epoch = min(history, key=lambda h: h[2])[0]
    assert history[-1][1] < history[0][1]      # train keeps improving
    assert best_epoch < history[-1][0]         # val peaked earlier: overfit
    assert min(h[2] for h in history) < V / 3  # far better than uniform
```

Typical output: validation perplexity drops from 16 (uniform over 16 characters) to under 4 by epoch 50, then creeps back up while training perplexity keeps falling. The validation text contains pairs the training text never had ("cow"), and further training pushes their probability toward zero. That is overfitting in miniature, and early stopping at the best validation checkpoint is the fix.

### The same loop in PyTorch

Illustrative; this is the shape of every fine-tuning script you will read, including those that TRL and the `transformers` Trainer wrap for you:

```python
import torch

model = MyModel()                                    # any nn.Module
opt = torch.optim.AdamW(model.parameters(), lr=3e-4, weight_decay=0.01)
loss_fn = torch.nn.CrossEntropyLoss()

for step, (x, y) in enumerate(train_loader):
    logits = model(x)                                # forward
    loss = loss_fn(logits.view(-1, logits.size(-1)), y.view(-1))
    opt.zero_grad()
    loss.backward()                                  # backprop
    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
    opt.step()                                       # update
    if step % 500 == 0:
        val_loss = evaluate(model, val_loader)       # watch for overfitting
```

### Reading a training run

| You see | It usually means | Do |
|---|---|---|
| Loss spikes or goes NaN | Learning rate too high, bad batch, fp16 overflow | Lower LR, add warmup, clip gradients, use bf16 |
| Train loss falls, val loss rises | Overfitting | Stop earlier, fewer epochs, more data, regularize |
| Both losses flat from the start | LR too low, frozen weights, data or label bug | Check that gradients are nonzero, inspect batches |
| Val loss much lower than train | Leakage, or dropout active only in training | Check splits for duplicates |
| Loss improves but task metric does not | Loss is a proxy | Evaluate the actual task, not just loss |

## Pitfalls

- **Comparing perplexity across tokenizers.** A model with a larger vocabulary predicts fewer, harder tokens. Compare per byte, or compare within one model family.
- **Treating training loss as quality.** A fine-tune whose loss went down can still be worse at the task, or worse at everything else. Always run a task eval and a regression eval.
- **Training on your test set.** Duplicates between fine-tuning data and eval data, or eval sets copied from public benchmarks the base model saw, inflate results. Deduplicate and keep a private holdout.
- **Overfitting small fine-tunes.** Symptom: the model parrots training phrasing, ignores new instructions, or loses general ability. Fix: fewer epochs, lower learning rate, more varied data, validation-based early stopping.
- **Underestimating training memory.** Inference memory (2 bytes per parameter in bf16) is a fraction of full-training memory (around 16 bytes per parameter plus activations).
- **Naive softmax.** `exp(1000)` overflows. Subtract the max, or use the framework's `log_softmax` and `cross_entropy`, which are numerically stable.

## Rules of thumb

- Parameters × bytes = weight memory. Parameters × 2 = FLOPs per generated token.
- Loss is in nats per token; `exp(loss)` is perplexity.
- If training is unstable, lower the learning rate first.
- Always keep a validation set and plot it next to training loss.
- For fine-tuning, few epochs over good data beat many epochs over the same small set.
- Embeddings are learned parameters; their geometry is only as good as the training signal behind them.

## Interview questions

- **What is a parameter, and what does "7B" tell you operationally?** A learned number in the model's weight matrices. 7B parameters means ~14 GB in bf16, ~3.5 GB in 4-bit, and about 14 GFLOPs per generated token.
- **Explain gradient descent in one sentence.** Repeatedly compute how the loss changes with each parameter and nudge every parameter a small step in the direction that lowers the loss.
- **Why does training need so much more memory than inference?** Training stores gradients, optimizer state (two fp32 moments for Adam) and often fp32 master weights, around 16 bytes per parameter, plus activations kept for the backward pass. Inference only needs the weights and the KV cache.
- **What is cross-entropy loss for a language model?** The average negative log-probability the model assigns to each actual next token. Its gradient with respect to the logits is the predicted distribution minus the one-hot target.
- **What is perplexity and when is it misleading?** `exp` of the average cross-entropy per token, the effective branching factor. It is misleading across different tokenizers and does not measure instruction following or factuality.
- **How do you detect overfitting in a fine-tune?** Validation loss stops improving and rises while training loss keeps falling; task metrics on held-out data plateau or drop; outputs start echoing training phrasing.
- **What is an embedding?** A learned vector representation. In an LLM, a lookup table row per token, trained with the rest of the network so similar usage yields nearby vectors.

## Further reading

- Goodfellow, Bengio and Courville, 2016. *Deep Learning* (MIT Press), chapters on optimization and regularization.
- Rumelhart, Hinton and Williams, 1986. "Learning representations by back-propagating errors."
- Kingma and Ba, 2014. "Adam: A Method for Stochastic Optimization."
- Loshchilov and Hutter, 2017. "Decoupled Weight Decay Regularization."
- Mikolov et al., 2013. "Efficient Estimation of Word Representations in Vector Space."
- Rajbhandari et al., 2019. "ZeRO: Memory Optimizations Toward Training Trillion Parameter Models."
