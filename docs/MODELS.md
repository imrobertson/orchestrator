# Model catalog

**Last verified against `recipes/local/*.yaml`: 2026-09-09** (second pass — four recipes corrected, gemma-4-31b figures recovered from the retired `TROUBLESHOOTING.md`). Every row below
comes from a direct read of the recipe file, not from a previous version of
this document.

Live truth is `dgx-config status` or the dashboard dropdown. If this file and
the running system disagree, the running system wins.

Recipes prefixed with `_` are experimental or under test and are not for
production use.

---

## How to read this

**Concurrency** is the recipe's `--max-num-seqs`. `unset` means the flag isn't
passed and vLLM's own default applies — not that concurrency is unlimited in
practice; the KV pool still bounds it.

**Arch** is stated only where the recipe or the checkpoint name gives real
evidence: an `A<n>B` suffix in `hf_path` (active-parameter count, so MoE), an
explicit statement in the recipe header, or MoE-specific flags
(`--moe-backend`, `--enable-expert-parallel`). Where none of those are present
it is marked `?` rather than guessed. Filling those in is a small, worthwhile
pass — from the model cards, not from inference.

**Speculative decode** is read from `--speculative-config`'s `method` and
`num_speculative_tokens`. `qwen3_next_mtp` is Qwen's own MTP implementation and
is listed as `MTP` with its method name in parentheses where it matters.
**No recipe in the catalog currently uses DFlash2** — that's the open GLM-5.3
item in `WORKSTREAMS.md`.

**Speed** is recorded as a range where more than one run exists, per the
two-runs-minimum rule. A blank means unmeasured, not slow.

**A 2-node row cannot be checked with `--dry-run`.** That preview shows
only the Ray bootstrap containers; the engine is `docker exec`'d in
afterwards, so no `vllm_args` flag appears in it (confirmed 2026-09-10,
WORKSTREAMS WS-12). For a 2-node recipe the only verification is a real
deploy — which is why the untested 2-node rows below stay blank rather than
being marked plausible.

---

## Single-node (`1_node`)

