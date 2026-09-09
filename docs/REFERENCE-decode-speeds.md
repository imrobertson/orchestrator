# Decode speed reference — measured, 2026-09-08/09

Every number here was measured on this cluster with `benchmark.py` (3-pass,
temp=0.0, 256-token generation) and is in `benchmark_ledger.csv`. Nothing is
carried over from a community claim.

**Read the RANGE, not the mean.** Where a recipe was benchmarked more than
once, both figures are given. Two claims died today because they were single
measurements that did not survive a repeat — see "On single measurements"
below.

## Single-node

| recipe | warm tok/s | warm TTFT | shape |
|---|---|---|---|
| `gemma4-26b-a4b-nvfp4-tools` | 53.2 | 0.08 s | MoE 4B active / NVFP4 / MTP |
| `gemma4-26b-a4b-nvfp4` | 51.9, 55.3 | 0.07 s | MoE 4B active / NVFP4 / MTP |
| `gemma4-26b-a4b-aeon-dflash` | 49.2, 49.2, 48.8 | 0.08 s | MoE 4B active / NVFP4 / DFlash |
| `muse-glimmer-30b-nvfp4` | 21.4 | 0.43 s | dense 30B / NVFP4 / no spec |
| `gemma-4-31b` | 6.7, 6.7, 6.7 | 0.17 s | dense 31B / **bf16** / no spec |

## Two-node (TP2)

| recipe | context | warm tok/s | warm TTFT |
|---|---|---|---|
| `_glm-5.3-flash-nvfp4-tp2` | 262,144 | 21.3, 23.2, 22.5 | 0.23–0.25 s |
| `_glm-5.3-flash-nvfp4-tp2` | 98,304 | 22.3, 23.1 | 0.22 s |

## What explains the 8x spread

Three factors, and today's runs isolate each one because pairs of recipes
differ in exactly one of them.

**MoE vs dense: ~2.4x.** `gemma4-26b-a4b` (26B total, ~4B active) against
`muse-glimmer-30b` (30B dense), both NVFP4, both single-node: 52 vs 21.4.
Decode on GB10 is memory-bandwidth-bound, and an MoE moves only its active
experts per token.

**Quantization: ~3.2x.** `muse-glimmer-30b` NVFP4 against `gemma-4-31b`
bf16, both dense and near-identical size: 21.4 vs 6.7. Four-bit weights
against sixteen-bit, on a bandwidth-bound path.

**They compound.** MoE+NVFP4 (52) against dense+bf16 (6.7) is 7.8x, which
is 2.4 × 3.2. That is why `gemma-4-31b` at 6.7 tok/s is **the correct
number for what that recipe is**, not a misconfiguration: it is the only
recipe in the catalog running with neither quantization nor speculative
decoding. Whether it earns its place is a workload question — if nothing
needs dense-31B quality over 26B-A4B, it is a ~7x throughput tax for no
benefit. An NVFP4 or FP8 quant of it should land near Muse Glimmer's 21;
that is untested.

**Speculative decoding does not appear as a separate factor here**, because
every fast recipe already has it and the two Gemma4 variants that differ
only in method (MTP ~52 vs DFlash ~49) are within each other's spread on
this prompt. See the caveat below.

## Caveats that matter more than the numbers

**`benchmark.py` sends ONE general-prose prompt.** It cannot see
workload-dependent differences. Task ME measured DFlash beating MTP 103 vs
67 on coding and 203 vs 73 on extraction while tying on prose — and prose is
all this table measures. **Choosing MTP over DFlash on the strength of the
~3 tok/s gap above would be exactly the mistake that finding exists to
prevent.** `tests/ab_test.py --prompts all` is the tool for that comparison.

**Tool-calling is not exercised.** `gemma4-26b-a4b-nvfp4-tools` at 53.2
shows the parser costs nothing on prose decode. It does **not** show the
parser works. Given GLM-5.3's `--tool-call-parser glm` silently swallowing
tool calls (`finish_reason=stop`, `tool_calls=null`, see `TOMBSTONES.md`
#133), a silently-broken parser is a real failure mode a throughput
benchmark cannot detect. One manual `/v1/chat/completions` with a `tools`
array is the check.

**Cold-start TTFT is not comparable across rows.** A first-ever request on
a freshly-built kernel cache paid **52.11 s** TTFT on the AEON recipe;
every subsequent cold start on the same recipe was 0.08–0.44 s. The JIT
caches (`~/.cache/triton`, `flashinfer`, `deepgemm`) persist across
container restarts via bind mounts, so this appears to be once per
image/kernel-cache generation rather than once per deploy — **not
confirmed**, but it fits both observations. Clearing those caches would
presumably resurface it. Decode speed was unaffected (49.5 cold vs 49.2
warm), so it is purely a prefill-path compile.

## GLM-5.3 context: 262K costs nothing measurable

The recipe originally claimed 524288 with a ~507K KV pool. 524288 does not
boot on this hardware (see `TOMBSTONES.md` #133). It was set to 98304 to get
it serving, and then raised:

| context | warm tok/s across runs | mean |
|---|---|---|
| 98,304 | 22.3, 23.1 | 22.7 |
| 262,144 | 21.3, 23.2, 22.5 | 22.3 |

**2.67x the context for no measurable throughput cost.** The spread within
262144 alone (21.3–23.2, 9%) is larger than any apparent difference between
the two settings.

262144 is also upstream's own shipping TP2 configuration. Their pinned
`--kv-cache-memory 3221225472` yields a 310,292-token pool; the utilization
heuristic here arrives at 2.88 GiB unpinned, which is close enough that
pinning has not been necessary.

**Do not take vLLM's `--kv-cache-memory=8017442816` suggestion.** The engine
offers it as "to fully utilize gpu memory", but upstream documents that
concurrency is bounded by free-memory headroom rather than by the pool: at
`4445787956` a 3-way 20K-token prefill drove MemAvailable to 3.06 GB and
their anti-OOM watchdog killed the engine. Their shipping pin is
deliberately *lower* than what fits, for headroom under load.

## Still on the table for GLM-5.3

**DFlash2 is the real performance lever: 46.9 tok/s vs 21.8 for MTP-4 at
TP2/262K upstream — 2.15x — at 74.1% acceptance, and it costs zero KV
pool** (its layers slot-share the MLA tensors the way GLM's own mamba layers
do, so context is unaffected). Not a config change: the vLLM build in the
current image (`0.1.dev20051+g487ecf187`) ships DFlash1 and predates DFlash2
(upstream PR #52816), so it needs a different image plus the 2.2 GB
`incoai/GLM-5.3-Flash-DFlash2` drafter staged on both hosts and an
`extra_mounts` entry. Tracked in WORKSTREAMS.

## On single measurements

Two numbers were nearly written into recipe headers today and did not
survive a repeat:

- GLM at 262144 measured **21.3** on its first run, which read as an ~8%
  cost against 98304's 22.7. Two further runs gave 23.2 and 22.5. There is
  no measurable cost.
- `gemma4-26b-a4b-nvfp4` measured **51.9**, then **55.3** on an identical
  rerun — a 6.5% spread on the same recipe, same host, same prompt.

Both would have been recorded as findings. **The rule that follows: two
runs minimum before a number goes in a recipe header, and record the range
rather than the mean.** Every claim from today that survived scrutiny had
repeats; every one that shifted had a single sample. This is `WORKSTREAMS.md`
F-e's standing complaint, arriving in a case where the repeats existed and
disagreed.
