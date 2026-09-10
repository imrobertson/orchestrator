# Community sources

Where the images, checkpoints, fixes and design patterns in this system came
from — and which ones not to retry.

Nothing here is upstream in the sense of something we track and follow. The
working model is best-of from community posts, cooked into our own recipes:
sometimes starting from eugr, more often from someone else. This file exists so
a recipe's lineage stays traceable after the person who found it has forgotten
where.

**Per-recipe operational detail belongs in the recipe's own header and `notes:`
field, not here.** This file is the index: who, where, and whether it is worth
your time. To know why a specific flag is set, read the recipe.

**Last updated: 2026-09-09.** Absorbs `EUGR-REFERENCE-NOTES.md`,
`EUGR-NOTES-UPDATE-2026-08-29.md` (including its two REPLACE blocks, which
had been specified on 2026-08-29 and never applied), and the reference
material from `BACKLOG-dspark-sm120-image.md`. All three are archivable.

Two directories these notes assume are gone: `eugr-samples/`, which was
`tools/translate_eugr_recipes.py`'s default input, and `examples/`. Both
existed on 2026-08-20 and were later deleted, so instructions elsewhere
pointing at `examples/diffusion-gemma-bf16.yaml` or at a standing
`eugr-samples/` are stale rather than fictional. Ingest still works — pass
`--in` explicitly.

---

## Contents