| Recipe | Ctx | Conc | Arch | Quant | Spec decode | Measured warm tok/s | Status |
|---|---|---|---|---|---|---|---|
| `gemma4-26b-a4b-aeon-dflash` | 32K | 32 | MoE 26B/A4B | NVFP4 (compressed-tensors) | **DFlash n=10** | coding 103.4 · extraction 202.8 · creative 54.2 · default 49.3 | validated 09-01 |
| `qwen-3.6-35b-a3b-nvfp4` | 256K | 8 | MoE 35B/A3B | NVFP4 (mixed FP8/NVFP4) | MTP n=3 (`qwen3_next_mtp`) | 68.6, TTFT 0.11 s, 88.6% accept | validated 09-05 |
| `gemma4-26b-a4b-nvfp4` | 32K | unset | MoE 26B/A4B | NVFP4 (ModelOpt) + fp8 KV | MTP n=2 | coding 67.3 · extraction 72.9 · creative 54.6 | validated |
| `qwen-3.6-35b-a3b-nvfp4-nothink` | 256K | 4 | MoE 35B/A3B | NVFP4 | MTP n=3 | — | thinking disabled |
| `qwen-3.8-27b-nvfp4` | 256K | 2 | ? | NVFP4 | MTP n=5 | — | |
| `qwen-3.8-27b-nvfp4-sqk2` | 256K | unset | ? | NVFP4 | MTP n=5 | — | 1-node only; a coworker's personal tune |
| `qwen-3.8-27b` | 256K | unset | ? | bf16 + fp8 KV | MTP n=5 | — | no `image:` field |
| `qwen-3_6-27b-nvfp4-tp` | 256K | unset | ? | NVFP4 | MTP n=3 | — | |
| `qwen-3.6-35b-a3b-hauhaucs-nvfp4-nospec` | 256K | 8 | MoE 35B/A3B | NVFP4 | none | — | header says `_TEST`, filename doesn't |
| `nemotron-3.5-lightning-nvfp4` | 128K | 4 | MoE 30B/A3B | NVFP4 | DSpark n=5 | — | E023 fixed 2026-09-09, **untested** |
| `_nemotron-3.5-lightning-nvfp4-tools` | 128K | 4 | MoE 30B/A3B | NVFP4 | DSpark n=5 | — | E023 fixed 2026-09-09, experimental |
| `nemotron-3-nano-30b-a3b-nvfp4` | 128K | 8 | MoE 30B/A3B | NVFP4 | none | — | |
| `nemotron-3_5-lightning-bf16` | 128K | unset | MoE 30B/A3B | bf16 | none | — | |
| `muse-glimmer-30b-nvfp4-dflash-tools` | 128K | 8 | ? | NVFP4 W4A4 | **DFlash n=15** | — | untested; header says `_TEST`, filename doesn't |
| `muse-glimmer-30b-nvfp4` | 128K | 8 | ? | NVFP4 W4A4 | none | — | |
| `muse-glimmer-30b` | 128K | 4 | ? | bf16 + fp8 KV | none | — | **suspect — see below** |
| `gemma4-26b-a4b-nvfp4-tools` | 32K | unset | MoE 26B/A4B | NVFP4 | MTP n=2 | — | header says `_TEST`, filename doesn't |
| `deepseek-r1-distill-qwen-32b` | 32K | unset | dense 32B | bf16 + fp8 KV | none | — | |
| `qwen-2_5-coder-32b` | 32K | 2 | dense 32B | bf16 + fp8 KV | none | — | |
| `qwen-2_5-coder-32b-tp` | 32K | 2 | dense 32B | bf16 + fp8 KV | none | — | 1-node identical to the above |
| `llama-4-fp4` | 32K | unset | MoE 17B active / 16E | FP4 | none | — | |
| `llama-4-fp8` | 16K | 1 | MoE 17B active / 16E | FP8 | none | — | |
| `gemma-4-31b` | 32K | unset | dense 31B | BF16 ckpt + runtime fp8 + fp8 KV | none | **6.7**, warm TTFT 0.17 s | validated 2026-08-30 |

## Two-node (`2_node`, TP2 unless noted)

| Recipe | Ctx | Conc | Arch | Quant | Spec decode | Measured warm tok/s | Status |
|---|---|---|---|---|---|---|---|
| `deepseek-v4-flash-0731-dspark` | 384K | 4 | MoE (sparse-MLA) | fp8 KV | **DSpark n=5** | 42.7–44.7, TTFT 0.12–0.13 s, 38–46% accept | validated 08-29 |
| `glm-5_3-flash-nvfp4-mtp` | 256K | 2 | MoE 320B/A18B | NVFP4 W4A4 + fp8 KV | MTP n=5 | 21.3–23.2, TTFT 0.23–0.25 s, ~37% accept | validated 09-09 |
| `deepseek-v4-flash-0731-dspark-512k` | 512K | 1 | MoE | fp8 KV | DSpark n=5 | — | never run — deliberate mid-step to 1M |
| `deepseek-v4-flash-0731-1M` | 1024K | 1 | MoE | fp8 KV | none | — | |
| `qwen-3.5-122b-mtp2` | 256K | 2 | MoE 122B/A10B | FP8 | MTP n=2 | — | depth-sweep sibling |
| `qwen-3.5-122b-tp` | 256K | 2 | MoE 122B/A10B | FP8 | MTP n=3 | — | depth-sweep sibling |
| `qwen-3.5-122b-mtp4` | 256K | 2 | MoE 122B/A10B | FP8 | MTP n=4 | — | depth-sweep sibling |
| `qwen-3.8-27b-nvfp4` | 256K | 2 | ? | NVFP4 | MTP n=5 | — | |
| `qwen-3_6-27b-nvfp4-tp` | 256K | unset | ? | NVFP4 | MTP n=3 | — | |
| `qwen-3.8-27b` | 256K | unset | ? | bf16 + fp8 KV | MTP n=5 | — | rebuilt TP2 2026-09-09, **untested** |
| `minimax-m2_7-nvfp4-gb10` | 192K | 8 | ? | NVFP4 | none | — | |
| `gemma-4-31b` | 128K | 4 | dense 31B | BF16 ckpt + runtime fp8 | none | **12.0**, cold TTFT 0.99 s / warm 0.11 s | validated 2026-08-29, TP2 |
| `deepseek-r1-distill-qwen-32b` | 128K | unset | dense 32B | bf16 + fp8 KV | none | — | |
| `nemotron-3-nano-30b-a3b-nvfp4` | 128K | 8 | MoE 30B/A3B | NVFP4 | none | — | constructed 09-03, never run |
| `nemotron-3_5-lightning-bf16` | 128K | unset | MoE 30B/A3B | bf16 | none | — | constructed 09-03, never run |
| `llama-4-fp8-tp` | 64K | 16 | MoE 17B / 16E | FP8 | none | — | |
| `llama-4-fp8` | 64K | 16 | MoE 17B / 16E | FP8 | none | — | PP2 |
| `_llama-4-fp8-tp-noep` | 64K | 16 | MoE 17B / 16E | FP8 | none | — | experimental: `--no-enable-expert-parallel` |
| `llama-3.3-70b` | 56K | 16 | dense 70B | bf16 + fp8 KV | none | — | PP2 |
| `qwen-2_5-coder-32b-tp` | 32K | 6 | dense 32B | bf16 + fp8 KV | none | — | |
| `qwen-2_5-coder-32b` | 32K | 6 | dense 32B | bf16 + fp8 KV | none | — | PP2 |
| `llama-4-fp4` | 32K | 1 | MoE 17B / 16E | FP4 | none | — | PP2 |
| `_deepseek-v4-flash-vision-exp` | 128K | 2 | MoE 305B, multimodal | fp8 KV | DSpark n=6 | — | experimental, TP2+EP |

