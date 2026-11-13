---
title: Multimodal models
section: llm
order: 12
phase: "Phase 2 · Prompting and context"
summary: How vision-language and audio models turn pixels and sound into tokens, what that costs, and how to use them for documents, charts and speech.
---

Multimodal models accept (and sometimes produce) images, audio and video alongside text. The practical questions are concrete: how does an image become something a language model can read, what does it cost per page or per minute, when is a vision model better than OCR, and where do these models hallucinate? Most production uses are understanding tasks: invoices and forms, screenshots, charts, call transcripts, voice agents.

## How images become tokens

### Vision encoders

A Vision Transformer (ViT; Dosovitskiy et al., 2020) cuts the image into fixed-size patches (14 or 16 pixels square is typical), flattens each patch, projects it to a vector with a linear layer, adds position embeddings and runs a transformer encoder over the sequence of patches. A 224x224 image with 16-pixel patches is a 14x14 grid, 196 patch embeddings. A 336x336 image with 14-pixel patches is 24x24, 576 embeddings.

Most VLM vision encoders come from contrastive pretraining. CLIP (Radford et al., 2021) trains image and text encoders so matching image-caption pairs get similar embeddings; SigLIP swaps the softmax loss for a sigmoid one. The patch features then already line up with language concepts.

### Vision-language models

```text
 image --> resize / tile --> vision encoder (ViT: CLIP, SigLIP)
                                  |  patch features (N x d_vision)
                                  v
                          connector / projector
            (MLP per patch, 2x2 patch merge, or a resampler
             with a fixed number of learned queries)
                                  |  visual tokens (M x d_model)
                                  v
 text tokens ------------> LLM decoder: attends over visual
                           and text tokens in one sequence
                                  |
                                  v
                             text output
```

The connector decides how many tokens an image costs:

