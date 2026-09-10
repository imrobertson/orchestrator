# FlashInfer/vLLM MoE autotune internals — a map, not a fix

**Purpose:** if a future TP-parallel MoE deploy shows one rank hitting the
FlashInfer autotune cache instantly while another rank runs a long,
repeated `[AutoTuner]: Tuning ...` sweep — possibly ending in a hang or a
Gloo/NCCL transport crash — read this first. It won't tell you the cause;
it will tell you, in minutes rather than fifteen rounds of grep, which
parts of this failure class are already understood, which are ruled out,
and exactly where to look next.

Full narrative, evidence, and reasoning: `TOMBSTONES.md` #116–#126. This
document is the distilled, fast-reference version of that arc — read the
tombstones if you want the "why," read this if you want the "where."

**Provenance and a real caveat about it:** everything below was confirmed
against `eugr/spark-vllm-b12x:latest` at
`vllm.__version__ == "0.1.dev20482+g83cb22a0e.d20260903"`, FlashInfer
`0.6.18`, GB10/`sm121`. Function *names* and their *relationships* are
almost certainly durable across nearby versions. Exact line numbers are
not — `:latest` has already moved at least twice during the investigation
this document summarizes (see TOMBSTONES #113, #118). Verify identity
before trusting any line number below:

```bash
docker run --rm eugr/spark-vllm-b12x:latest python3 -c "import vllm; print(vllm.__version__)"
```

If that doesn't match the string above, treat every line number here as a
starting guess, not a citation — re-grep for the function name instead.

---

## Quick triage — cheapest checks first, run these before anything else

**1. Is `enable_expert_parallel` actually active?** (seconds, read-only,
no deploy needed)

```bash
docker run --rm eugr/spark-vllm-b12x:latest python3 -c "
from vllm.engine.arg_utils import EngineArgs
import dataclasses
for f in dataclasses.fields(EngineArgs):
    if 'expert' in f.name.lower(): print(f.name, '=', f.default)
"
```

Then check the **specific deploy's own boot log** for its `non-default
args: {...}` line — if `enable_expert_parallel` isn't in that dict, the
default above is what's actually running. **Do not** use the presence of
`TP rank X, EP rank Y` in the boot log as a substitute for this check —
confirmed in #124/#125 that this print is unconditional bookkeeping in
`vllm/distributed/parallel_state.py`, gated on nothing, present regardless
of whether EP is active. This was the single biggest false lead in the
whole investigation; don't re-walk it.

**2. Get the suppressed per-tactic detail directly, instead of inferring
divergence from cache-hit/miss behavior.** Every skipped-tactic summary
line ends with `"(enable debug logs to see details)"` — that's a
`logger.info` pointing at separate `logger.debug` calls elsewhere in the
same loop that log the actual shape and failure reason per tactic
(`autotuner.py`, inside the `except Exception as e:` branch feeding
`choose_one`'s inner loop). Setting `VLLM_LOGGING_LEVEL=DEBUG` in the
recipe's `env_vars` for one deploy should surface this directly on both
ranks — turning "the ranks seem to diverge somehow" into something you can
read side-by-side, without needing this whole document's archaeology
again. Not yet tried against a real deploy; a real, low-cost next step if
this exact class recurs and you want ground truth fast.

**3. Confirm whether both ranks are genuinely hitting `choose_one` for the
same set of ops.** The boot log already shows this per-rank if you diff
the two full container logs (`docker logs`, captured before teardown —
see WS-7 on retaining these) side by side for `Config cache hit for
<op>` / `[AutoTuner]: Tuning <op>` lines. If one rank shows zero live
tuning lines at all for an op the other rank spends minutes on, that's the
signature this whole document is about.

---

## The call chain — what's safe, what isn't, and why

```
vllm/model_executor/warmup/kernel_warmup.py
  flashinfer_autotune(runner)
    is_leader = (world.rank_in_group == 0)          # TP0, always, in this cluster's convention
    ├─ cache LOAD: only `is_leader` reads cache_path from disk,
    │  broadcasts raw bytes to all ranks via world.broadcast_object(src=0)
    │  -- every rank then writes + loads the SAME broadcast content.
    │  SAFE: both ranks end up with identical baked-cache contents.
    │
    ├─ world.barrier()  ── OUTER barrier, called by every rank unconditionally.
    │  CONFIRMED SYMMETRIC (#117). Not where a hang originates.
    │
    ├─ runner._dummy_run(...)  inside fi_utils.autotune(tune_mode=True)
    │  -- this is where AutoTuner.choose_one() gets called, once per
    │     (custom_op, real shape) the model's forward pass actually visits
    │
    ├─ world.barrier()  ── second outer barrier, also symmetric
    │
    └─ cache SAVE: `if is_leader: tuner.save_configs(...)`
       ASYMMETRIC BY DESIGN (#120) -- only rank 0 ever persists anything,
       even though save_configs()'s own merge logic (re-read + per-key
       merge) would safely support every rank saving. TP1's freshly-tuned
       results, if it needed any, are discarded when its process exits.
       AND: /root/.cache/vllm/flashinfer_autotune_cache/ is NOT bind-
       mounted anywhere (#116) -- even a patched save-every-rank fix
       would write to ephemeral container storage. Both pieces would be
       needed together for this to ever self-heal.

flashinfer/autotuner/autotuner.py
  class AutoTuner:                    # process-local singleton (_instance,
                                       # _class_lock) -- NOT shared across
                                       # ranks; each RayWorkerProc is a
                                       # genuinely separate OS process.

    choose_one(custom_op, runners, tuning_config, inputs, ...)
      profiles = self._generate_optimization_profiles(tuning_config, inputs)
      # profiles' LENGTH comes from tuning_config alone -- identical
      # across ranks by construction. This outer loop is SAFE.
      for p in profiles:                              # <- the "0/21..21/21" bar
          is_cache_hit, ... = self.search_cache(...)   # per-profile cache check
          if not is_cache_hit:
              for r_id, r in enumerate(runners):
                  valid_tactics = r.get_valid_tactics(tensors, p)
                  for tac in valid_tactics:
                      try:
                          time_measured = self._profile_single_kernel(...)
                      except torch.cuda.OutOfMemoryError:
                          # CONFIRMED (#118) safe when a tune group is set:
                          # marks tactic failed (inf), keeps looping in
                          # lockstep, preserves collective cardinality.
                          # Only unsafe (early-return) when NO tune group
                          # is set -- not our case (kernel_warmup.py always
                          # sets one when world_size > 1).
                      except Exception:
                          # Same lockstep-preserving treatment. Also safe.

    _profile_single_kernel(...)
      # THE INNER COLLECTIVE. dist.all_reduce() here, once per tactic,
      # so per-tactic timing noise doesn't let different ranks pick
      # different winners. Code's own comment (paraphrased): every rank
      # MUST reach this exactly once per call, in lockstep, or the NEXT
      # tactic's reduce deadlocks. CONFIRMED REAL by direct source read
      # (#118) -- this is where the original py-spy trace pointed.

    rank_tactics(custom_op, runners, tuning_config, inputs, k, ...)
      # A SEPARATE tuning entry point, called from inside runner-specific
      # get_valid_tactics() implementations (see below) -- NOT from
      # choose_one directly. Used for multi-stage compound-tactic
      # refinement (e.g. MoE's gemm1/gemm2 pair).
      valid_tactics = runner.get_valid_tactics(tensors, profile)
      for tac in valid_tactics:            # <- THIS loop's LENGTH is
          time_measured = self._profile_single_kernel(...)
          #                    ^ same inner collective as above.
      # UNSYNCHRONIZED (#126): nothing here guarantees `valid_tactics`
      # comes back the same LENGTH on every rank. If it doesn't --
      # for ANY reason, EP or otherwise -- every call inside this loop
      # goes out of lockstep with the inner all_reduce, for exactly as
      # many extra/missing iterations as the length differs by.
      # THIS IS THE LEADING STRUCTURAL CANDIDATE for the observed hang,
      # confirmed real, never confirmed as THE cause.
      # ALSO EXPLAINS THE TIMELINE: because this lives one level below
      # choose_one's own outer bucket loop, the divergence can occur
      # mid-way through an otherwise-normal-looking sequence of completed
      # "0/21 -> 21/21" cycles -- not necessarily on the first one.

flashinfer/fused_moe/runners.py
  class MoERunner(TunableRunner):
    def get_cache_key_extras(self) -> tuple:
      # Includes `local_expert_offset` -- CONFIRMED rank-divergent under
      # genuine expert-parallelism (#119). CONFIRMED NOT the cause for
      # llama-4-fp8-tp specifically, because EP was confirmed inactive
      # on that deploy (#125). Real mechanism, wrong incident.

  class _CutlassRunnerBase(MoERunner):
    def get_valid_tactics(self, inputs, profile) -> List[Any]:
      # Calls tuner.rank_tactics() internally, once each for gemm1 and
      # gemm2 (matches the observed log pattern exactly). This is the
      # `runner.get_valid_tactics()` called from inside choose_one's
      # own loop above -- the nesting is real, not a guess.

    def _build(self):
      module = get_cutlass_fused_moe_module(str(self._device_arch))
      self._inner = module.MoERunner(...)
      # `self._inner` is NOT the class above -- it's a dynamically
      # defined class living INSIDE get_cutlass_fused_moe_module(),
      # wrapping a JIT-compiled CUDA extension.

flashinfer/fused_moe/core.py
  def get_cutlass_fused_moe_module(backend, ...):
    module = gen_cutlass_fused_moe_sm120_module(...).build_and_load()
    # <-- THE WALL. JIT-compiled at runtime, architecture-specific.
    # The MoERunner defined here is Python, but its actual
    # get_valid_tactics()/forward() almost certainly delegate into
    # compiled C++/CUDA with no further .py source to read.
    # Constructor takes tp_size, tp_rank, ep_size, ep_rank explicitly --
    # a live, UNREAD thread: whether ep_size/ep_rank get constructed
    # from something independent of `enable_expert_parallel` was never
    # checked. Tracing stopped here deliberately (#126), not from lack
    # of a next question.
```

---

## What's confirmed, what's ruled out, what's still open

| Claim | Status | Source |
|---|---|---|
| Outer `world.barrier()` calls are symmetric across ranks | **Confirmed** | #117 |
| `_profile_single_kernel()` has its own per-tactic `all_reduce()` | **Confirmed** | #118 |
| OOM/generic exceptions inside `choose_one`'s inner loop preserve cardinality *when a tune group is set* | **Confirmed** | #118, verified against actual source in this doc's chase |
| `MoERunner._cache_key_extras()` includes rank-divergent `local_expert_offset` under genuine EP | **Confirmed as code** | #119 |
| EP was active on the `llama-4-fp8-tp` deploy that hung | **Disconfirmed** | #123–#125, direct source read of `parallel_state.py` |
| `TP rank X, EP rank Y` boot log line is gated on `enable_expert_parallel` | **Disconfirmed** — unconditional print | #124–#125 |
| `flashinfer_autotune()`'s save is leader-rank-only; cache path isn't host-persisted | **Confirmed** | #120 |
| `rank_tactics()`'s inner loop length is unsynchronized across ranks | **Confirmed as code, real risk** | #126, this doc |
| This unsynchronized length is what caused `llama-4-fp8-tp`'s specific hang | **Never confirmed** | stopped at JIT wall, #126 |
| `ep_size`/`ep_rank` in the compiled runner's constructor are independent of `enable_expert_parallel` | **Unread** | flagged, not pursued |
| Actual root cause of `llama-4-fp8-tp`'s hang | **Unknown** | — |

---

## Methodology notes worth keeping independent of FlashInfer entirely

- **Verify build identity via the image tag, never a resident container's
  name.** `docker run --rm <image>:<tag> ...` for every check. A resident
  container named e.g. `vllm-head` can be running an entirely different
  model on an entirely different image by the time you get around to
  checking it (this happened once during this investigation — see
  TOMBSTONES #123).
- **A version string containing a git hash
  (`+g<hash>.d<date>`, standard `setuptools_scm` format) is a resolvable
  commit** — fetch the exact matching source from GitHub at that SHA
  rather than guessing a branch.
- **A JIT-compiled extension (`build_and_load()`, `torch.utils.cpp_
  extension.load(...)`, or similar) is an expected, legitimate stopping
  point for source archaeology**, not a dead end to route around. Naming
  it precisely and stopping is more useful than a fourth grep pretending
  the .py source goes deeper than it does.
- **A grep miss for one exact identifier isn't proof of absence** if the
  same codebase is confirmed to rename that identifier elsewhere (this
  codebase renames `enable_expert_parallel` → `enable_ep` in at least one
  call site) — always grep for known renames too before trusting silence.