---

## Issues this pass turned up

Listed loudest first. Nothing here is speculative about *what the file says* —
the uncertainty, where it exists, is about whether the file still fails.

**1. FIXED 2026-09-09 (now errata E023) — `nemotron-3.5-lightning-nvfp4`
passed both `--speculative-model` and `--speculative-config`.** `_deepseek-v4-flash-vision-exp.yaml`'s header states
this outright: "This build REJECTS a separate `--speculative-model` flag
(confirmed: our nemotron-3.5 recipe crashed on exactly that)." The crash was
diagnosed, written down in a *different* recipe, and the broken recipe was
never fixed. `_nemotron-3.5-lightning-nvfp4-tools` inherits it. This is
`errata.yaml` rule material and the clearest single argument for the
known-bad-flag linter.

**2. `muse-glimmer-30b` uses `vllm/vllm-openai:nightly` with no
`entrypoint: ""`.** That base sets `ENTRYPOINT ["vllm","serve"]`, and Docker
appends rather than replaces — the exact mechanism `glm-5_3-flash-nvfp4-mtp`
documents at length after it cost a nine-minute weight load to diagnose. Every
other recipe on an ENTRYPOINT-bearing image (`glm-5_3`, `gemma4-aeon-dflash`)
carries `entrypoint: ""`. This one doesn't. I have not run it, so: suspect, not
confirmed.

**3. FIXED 2026-09-09 — two 2-node topologies paired MTP with `pp_size: 2`,
which cannot work.** `qwen-3.8-27b` and `qwen-3.8-27b-nvfp4-sqk2`. This is not a
suspicion — TOMBSTONES #104 records it as a hard upstream vLLM
`NotImplementedError` raised at engine-config-creation time, before any weight
load: the MTP draft model class fails its own `SupportsPP` check. **The failure
is about the method, not the checkpoint**, so it applies to any recipe pairing an
MTP-family draft head with `pp_size > 1`. There is no fix — MTP on this cluster
requires `tp_size` or single-node, never PP.

The precedent is already set twice over. #104's original victim,
`qwen-3.6-27b-nvfp4`, was rebuilt as TP and is now `qwen-3_6-27b-nvfp4-tp`;
#107 retired `qwen-3.5-122b.yaml` for the same reason and rebuilt its
depth-sweep siblings as TP. These two were missed in that sweep.