| Connector | How | Tokens per image | Trade-off |
|---|---|---|---|
| MLP projector (LLaVA) | Each patch becomes one token | Equal to patches (576 at 336 px) | Simple, keeps detail, expensive |
| Patch merging (Qwen2-VL, pixel shuffle) | Fold 2x2 neighboring patches into one token | Patches / 4 | Cheaper with little loss |
| Resampler (Flamingo's Perceiver Resampler, BLIP-2 Q-Former) | Learned queries cross-attend to patches | Fixed, e.g. 32 to 64 | Cheap, loses fine detail like small text |
| Cross-attention layers (Flamingo, Llama 3.2 Vision) | Image features enter through extra cross-attention, not the sequence | No sequence tokens | Text context stays short; more architecture |
| Early fusion (Chameleon-style) | Images tokenized into discrete tokens and trained jointly from scratch | Varies | Can also generate images; expensive to train |

Resolution handling is the other design axis. A fixed input size (everything resized to 336 px) is cheap and destroys small text. Tiling ("AnyRes" in LLaVA-NeXT) splits a large image into encoder-sized tiles plus a downscaled thumbnail. Native dynamic resolution (Qwen2-VL) encodes the image near its own size with 2D position encoding, so token count follows pixel count; each token covers 28x28 pixels (14-pixel patches merged 2x2).

Typical training: take a pretrained encoder and LLM, train the connector on image-caption pairs with both frozen, then fine-tune on visual instruction data (VQA, document QA, charts, chat about images; LLaVA, Liu et al., 2023), usually unfreezing the LLM.

## Image token cost

Cost scales with pixel area, up to a per-image cap, not with how much information the image contains. A blank 1000x1000 image costs the same as a dense spreadsheet screenshot. Published formulas as of 2025 to 2026 (they change, so check the current vision docs):

- **Area-based.** About one token per 28x28-pixel area (Anthropic's docs describe it as `width * height / 750`), after downscaling images above a size limit. Per-image maximums ranged from about 1.6k tokens to about 4.8k on newer high-resolution models.
- **Tile-based.** OpenAI's GPT-4o-class formula: fit within 2048x2048, scale the shortest side down to 768, count 512-pixel tiles, then 170 tokens per tile plus 85 base. "Low detail" is a flat 85.
- **Fixed tiles.** Gemini 2.x counts 258 tokens for a small image and tiles larger ones into 768x768 tiles at 258 tokens each.

```python
import math


def vit_tokens(w: int, h: int, patch: int = 14, merge: int = 1) -> int:
    """Patch grid of a ViT; merge=2 folds 2x2 patches into one token."""
    cols, rows = math.ceil(w / patch), math.ceil(h / patch)
    return math.ceil(cols / merge) * math.ceil(rows / merge)


def area_tokens(w: int, h: int, px_per_token: int = 750,
                max_pixels: int = 1_150_000) -> int:
    """Downscale to a pixel cap (keeping aspect), ~1 token per N pixels."""
    scale = min(1.0, math.sqrt(max_pixels / (w * h)))
    return math.ceil(int(w * scale) * int(h * scale) / px_per_token)


def tile_tokens(w: int, h: int, low_detail: bool = False) -> int:
    """GPT-4o-class tile formula as documented by OpenAI."""
    if low_detail:
        return 85
    scale = min(1.0, 2048 / max(w, h))          # fit in 2048 x 2048
    w, h = w * scale, h * scale
    scale = min(1.0, 768 / min(w, h))           # shortest side to 768
    w, h = w * scale, h * scale
    return 85 + 170 * math.ceil(w / 512) * math.ceil(h / 512)


def daily_cost(pages: int, tokens_per_page: int, usd_per_m: float) -> float:
    return pages * tokens_per_page * usd_per_m / 1e6


if __name__ == "__main__":
    assert vit_tokens(224, 224, patch=16) == 196
    assert vit_tokens(336, 336) == 576                   # LLaVA-1.5
    assert vit_tokens(336, 336, merge=2) == 144          # 2x2 merge
    assert tile_tokens(1024, 1024) == 765                # documented example
    assert tile_tokens(2048, 4096) == 1105               # documented example
    assert tile_tokens(4000, 3000, low_detail=True) == 85
    assert area_tokens(1000, 1000) == 1334
    assert area_tokens(4000, 3000) <= 1_150_000 // 750 + 1   # capped
    ratio = area_tokens(1000, 1000) / area_tokens(500, 500)
    assert abs(ratio - 4) < 0.01             # 2x the side, ~4x the tokens
    cost = daily_cost(pages=10_000, tokens_per_page=1_500, usd_per_m=3)
    assert cost == 45.0
    print("1024px tile:", tile_tokens(1024, 1024),
          "| area 1000px:", area_tokens(1000, 1000), f"| ${cost:.0f}/day")
```

The last line is a typical document workload: 10,000 scanned pages a day at about 1,500 tokens each is 15M input tokens, roughly $45 a day at $3 per million, before any output. The biggest lever is resolution. Send the smallest image where the text is still legible, or crop to the region you need.

## Document understanding

| Approach | How | Strengths | Weaknesses |
|---|---|---|---|
| PDF text layer | Read embedded text from digital PDFs | Free, exact characters | No layout; tables come out garbled; nothing for scans |
| OCR (Tesseract, cloud OCR) | Pixels to text with bounding boxes | Cheap, deterministic, character confidence, coordinates | Weak on table structure, handwriting and reading order |
| Layout-aware parsing | OCR plus detection of tables, headings and reading order | Structure for chunking and RAG | More pipeline to run |
| VLM on page images | Page image to answer or JSON | Understands layout, checkboxes, stamps, charts in one step | Cost per page, nondeterminism, can invent text, no character confidence |
| Hybrid | OCR text plus the page image into a VLM | Exact characters plus layout understanding | More tokens per page |

Practical guidance:

- **Digital PDFs: text layer first.** Use a VLM only for pages or regions where extraction fails (scans, figures, complex tables).
- **Scans: mind the resolution chain.** A letter-size page rendered at 200 DPI is 1700x2200 pixels, and the provider will downscale it to its limit. Small print can become illegible after that resize, and the model then guesses. Crop regions or split the page into tiles instead of sending the whole page at once.
- **Ask for evidence.** Page numbers, quoted text, or bounding boxes where the model supports them. Cross-check numbers against OCR output: if the VLM's total isn't in the OCR text, flag it.
- **Tables.** Ask for rows as JSON or HTML, then check row counts and column sums in code. Use [Structured outputs]({{ '/llm/structured-outputs/' | relative_url }}) for the contract.
- **Charts.** VLMs read titles, labels, legends and trends well. They are unreliable at exact values from bar heights or line positions without data labels. Ask for approximate values, mark them as estimates, and use the underlying data whenever you have it.
- **Multi-page documents.** Extract per page with page numbers in the output, then merge in code. See [Document ingestion]({{ '/llm/document-ingestion/' | relative_url }}).

Illustrative request shapes (provider SDKs; field names as of 2025 to 2026):

```python
import base64

b64 = base64.b64encode(open("invoice_p1.png", "rb").read()).decode()

# OpenAI Chat Completions: image as a content part with a detail level
openai_message = {
    "role": "user",
    "content": [
        {"type": "text", "text": "Transcribe the line items as JSON rows."},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
        },
    ],
}

# Anthropic Messages: image block, placed before the question
anthropic_message = {
    "role": "user",
    "content": [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64},
        },
        {"type": "text", "text": "Transcribe the line items as JSON rows."},
    ],
}
```

## Audio

### Speech-to-text

Whisper (Radford et al., 2022) is the reference design. It's an encoder-decoder transformer trained on about 680k hours of weakly supervised audio. Audio is resampled to 16 kHz, turned into a log-Mel spectrogram and processed in 30-second windows. The encoder produces 1,500 frames per window (one per 20 ms), and the decoder writes text with special tokens for language, task and timestamps.

A transcription pipeline usually has voice activity detection (VAD), chunking, timestamps, speaker diarization (a separate model such as pyannote) and post-processing for numbers, names and jargon. Quality is measured by word error rate:

`WER = (substitutions + deletions + insertions) / words in reference`

WER treats every word the same. A transcript at 5% WER can still get the one order number wrong, so measure entity accuracy (ids, amounts, names) separately on what matters.

```python
import re


def words(text: str) -> list[str]:
    return re.sub(r"[^\w\s']", " ", text.lower()).split()


def wer(reference: str, hypothesis: str) -> float:
    """Word-level edit distance divided by reference length."""
    r, h = words(reference), words(hypothesis)
    prev = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        cur = [i] + [0] * len(h)
        for j in range(1, len(h) + 1):
            sub = prev[j - 1] + (r[i - 1] != h[j - 1])
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, sub)
        prev = cur
    return prev[-1] / max(1, len(r))


if __name__ == "__main__":
    assert wer("Hello, world.", "hello world") == 0.0      # normalized
    assert wer("refund order four four two one",
               "refund order for for two one") == 2 / 6
    ref, hyp = "please refund order 4421 today", "please refund order 4412 today"
    assert wer(ref, hyp) == 0.2        # low WER, but the order id is wrong
    assert wer("a b c", "") == 1.0     # all deletions
    print(f"WER {wer(ref, hyp) * 100:.0f} percent, wrong order id")
```

Illustrative transcription with transformers (chunking handles audio longer than 30 seconds):

```python
from transformers import pipeline

asr = pipeline("automatic-speech-recognition", model="openai/whisper-small",
               chunk_length_s=30, return_timestamps=True)
result = asr("support_call.wav")
print(result["text"])
for chunk in result["chunks"]:
    print(chunk["timestamp"], chunk["text"])
```

### Text-to-speech and voice agents

Neural TTS streams audio, and the metric that matters is time to first audio byte. Many generative audio systems predict discrete tokens from neural codecs (SoundStream, EnCodec, using residual vector quantization), which the codec decoder turns into a waveform.

Voice agents come in two shapes:

```text
Cascaded:  mic -> VAD -> ASR -> LLM (+ tools) -> TTS -> speaker
Native:    mic -> speech-to-speech model (audio tokens in/out) -> speaker
```

| | Cascaded | Native audio model |
|---|---|---|
| Latency | Sum of stages; needs streaming at every stage to reach about 1 s | Lower; one model, responds while you finish speaking |
| Prosody, emotion, interruptions | Lost at the ASR step | Preserved; natural barge-in |
| Control, debugging, guardrails | Text at every hop: easy to log, filter and test | Harder; rely on transcripts produced on the side |
| Tool calling, model choice | Any LLM | Limited to models that support it |
| Cost | Usually lower | Audio tokens are priced higher (as of 2025-2026) |

Cascaded latency is end-of-speech detection (a few hundred ms) plus final ASR, LLM time to first token and TTS first byte. Every stage must stream, or you blow past the one to two seconds where conversation still feels natural. Audio input on hosted multimodal models is tokenized at a fixed rate (Gemini documents 32 tokens per second), so a 10-minute call is about 19k tokens.

## Video

Video models sample frames (often around one per second), encode each frame as an image (usually at reduced resolution), and add the audio track as audio tokens or a transcript, with timestamps so the model can reference moments. At a few hundred tokens per second of video, 10 minutes is on the order of 150k to 200k tokens. Practical approaches for long video: transcript first, then look at frames only around the relevant timestamps; sample keyframes at scene changes instead of a fixed rate; lower the frame rate or resolution for long footage. Anything that happens between sampled frames, such as a quick gesture or a flash of text, is invisible to the model.

## Generation vs understanding

Understanding (media in, text out) and generation (text in, media out) are usually different systems. Image generation is dominated by latent diffusion (Rombach et al., 2022), with autoregressive image-token models and natively multimodal LLMs growing; video generation is mostly diffusion over spatio-temporal latents; speech is TTS or codec-token models. In applications, generators are called as tools, priced per image or per second. Evaluation leans on human preference plus proxies (FID, CLIP score), and watermarking and C2PA content credentials are becoming standard for provenance.

## Multimodal RAG

Three patterns, from simplest to most capable (details in [Advanced RAG]({{ '/llm/advanced-rag/' | relative_url }})):

1. **Convert to text.** OCR, image captions, chart descriptions and transcripts, then normal text RAG. Simple and cheap, but anything the conversion missed is gone.
2. **Shared embedding space.** CLIP-style encoders put images and text in one vector space, so a text query retrieves images directly. Good for photos and catalogs, weak for text-heavy pages.
3. **Page-image retrieval.** ColPali (Faysse et al., 2024) embeds page screenshots as multi-vector patch embeddings and matches them to query tokens with late interaction, skipping OCR. Retrieved page images then go to a VLM to answer. Strong for visually rich documents (slides, reports with charts), but storage is larger and you need a VLM at answer time.

## Pitfalls

- **Hallucinated text.** The model "reads" plausible text that isn't there, especially when it's small, blurry or partly hidden, and it swaps digits. Symptom: values that don't appear in OCR output. Fix: higher-resolution crops, OCR cross-checks, an explicit "illegible" option in the schema, evidence quotes.
- **Silent downscaling.** The provider resizes your 4000-pixel scan to its limit and the fine print disappears. Fix: compute the effective resolution after resizing and crop or tile to stay above legibility.
- **Counting and spatial precision.** VLMs are weak at counting many objects, exact positions, reading gauges and exact chart values. Fix: use detection or OCR tools for those parts, or have the model call tools that crop and zoom.
- **Preprocessing bugs.** EXIF rotation ignored, multi-page TIFFs truncated to page one, PDFs rendered at 72 DPI. Fix: normalize images in one place; test with ugly real inputs.
- **Cost blowups in agent loops.** Computer-use agents add a screenshot every step, and the history grows fast. Fix: keep only the last few screenshots and downscale them (see [Coding and computer-use agents]({{ '/llm/coding-and-computer-use-agents/' | relative_url }})).
- **Prompt injection through images.** Text inside an image ("ignore previous instructions") is read like any other input. Fix: treat image text as untrusted data, same as retrieved documents.
- **Privacy.** Images carry faces, ID documents, health data and GPS coordinates in EXIF. Fix: strip metadata, redact before sending, apply the same retention rules as text.
- **ASR errors on what matters.** Names, numbers and jargon. Fix: domain vocabulary hints (Whisper accepts an initial prompt), post-correction against known entities, per-entity metrics. Resample audio to the rate the model expects (16 kHz mono for Whisper).
- **Benchmarks that don't match your data.** DocVQA and ChartQA scores say little about your invoices. Build a labeled set from your documents.

## Checklist

- Text layer or OCR first for documents; a VLM where layout, scans or visuals require it, or a hybrid.
- Compute image tokens per page from the provider's formula and set resolution deliberately.
- Crop or tile to keep small text legible after the provider resizes.
- Structured output with evidence (quotes, page numbers), and numbers cross-checked against OCR.
- For audio: VAD, chunking, timestamps, diarization if needed, WER plus entity accuracy.
- Cascaded voice pipeline unless prosody and latency justify a native audio model. Stream every stage.
- Treat text inside images and audio as untrusted input. Strip metadata.

## Interview questions

**How does a VLM process an image?** Resize or tile, split into patches, encode with a ViT (often CLIP or SigLIP), project patch features into the LLM's embedding space with a connector, and the LLM attends over visual and text tokens together.

**Why does image cost scale with resolution, and how do you control it?** Tokens are proportional to patches or tiles, which grow with pixel area up to a cap. Downscale to the smallest legible size, crop to regions of interest, use low-detail modes for coarse tasks, and use OCR when text is all you need.

**OCR or a VLM for invoice extraction?** Use the PDF text layer or OCR for exact characters and a VLM for layout and messy scans. Often both: give the VLM the OCR text plus the image, require evidence, and cross-check totals in code. Decide on a labeled set with field-level accuracy and cost per page.

**How would you build a voice agent, and where does the latency go?** VAD, streaming ASR, LLM, streaming TTS. Latency goes to end-of-speech detection, final ASR, LLM time to first token and TTS first byte. Stream every stage, cache prompts, and consider a native speech-to-speech model for natural interruptions.

**How do you do RAG over PDFs full of charts and tables?** Parse layout into text and tables for text retrieval, add captions or descriptions for figures, or use page-image retrieval like ColPali. Send the retrieved page images to a VLM for the final answer, with page citations.

**Why do VLMs hallucinate text, and how do you catch it?** The decoder generates likely text when the visual evidence is weak (low resolution, blur, occlusion), and resizing often destroys small text. Catch it with OCR cross-checks, evidence requirements, an "illegible" option, and evals focused on numbers and ids.

**Native audio vs a cascaded pipeline?** Native: lower latency, natural turn-taking, preserved tone. Cascade: easier to guard, log and test, works with any LLM, usually cheaper.

## Further reading

- Dosovitskiy et al., "An Image is Worth 16x16 Words: Transformers for Image Recognition at Scale" (2020)
- Radford et al., "Learning Transferable Visual Models From Natural Language Supervision" (CLIP, 2021)
- Liu et al., "Visual Instruction Tuning" (LLaVA, 2023)
- Wang et al., "Qwen2-VL: Enhancing Vision-Language Model's Perception of the World at Any Resolution" (2024)
- Radford et al., "Robust Speech Recognition via Large-Scale Weak Supervision" (Whisper, 2022)
- Faysse et al., "ColPali: Efficient Document Retrieval with Vision Language Models" (2024)