- [In active use](#in-active-use)
- [eugr / spark-vllm-docker](#eugr--spark-vllm-docker)
- [Community checkpoints](#community-checkpoints)
- [Investigated, not pursued](#investigated-not-pursued)
- [Dead ends — do not retry](#dead-ends--do-not-retry)
- [Upstream issues worth re-checking](#upstream-issues-worth-re-checking)
- [Link index](#link-index)

---

## In active use

Things a deployed recipe depends on right now. Breaking changes here break us.

### tonyd2wild — GLM-5.3-Flash on 2× DGX Spark

Image `ghcr.io/tonyd2wild/vllm-glm53-flash:sm121-v8`, pullable from GHCR with no
auth. The single most load-bearing external dependency in the catalog: it
carries five patches without which GLM-5.3 does not run on SM121 at all — SM90
NoPE sparse-MLA extended to SM121, FlashInfer 0.6.18 nightly (0.6.17 produces
NaN on 64–256-row batches), NCCL pinned to 2.30.7, nvidia-cutlass-dsl pinned to
4.6.2, and PDL disabled on SM12x with the top-k indexer hardened.

Used by `glm-5_3-flash-nvfp4-mtp`. Their published MTP throughput (~21.8 tok/s,
~0.24 s TTFT) reproduces here. Their KV-pool figure does not — see that recipe's
MAX CONTEXT NOTE.

Their DFlash2 result (46.9 tok/s at 74.1% acceptance, 2.15× MTP-4, zero KV cost)
is why DFlash2 is a live workstream. It needs a different image; sm121-v8 ships
DFlash1 and predates vLLM PR #52816.

tonyd2wild also maintains the most information-dense DeepSeek reference we have
— see [Investigated, not pursued](#investigated-not-pursued) for the two
findings from it that were traced and set aside.

### hazyumps — DeepSeek-V4-Flash on GB10

Image `hazyumps/deepseek-v4-flash-gb10:sm121-cu130-20260727d`, built on jasl's
vLLM fork (PR #41834, SM12x enablement). Prebuilt GB10-native, linux/arm64,
sm_121.

Auto-selects FlashInfer SM120 sparse-MLA decode and MARLIN MoE with no
`--attention-backend` or `--moe-backend` hints, which is why those flags are
deliberately absent from the recipes using it. `--distributed-executor-backend
ray` works normally — no no-Ray workaround needed.

Used by `deepseek-v4-flash-0731-dspark` (validated: cold 44.7 tok/s / TTFT
0.12 s, warm 42.7 / 0.13) and `-dspark-512k`. Roughly 3× the ~14 tok/s the stock
`eugr/spark-vllm-b12x` image managed with no working spec-decode path.

Their `docs/TUNING.md` and `docs/BUILD.md` are worth reading in full if you ever
need to build rather than pull.

### AEON-7 — Gemma 4 uncensored + DFlash

Image `ghcr.io/aeon-7/aeon-vllm-ultimate:2026-06-18-v0.23.0-dflashfix`,
checkpoint `AEON-7/Gemma-4-26B-A4B-it-Uncensored-NVFP4` (quantized from
`TrevorJS/gemma-4-26B-A4B-it-uncensored`), drafter
`z-lab/gemma-4-26B-A4B-it-DFlash`.

Not NVIDIA's checkpoint and not the same quantization format —
compressed-tensors NVFP4 via llmcompressor, versus ModelOpt NVFP4 for the
official one. Different content-moderation posture; that is the point of it.

**The image tag is pinned deliberately.** AEON's `:latest` has moved at least
twice since their 144 tok/s claim and now serves their whole model fleet from
one shared image. It was tested here exactly once, against one prompt.

Their reported page-thrashing above `gpu_util` ~0.8 on GB10's shared pool, and
stalling at 0.85, is why `gemma4-26b-a4b-aeon-dflash` runs at 0.65 rather than
this cluster's usual 0.75.

### RedHatAI — GLM-5.3-Flash NVFP4

The current default GLM checkpoint. Weight-only NVFP4 (W4A4
compressed-tensors), 184.26 GiB on disk across 11 large shards, 92.76 GiB
resident per rank under TP2. Loads roughly 2× faster than the LibertAIDAI quant
purely because of shard layout — 11 large files versus 120 small ones — setting
aside that the other one is corrupted.

Needs manual staging to `/var/tmp/glm-5.3-flash-nvfp4` on **both** hosts; the
orchestrator has no prefetch hook for paths outside the shared HF cache mount.

---

## eugr / spark-vllm-docker

A community-maintained toolkit for running vLLM on DGX Spark hardware: a tuned
build/image pipeline, a declarative recipe system, and a "mods" mechanism for
model-specific compatibility fixes. Our working copy is
`imrobertson/spark-vllm-docker-experiments` — the same content, ours to pull
from without depending on upstream availability.

Its images are still the most-used in the catalog by count. But it is **not a
leading edge we track**: it is neither the most current option nor, in several
measured cases, the most efficient. We borrow selectively and cook our own.

### The two images

`eugr/spark-vllm:latest` (mainline) and `eugr/spark-vllm-b12x:latest` (the b12x
fork). Most 1-node and several 2-node recipes run on `-b12x`, which does ship
Ray — evidenced by every working `--distributed-executor-backend ray` recipe
built on it.

Two recorded reasons to fall back from `-b12x` to mainline, each documented in
the recipe that hit it:

- **`Gemma4Proposer._greedy_sample()` crash** on the first real MTP request.
  `gemma4-26b-a4b-nvfp4` and `-tools` both pin mainline for this.
- **B12x plugin NVML crash.** `qwen-3.6-35b-a3b-nvfp4-nothink` pins mainline for
  this.

### Why we translate rather than adopt their schema

The two recipe schemas represent the same information in structurally
incompatible shapes. Confirmed against real files, not the public docs:

- **No `topologies` dict, ever.** One flat `defaults:` dict plus one `command:`
  block-scalar template with `{placeholder}` substitution via plain
  `str.format(**params)` — which is why real recipes carry doubled braces
  around JSON blobs (`{{` escapes to a literal `{`). Node count is not a schema
  field; it is derived downstream by parsing `-tp`/`-pp`/`-dp` back out of the
  rendered command text.
- **`cluster_only` / `solo_only` are whole-recipe booleans**, not per-topology.
  Absent and both-false mean the same thing: no constraint. Our
  `TopologyConfig.cluster_only` nests it instead — and is inert.
- **`container:` is not `image:`.** It is a short logical name
  (`vllm-node`, `vllm-node-b12x`) indirecting into their own build pipeline, not
  a registry ref. The mapping to a real image is a human decision the first time
  each new name appears.
- **No recipe uses pipeline parallelism.** Zero exceptions across 25 real
  recipes. Our own catalog uses PP for some models and TP for others, so the
  TP-vs-PP choice is genuinely per-model on our side — a mechanical converter
  defaulting either way would silently get some models wrong. The translator
  hardcodes `pp_size: 1` on that basis.
- **`name:` is a human-readable display string** on their side, never a
  filename-matching identifier. We had assumed otherwise by analogy with our own
  since-removed `name:` field.
- **`max_model_len: auto`** appears in real recipes and cannot be resolved from
  the file alone. The translator refuses rather than inventing a number.

`tools/translate_eugr_recipes.py` automates all of that except the `container:`
mapping. Tested against five real files: four translated cleanly, one
(`deepseek-v4-flash-0731`, the `auto` case) correctly refused.

**The schema-adoption question was asked directly and answered no.** Their shape
is optimized for a human at a terminal overriding flags interactively, with
nobody watching a given deploy in our case; and it would put `tensor_parallel`,
`max_model_len`, `host` and `port` in two places at once — structured data *and*
a placeholder buried in free text. That is the same two-sources-of-truth failure
class that emptied our whole catalog when the old `name:` field disagreed with
its filename. Translating at the boundary also means an upstream schema change
is a diff in one file rather than a break smeared across the deploy path.

> **Translator gotcha, worth re-reading before touching it.** An early version
> re-quoted a rendered token only if it contained a space.
> `--diffusion-config '{"canvas_length":256}'` has no space, so it went out
> unquoted — fine until the orchestrator's own `shlex.split()` runs on the
> *stored* string at deploy time and consumes the bare double-quotes as shell
> syntax, corrupting the JSON into `{canvas_length:256}`. Invisible in the YAML;
> only appears after a second parse. Fixed by unconditionally `shlex.quote()`-ing
> every token on rejoin. Any change to the tokenize/rejoin logic must re-run the
> `json.loads()`-after-second-`shlex.split()` round-trip check, not just eyeball
> the output.

### Mods: borrow the format, reject the delivery

eugr applies mods via `docker exec` after the container launches, which is why
they were once described here as "runtime patches." **That describes their
delivery timing, not what the mods are.** Read properly on 2026-08-29: of ~20
mod directories, all but one are *build-time modifications of the installed
vLLM* — `git apply` of source patches, in-place Python rewrites, a `.pth`
site-packages hook, file drops. They only work in eugr's system because their
containers start idle and vLLM is launched afterwards.

| Mod | What it actually does |
|---|---|
| `gpu-mem-util-gb` | in-place rewrite of **8** vLLM source files to add a `--gpu-memory-utilization-gb` CLI arg; self-validates with `ast.parse`, idempotent via `already=` guards |
| `diffusiongemma` | several `git apply` patches (attention, content-channel sanitizer, streaming reasoning) plus a chat-template drop |
| `fix-gemma4-tool-parser` | `curl`s vLLM PR #38909 and `git apply`s it |
| `fix-glm-4.7-flash-AWQ`, `fix-Salyut1-GLM-4.7-NVFP4` | vendored `.patch` files against vLLM source |
| `fix-qwen3-coder-next` | ships `_triton_alloc_setup.pth`, a site-packages hook run at Python startup |
| `fix-qwen3.5/3.6-chat-template` | copies a chat template into `$WORKSPACE_DIR` |
| `use-official-vllm`, `use-ngc-vllm` | base-image swap — no equivalent needed, our `image:` field already does this |
| `drop-caches` | the sole genuine runtime mod — see below |

**`gpu-mem-util-gb` is the decisive one.** It patches
`vllm/engine/arg_utils.py` to register a CLI argument parsed at process startup.
No exec-based mechanism can apply it in time. It was one of the two mods
previously prioritised for porting, which is how the flaw surfaced. It also
confirms an earlier speculative hope: specifying memory in absolute GiB rather
than a fraction genuinely does fit GB10's shared-memory model better than a
static `gpu_util_ceiling`. It is a large multi-file patcher, though, not a
trivial port.

**`drop-caches` is not a container mod at all.** A `nohup` loop running
`sync; echo 3 > /proc/sys/vm/drop_caches` every 60 s for the container's
lifetime. `/proc/sys/vm/drop_caches` is **not namespaced** — writing it inside a
container acts on the host. eugr wraps it as a mod because an on-node container
was their only execution surface; we have an off-node control plane that already
runs commands against hosts over SSH. Deferred: we have no recorded
`fastsafetensors` stall on our own hardware.

Their other memory-pressure lever sits outside the mod system entirely:
**`earlyoom` is baked into the image** and invoked via a `launch-cluster.sh
--earlyoom` flag with tuned thresholds. Same territory as our own OOM-watchdog
gap, but it is a property of their image and launcher rather than a portable
finding — noted so the option is on the table, not as a recommendation.

**`earlyoom` is the second instance of the same pattern, and it is worth a
look.** eugr bakes the `earlyoom` userspace OOM daemon into their image and
exposes it through their launcher as `launch-cluster.sh --earlyoom`, with
tuned thresholds. Structurally this is `drop-caches` again: a host-level
tool wrapped as a container concern because an on-node container was their
only execution surface. We have an off-node control plane that already runs
commands against hosts over SSH, so if we want it, we want it as a host
service, not as a mod.

Unlike `drop-caches` — deferred because we have no recorded
`fastsafetensors` stall of our own — **we do have the problem this
addresses.** TOMBSTONES #71 is Ray's memory monitor OOM-killing a worker as
unified-memory headroom ran out over hours; WS-9's 512K long-session soak
exists to watch for exactly that; and tonyd2wild's `--kv-cache-memory`
finding is their own watchdog killing an engine at 3.06 GB MemAvailable.
A userspace killer that acts on a threshold you choose, before the kernel's
does, is a plausible mitigation for a class of failure that has already
cost us a worker.

> **What is known vs. reasoned here.** Known from the eugr review: they
> bake it in, the flag exists, the thresholds are tuned. Reasoned, not
> observed: that `earlyoom` inside a container reads the host's
> `/proc/meminfo` (it is not namespaced without lxcfs) while only being
> able to signal processes in its own PID namespace — which is what would
> make the in-image placement awkward for us specifically. **Verify that
> before designing anything around it.** Nobody here has run it.

Three findings to carry into any mod work of our own:

1. **A mod can fetch from the network at apply time.** `fix-gemma4-tool-parser`
   `curl`s a GitHub PR diff. On a per-host bake that is a correctness hazard,
   not a style problem — two hosts baking at different moments could produce
   different images, presenting as a rank-dependent crash. It also breaks under
   offline mode. **Any mod we port must have its payload vendored.**
2. **Mods can be silently coupled to `vllm_args`.** Chat-template mods drop a
   file, and the recipe must *separately* pass
   `--chat-template fixed_chat_template.jinja`. Correct only together, with
   nothing enforcing the pairing — same class as the known-bad-flag linter, and
   worth folding into it.
3. **`WORKSPACE_DIR` is load-bearing.** Those mods write to `$WORKSPACE_DIR`,
   which eugr's launcher sets to the container's default working directory. Any
   port must set it to the image's real `WorkingDir` or the payload lands where
   vLLM won't look.

Three smaller observations worth keeping: their changelog mentions a
`--keep-entrypoint` flag, so entrypoint preservation is a real concern in their
tooling too; their `apply_mod_to_container()` exits non-zero on any `run.sh`
failure rather than continuing, which is correct behaviour worth copying — a
half-applied patch set is worse than a refused deploy; and a
`cluster_only`/`solo_only` mismatch produces a hard error whose message includes
the next-step commands to run. That last is a message *shape* worth borrowing
wherever we refuse an operation — our own HTTP 423 reserved-host refusal is the
closest analogue.

### What we skip

`launch-cluster.sh` / `autodiscover.sh` as an execution engine — peer-to-peer
SSH between the Sparks is exactly the on-node brittleness the off-node control
plane exists to avoid. `docker save` / `load` distribution, unnecessary while we
consume registry images. Their build-time `docker/patch_vllm_*.py` and top-level
`*.patch` files: we don't compile vLLM. (Note the reason once given for that —
"we pull prebuilt tags, we don't build images" — is now only partly true, since
we bake derived layers. The conclusion stands, the reasoning doesn't.)

### One thing we took that isn't code

Their `cluster_only` / `solo_only` mismatch does not fail with a bare
validation error. It raises a hard error whose message names the
contradiction and gives the next command to run. The original eugr review
flagged that message shape as worth copying.

Recording it here because **it was copied, and it has since been asserted
rather than merely intended.** `check_reserved_hosts()` names the offending
host and what is recorded as running on it, so a teardown dialog can say
what is about to be destroyed. The `gpu_util_ceiling` refusal names all
three remedies — set the exemption, lower the value, or move enforcement to
`warn` — and `tools/verify/verify_gpu_ceiling.py` section [4] asserts each
of those strings is present, so the message cannot silently degrade into an
unhelpful one. `errata.yaml` E020 and E025 carry the same shape in their
`remedy:` fields.

The generalized rule, which is ours now rather than borrowed: **a refusal
should name what to do next, and something should assert that it still
does.** A refusal that only says no is a refusal someone works around.

### Bus factor

~25 top-level recipes plus `3x-`/`4x-`/`8x-spark-cluster/` subdirectories, ~20
mod directories, largely single-maintainer. Treat it as a dependency with real
bus-factor risk, not infrastructure to lean on. Pin anything synced; don't track
their `main` live.

### Not yet reviewed

Still unread, in priority order if anyone pulls more:

1. `recipes/4x-spark-cluster/*.yaml` and
   `recipes/8x-spark-cluster/glm-5.2-nvfp4.yaml` — real N>2 examples, the
   closest prior art we have for Phase 3 and for `compute_config_hash()`
   topology-key stability.
2. `tests/expected_commands.sh` and `tests/test_recipes.sh` — possibly a
   golden-command regression suite worth mirroring.
3. `.env.example` — canonical field names, mostly informational.

---

## Community checkpoints

Quantizations and finetunes we deploy but did not produce.

| Source | Checkpoint | Used by |
|---|---|---|
| unsloth | `Qwen3.6-35B-A3B-NVFP4`, `Qwen3.8-27B-NVFP4` | the Qwen 3.6/3.8 NVFP4 recipes |
| Inferact | `Muse-Glimmer-30B-NVFP4-W4A4` | `muse-glimmer-30b-nvfp4*` |
| meta-models | `Muse-Glimmer-30B`, `Muse-Glimmer-30B-assistant` (DFlash drafter, 5.11 GB, block_size 16) | `muse-glimmer-30b*` |
| sakamakismile | `Qwen3.6-27B-Text-NVFP4-MTP` | `qwen-3_6-27b-nvfp4-tp` |
| lyf | `Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-NVFP4` | `qwen-3.6-35b-a3b-hauhaucs-nvfp4-nospec` |
| saricles | `MiniMax-M2.7-NVFP4-GB10` | `minimax-m2_7-nvfp4-gb10` |

---

## Investigated, not pursued

Real options that were evaluated and set aside, recorded so the same evaluation
isn't paid for twice.

**tonyd2wild's DSpark shared-expert loader bug.** Real and well-documented, +69%
decode elsewhere (25.7% → 60.2% draft acceptance on the official 0731
checkpoint). Traced our image's actual loader source
(`vllm/models/deepseek_v4/nvidia/dspark.py`) line by line: it already carries the
complete shared-expert tensor mapping their patch adds, and the markov-tensor
name collision the patch guards against cannot occur here due to a different code
structure. **Confirmed not applicable to this image** — traced, not assumed. Our
38–46% draft acceptance therefore stands unexplained by this bug; most likely
prompt-content dependence, since their own patched numbers ranged 33–78% by
content type.

> The full write-up was cited as `REFERENCE-dspark-shared-expert-fix.md`.
> **That file has never existed in this repository.** Both documents that
> cited it — `BACKLOG-dspark-sm120-image.md` and `TROUBLESHOOTING.md` — are
> now archived, so this paragraph is the last place the claim is made. The
> **conclusion** survives above and is what matters: the shared-expert
> mapping is already present in our image, traced line by line, so
> tonyd2wild's patch does not apply. The line-by-line trace itself is gone
> and would have to be re-derived from their repo.

**NVFP4 KV cache (`nvfp4_ds_mla`) as a context-stretch lever.** Researched via
tonyd2wild's separate heavily-patched runtime — three staged Docker builds, not a
flag. Their own measurement: NVFP4 versus fp8 KV cache has **zero effect on draft
acceptance or speed**; its only benefit is KV pool size. Given comfortable pool
headroom at 512K / `max_num_seqs: 1` on plain fp8, this is only worth it if
pushing toward ~1M *with real concurrency*, and it is a separate heavier project
(a new runtime lineage), not a recipe edit.

**`FLASHINFER_CUTLASS` MoE backend for GLM-5.3.** Runs, per tonyd2wild, but
`marlin` was kept for the validated config.

**`--kv-cache-memory` pinning on GLM-5.3 — actively retracted.** This was once
written up as "the most promising lead" for more context. Upstream documents
concurrency being bounded by free-memory *headroom* rather than pool size: at a
value *lower* than the one vLLM itself suggested, a 3-way 20K prefill drove
MemAvailable to 3.06 GB and their watchdog killed the engine. Their shipping pin
is deliberately below what fits. **Do not reach for this.**

**PP=2 for 2-node DeepSeek.** Abandoned for TP=2. Every external 2×GB10
DeepSeek-V4-Flash recipe found — tonyd2wild, hazyumps, MiaAI-Lab, vLLM's own
recipes page — uses TP2; PP2 was untested anywhere else for this model/hardware
pair.

**drowzeys' concurrency patch for DSpark.** Relevant only if pushing high
concurrency and long context together.

**MiaAI-Lab's alternative image** (`ghcr.io/anemll/dspark-vllm-gx10`) — never
tried. Currently set on `_deepseek-v4-flash-vision-exp`, which is experimental
and unvalidated.

**`vllm/vllm-openai:deepseekv4-flash-vision`** — the official dedicated vision
tag from recipes.vllm.ai; the documented fallback if the hazyumps image's MoonViT
vision tower fails to load.

---

## Dead ends — do not retry

- **`orthozany/vllm-jasl-dsv4:pr41834-2026-05-13`** — x86_64 only, confirmed no
  arm64 build exists, fails immediately on GB10 with `Exec format error`. Don't
  revisit unless an arm64 tag appears. This was once recommended in a session
  handoff as the fix for DSpark throughput; that recommendation was wrong, and
  the problem it targeted is solved by the hazyumps image.
- **`LibertAIDAI/GLM-5.3-Flash-NVFP4`** — ModelOpt token corruption, vLLM
  #54150. Use RedHatAI's instead.
- **`--speculative-model` as a separate flag** — rejected by our vLLM builds.
  Drafters go in `--speculative-config`'s `model` field. This crashed the
  nemotron-3.5 recipe, which still carries the flag; see `MODELS.md`.
- **`--moe-backend marlin` on mixed FP8/NVFP4 checkpoints** — `marlin` is
  NVFP4-only. The unsloth `Qwen3.6-35B-A3B-NVFP4` quant is mixed (group_0 FP8
  for attention/linear_attn/lm_head plus MoE experts on layers 32–39; group_1
  NVFP4 for MoE gate/up/down on all layers) and vLLM's resolver refuses it with
  `moe_backend='marlin' is not supported for unquantized MoE`. Omit the flag and
  let vLLM auto-select. This caused both 2026-09-05 load failures.
- **`VLLM_USE_V1=0`** — confirmed ignored on these images. Still present in five
  recipes as dead weight.

---

## Upstream issues worth re-checking

| Ref | Subject |
|---|---|
| vLLM #41834 | jasl's SM12x enablement — the fork our DeepSeek image is built on. Re-check periodically whether GB10/SM120 support has landed upstream, which would obsolete the fork dependency entirely |
| vLLM #46995 | DSpark, merged into vLLM main — same question |
| vLLM #52816 | DFlash2 support, needed for the GLM-5.3 DFlash2 workstream |
| vLLM #54150 | ModelOpt token corruption in the LibertAIDAI GLM quant |
| vLLM #51562 | Uninitialized kpool class of issue; motivates tonyd2wild's patch 5 |
| vLLM #38909 | Gemma4 tool parser fix, fetched at apply time by an eugr mod |

---

## Link index

**In use**

- hazyumps/deepseek-v4-flash-gb10 — https://github.com/hazyumps/deepseek-v4-flash-gb10
- deepseek-ai/DeepSeek-V4-Flash-0731 — https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-0731 — the Discussions tab (esp. #17) carries real deployment chatter worth searching
- tonyd2wild GLM-5.3 TP2 — https://github.com/tonyd2wild/GLM-5.3-Flash-NVFP4-DFlash2-2x-DGX-Spark
- RedHatAI/GLM-5.3-Flash-NVFP4 — https://huggingface.co/RedHatAI/GLM-5.3-Flash-NVFP4
- our eugr copy — https://github.com/imrobertson/spark-vllm-docker-experiments

**Deep reference**

- tonyd2wild DeepSeek DSpark 1M + NVFP4 KV — https://github.com/tonyd2wild/DeepSeek-v4-Flash-0731-DSpark-1M-NVFP4-KV-2x-DGX-Spark — the single most information-dense source found. Their `RUNTIME-BAKEOFF-2026-07-29.md` and `OFFICIAL_MAIN_PORT_PLAN.md` were never pulled in full; worth reading if chasing the `nvfp4_ds_mla` KV path or wanting the vLLM-main-versus-fork comparison
- drowzeys concurrency patch — https://github.com/drowzeys/Keys-Concurrency-Patch-for-DSpark-DeepSeek-V4-Flash

**Not evaluated, lower priority**

- MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark — alternative image `ghcr.io/anemll/dspark-vllm-gx10`
- al-engr.com blog post and the Level1Techs forum thread — secondary corroboration, skim only for a second data point

**Dead**

- `orthozany/vllm-jasl-dsv4:pr41834-2026-05-13`
- `LibertAIDAI/GLM-5.3-Flash-NVFP4` — https://huggingface.co/LibertAIDAI/GLM-5.3-Flash-NVFP4