They fail cheaply, at config validation before any GPU cost, which is also why
nobody noticed. #104 names itself a candidate for the known-bad-flag linter.

Separately, **`qwen-3.8-27b` has no `image:` field at all**, so it falls back to
`default_image` — the `default_image`-has-no-Ray trap of TOMBSTONES #43, fixed
across three recipes in #103, and which `nemotron-3_5-lightning-bf16`'s header
describes fixing proactively "rather than let it recur a third time." It
recurred anyway, in a file nobody re-read. That one is second in line: #104
kills the deploy first.

**4. `gemma-4-31b` — RESOLVED 2026-09-09, and both sources were describing
something real.** I had flagged a contradiction: `REFERENCE-decode-speeds.md`
called it the one recipe with "neither quantization nor speculative decoding"
while the recipe passes `--quantization fp8`. The retired `TROUBLESHOOTING.md`
settles it. The **checkpoint** is BF16 — `google/gemma-4-31B-it`, and errata
E009 records that no `google/…-FP8` repo exists at all — and `--quantization
fp8` is **runtime dynamic quantization** applied on top. So it is not an
unquantized control case. The "~3.2x from quantization" figure is really
BF16-checkpoint-plus-runtime-fp8 versus a pre-quantized NVFP4 checkpoint,
which is a meaningful comparison but not the one the doc claims. Worth
correcting where it is cited. WORKSTREAMS §3a item 13 (try an NVFP4 quant of
this model) survives — just not on the grounds that the recipe is currently
unquantized.

**5. Three recipes declare `_TEST` in their header but aren't `_`-prefixed:**
`gemma4-26b-a4b-nvfp4-tools`, `muse-glimmer-30b-nvfp4-dflash-tools`,
`qwen-3.6-35b-a3b-hauhaucs-nvfp4-nospec`. Under the convention you just stated,
they're mislabeled in the dashboard dropdown — where the `_` prefix is the only
signal a user gets.

**6. `deepseek-v4-flash-0731-1M`'s comment contradicts its own field.** The
comment says "gpu_util dropped 0.90 -> 0.82"; `gpu_util:` is `0.75`.

**7. `VLLM_USE_V1=0` is vestigial** in `llama-4-fp4`, `llama-4-fp8`,
`llama-4-fp8-tp`, `qwen-2_5-coder-32b` and `_llama-4-fp8-tp-noep`. It was half
of TOMBSTONES #43's fix — "use `ray` **and** set `VLLM_USE_V1=0` to force the V0
cross-host executor." #113 later confirmed the variable no longer exists in vLLM
on the current build, scoping that rule rather than deleting it, and
`deepseek-v4-flash-0731-1M`'s header records it as "confirmed ignored on this
image." Harmless, but it reads as load-bearing to anyone copying a recipe, and
the surviving half of #43's fix — the Ray flag — genuinely is.

**8. `qwen-3.6-35b-a3b-nvfp4`'s header cites A/B-rejected variants "kept as
`_`-prefixed test recipes."** No such files exist in `recipes/local/`. Either
deleted or never committed — the measurements survive only in that header.

---

## Relative strengths

Deliberately not filled in yet. The only workload-differentiated data in the
catalog is the Gemma4 DFlash-vs-MTP comparison, and it is a good argument for
why this needs its own pass rather than a column of guesses: DFlash wins coding
by 54% and extraction by 178%, and then ties MTP dead level on prose. A single
"good for" label would have hidden that.

Doing it properly means a fixed prompt suite run against each validated recipe —
which is what `tests/ab_test.py` and `tests/agentic_eval.py` already do
piecewise. That's a workstream, and it should land in `WORKSTREAMS.md` before
this section grows a table.

Two things worth capturing when it does: `qwen-3.6-35b-a3b-nvfp4` is recorded as
tool-calling verified multi-turn and 5/5 on the coding suite, and
`qwen-3.6-35b-a3b-nvfp4-nothink` exists specifically because the thinking mode
scored 0/5 on that same suite.
