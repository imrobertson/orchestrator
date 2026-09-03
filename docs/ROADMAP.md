# Roadmap: v4 -> v5

Tracks work against the control plane's own API version (`FastAPI(...,
version="4.8.x")` in `dgx-orchestrator.py`), separate from
`ARCHITECTURE-MIGRATION-PLAN.md`'s Phase 1-5 numbering, which tracks the
models.yaml -> recipes/ migration specifically. Put things here when they're
about runtime robustness/behavior rather than the config-format migration.

Each entry: what's wrong today, why it matters, and a rough shape for the
fix -- not a full spec. Flesh out into a real prompt (see
`PHASE-2-PROMPTS.md` for the style) when it's actually picked up.

## How this document is organised

Entries are grouped by theme rather than by the order they were noticed.
Two groups are worth calling out:

- **Residual gaps from shipped work** -- entries whose headline problem was
  largely fixed in 4.8.x, kept because the *remaining* gap is documented
  there and because the reasoning explains why the current code looks the
  way it does. Lower priority than anything in the earlier sections, but do
  not delete them: several exist specifically to stop a future reader from
  "fixing" something that is deliberate.
- **Phase 3 inputs** -- not work to do now, but decisions that get more
  expensive the longer they're deferred, and that belong in front of whoever
  picks up N-node generalisation.

Within each group, entries are roughly ordered by priority.

---

## Deployment mechanics

### Model-specific mods: bake a derived image layer

**New, 2026-08-29. Priority: HIGH.** Supersedes the `mods:` execution
decision recorded in `ARCHITECTURE-MIGRATION-PLAN.md` on 2026-08-20 (see
"What changed since 2026-08-20" at the end of this entry). This is the
largest open item in this document and the only one that unblocks a whole
class of models rather than fixing a specific defect.

**Context: why this is now load-bearing.** Cutting-edge checkpoints
increasingly require patched vLLM source to load at all, months before the
fix reaches upstream. The triggering case:
`bg-digitalservices/Gemma-4-26B-A4B-it-NVFP4` ships its own
`gemma4_patched.py`, which must replace
`<vllm>/model_executor/models/gemma4.py`, because stock vLLM's loader can't
map that checkpoint's per-expert NVFP4 scale tensors and dies at load with a
`KeyError`. This is not a one-off -- waiting ~6 months per fix means not
running these models at all.

**What exists today:** nothing. `RecipeConfig.mods` is declared in
`common/recipes.py` and validates, but is documented INERT -- excluded from
`_TOPOLOGY_OUTPUT_FIELDS`, never read by `build_catalog_response()` or the
deploy path, and excluded from `compute_config_hash()`. All `docker run`
volume mounts are constructed programmatically from `cluster_config.yaml`
plus hardcoded cache roots; nothing recipe-authored has ever reached a
container's filesystem.

#### The decision

**A mod is a directory containing `run.sh` plus vendored payload files
(eugr's format). It is applied by baking a derived image layer before the
container is ever launched -- not by `docker exec` into a running one.**

Per deploy, the orchestrator resolves the recipe's mod set to a
deterministic tag (`<base-image>-mods-<hash-of-mod-set>`). If that tag
doesn't exist on the host: start a throwaway container from the base image,
run each mod's `run.sh` inside it, `docker commit` to the tag. The real
deploy then runs from that tag with existing entrypoint semantics completely
untouched.

**Confidence.** The format choice and the rejection of exec-based delivery
are grounded in a direct reading of eugr's actual mod library and this
codebase's deploy path -- high confidence, with the falsifiable claims cited
inline below. The bake mechanism itself rests on one unverified assumption
(that `docker commit` preserves image config faithfully on these images),
which is standard documented behaviour but untested here. Step 0 of the
sequence exists to falsify that before any code is written.

#### Evidence: what eugr's mods actually are

This was decided by reading the mod library, not by reasoning from the
format's name. Every mod in `eugr/spark-vllm-docker/mods/` except one is a
build-time modification of the vLLM installation:

| Mod | What it does | Phase |
|---|---|---|
| `gpu-mem-util-gb` | rewrites 8 vLLM source files to add a `--gpu-memory-utilization-gb` CLI arg | bake |
| `diffusiongemma` | several `git apply` patches + chat-template drop | bake |
| `fix-gemma4-tool-parser` | `git apply` of vLLM PR #38909 | bake |
| `fix-glm-4.7-flash-AWQ`, `fix-Salyut1-GLM-4.7-NVFP4` | `.patch` files against vLLM source | bake |
| `fix-qwen3-coder-next` | ships `_triton_alloc_setup.pth` (site-packages hook, runs at Python startup) | bake |
| `fix-qwen3.5/3.6-chat-template` | drops a `.jinja` into the container workdir | bake |
| `use-official-vllm`, `use-ngc-vllm` | base-image swap | N/A -- our `image:` field already does this |
| `drop-caches` | persistent 60s `sync; echo 3 > /proc/sys/vm/drop_caches` loop | **runtime** -- see separate entry below |

**`gpu-mem-util-gb` is the decisive case.** It patches
`vllm/engine/arg_utils.py` to register a new CLI argument. That argument is
parsed at process startup, so a mod applied after the container is
`RUNNING` is unconditionally too late. Any exec-based mechanism fails on
this mod -- and it was one of the two named first candidates for porting.

**`drop-caches` is the only genuine runtime mod, and it isn't a mod for
us.** `/proc/sys/vm/drop_caches` is not namespaced -- writing it inside a
container affects the host. In eugr's on-node architecture a container was
the only execution surface; we have an off-node control plane that already
runs commands against hosts at deploy time. It belongs as a host-side
daemon with deploy/teardown lifecycle, tracked separately below.

**So: one mechanism, not two.** No `phase: image|runtime` field. A two-phase
design was drafted and discarded once the library was actually read -- it
existed only to let two earlier decisions both be right.

#### Why not the alternatives

**Rejected: `extra_mounts: list[str]` on the recipe schema.** (1) Makes
recipe-authored strings into raw host paths bind-mounted into a container
that is `--privileged` on the 2-node path. (2) The file must independently
exist at an identical path on every target host before any deploy, which
nothing guarantees -- `cache_cluster_assets.py` handles weights and images,
not arbitrary files. That converts "unsupported" into "deploys silently
against a host missing the file," the same shape as the host-hardcoding bug
class. (3) A mount can only express file replacement -- it cannot express
`git apply`, a source rewrite, or a `.pth` hook, i.e. most of the table
above.

**Rejected: hand-maintained custom image per patched model.** Needs zero
code change (`image:` is already a string field), but creates a private
image per model rebuilt on every base bump, turns patch-to-vLLM-version
coupling into a human-maintained tag matrix, and erases the fact that vLLM
was modified -- when upstream changes and a patch goes stale, a hand-baked
image gives no signal. The chosen design keeps the baking and removes the
human: the tag encodes the mod-set hash and the recipe declares the mods, so
provenance is *better*, not worse.

**Rejected: eugr's own delivery -- `docker exec` into a running container.**
Their `launch-cluster.sh::apply_mod_to_container()` scps the mod to the
node, `docker cp`s it in, then
`docker exec bash -c "cd <dest> && chmod +x run.sh && ./run.sh"`. This works
for them because their containers start *idle* (a keepalive command), mods
apply, then the launch script is copied in and exec'd. Ours don't: the
1-node path starts the container with the vLLM entrypoint as PID 1, so an
exec'd mod races against vLLM's own startup and loses for anything that
patches code vLLM imports or arguments it parses (see `gpu-mem-util-gb`
above). Restructuring the 1-node path to idle-then-exec would also destroy a
working failure signal -- today, vLLM dying on 1-node kills the container and
`docker ps` catches it; an exec'd engine leaves a healthy-looking container
wrapped around a dead process, which is precisely the failure mode the
"Engine health monitoring" entry exists to fix on the 2-node path.

#### Properties this buys

- **`compute_config_hash()` coverage is free.** `image` is already a hashed
  field. If the mod set resolves *into* the image tag, mod changes
  invalidate the hash with zero changes to hashing logic -- no `sha256`
  schema field, no file I/O added to `build_catalog_response()` (which runs
  on every dashboard status poll), no hash-versioning decision. This was the
  messiest open question in every rejected design and it evaporates.
- **No file-transport problem.** Each node bakes locally from the same base
  plus the same mod directory, so only the small mod directory ships. No
  need to extend `run_ssh()` for stdin (it's `subprocess.Popen` with no
  stdin plumbing today) and no base64-in-argv workaround.
- **The container-path hazard is dodged rather than mitigated.** Hardcoding
  a target like `python3.12/dist-packages/vllm/...` in a recipe would break
  silently on a base-image Python bump -- patch lands where nothing imports
  it, stock loader runs, original error returns, same class as the
  host-hardcoding bug. A `run.sh` running *inside* the container resolves
  the path itself at the moment it matters.

#### Hard constraints

1. **Mod payloads must be vendored in-repo. No network fetches at bake
   time.** `fix-gemma4-tool-parser` is a live example of the hazard: it
   `curl`s a GitHub PR diff and pipes it to `git apply`. If each host bakes
   locally, a non-deterministic mod can produce *different images on head and
   worker*. It also fails outright under the `global_hf_hub_offline` /
   `global_transformers_offline` switches. Porting that mod means pinning the
   diff into the repo, not copying the `curl`.
2. **Bake per-host, not bake-once-and-distribute.** Far simpler (no
   registry, no multi-GB `docker save | ssh docker load`) and safe *only
   because* constraint 1 guarantees determinism. Record that dependency --
   if constraint 1 is ever relaxed, this must be revisited.
3. **`run.sh` must execute with `WORKSPACE_DIR` set to the image's actual
   `WorkingDir`.** Chat-template mods do
   `cp chat_template.jinja $WORKSPACE_DIR/fixed_chat_template.jinja`; get
   this wrong and the file bakes into a directory vLLM never looks in. Folds
   into Step 0's config check, which already inspects `WorkingDir`.

#### Known coupling worth catching later

Chat-template mods only work if the recipe *also* passes a matching flag
(`--chat-template fixed_chat_template.jinja`) in `vllm_args`. The mod and the
flag are correct only together, and nothing enforces the pairing. Same class
as the "Recipe-level guardrails against known-bad flag combinations" entry
above -- worth folding into that linter once both exist, rather than building
a separate check.

#### Open questions

1. **Baked layers accumulate.** Needs the same retention treatment
   `crash_log_retention_days` gives Ray logs. Not urgent; layers are small
   relative to weights.
2. **Where the mod source of truth lives.** `mods/` at repo root is the
   obvious answer -- reviewable, versioned alongside the recipes that
   reference it, and already visible inside the orchestrator container at
   `/app/mods/` via `docker-compose.yml`'s `.:/app` bind mount. Confirm
   `.gitignore` doesn't exclude the payload files (`.patch`, `.pth`,
   `.jinja`, `.diff`) before the first commit, given the credentials-scrub
   history.

#### Implementation sequence

Steps 1-5 are each independently testable. Step 0 gates the design; step 4
gates the implementation. Neither is a formality.

0. **Verify the load-bearing assumption: that `docker commit` faithfully
   preserves image config on these specific vLLM images.** The whole design
   rests on a derived layer behaving identically to its base except for the
   patched files. That is documented `docker commit` behaviour and is
   expected to hold, but has **not been checked against
   `eugr/spark-vllm-b12x` or any GB10 vLLM image**. If it doesn't hold, the
   delivery mechanism is wrong -- not an implementation detail -- and the
   rejected alternatives come back into play. Cheap to falsify, expensive to
   discover late.
   - Diff `docker inspect --format '{{json .Config}}'` between the base
     image and a throwaway commit of it. `Entrypoint`, `Cmd`, `Env`,
     `WorkingDir`, `Labels`, `ExposedPorts` must survive unchanged.
   - Confirm a committed layer with an *empty* mod set launches correctly
     through the existing deploy path -- i.e. `<base>-mods-<hash>` is
     behaviourally identical to `<base>`. If that isn't true, nothing
     downstream can be trusted.
   - Check the NGC-derived base's `ENTRYPOINT` script and NVIDIA container
     runtime hooks still fire from the committed image. These images print a
     CUDA banner and do driver checks at startup; that machinery is the most
     plausible thing to behave differently.
   - Note the image is arm64 on GB10. Nothing about `docker commit` should be
     architecture-sensitive, but this stack already produced one x86-only
     surprise (`orthozany/vllm-jasl-dsv4`, see
     `BACKLOG-dspark-sm120-image.md`) -- don't assume by analogy to x86
     experience.

   If step 0 fails, stop and re-open the delivery decision rather than
   working around it. The rejected alternatives are documented above
   precisely so that's a cheap pivot.

1. **Mod format + loader schema.** Retype `RecipeConfig.mods` from bare
   `list` to a typed model (mod directory name; the orchestrator resolves it
   against `mods/`, so no host paths in recipes). Every existing recipe has
   it empty, so this is a zero-data migration -- verify that against
   `recipes/local/*.yaml` and `recipes/eugr/*.yaml` rather than assuming.
   Shape validation at load (pydantic); existence verification deferred to
   deploy, because `build_catalog_response()` fails closed and one bad mod
   entry would otherwise empty the entire catalog.
2. **Bake + cache + tag resolution.** Deterministic tag from the mod-set
   hash; skip the bake when the tag already exists on the host. Fail loudly
   and abort the deploy if any `run.sh` returns non-zero -- eugr's own
   `apply_mod_to_container()` does exactly this, and a half-applied patch set
   is worse than no deploy.
3. **Deploy-path integration.** One shared helper called from both the 1-node
   and 2-node branches. Given the host-hardcoding history, do not write the
   resolution logic twice.
4. **Prove end-to-end with a no-op mod** -- a `run.sh` that touches one
   harmless file. Validates schema parse -> hash -> bake -> tag resolution ->
   deploy, with the failure mode *not* entangled with "did the real patch
   work." If the mechanism is broken, find that on a harmless file, not on
   the first production use.
5. **Wrap the Gemma patch** as `mods/gemma4-nvfp4/`. Port `gpu-mem-util-gb`
   if the OOM-watchdog gap still wants it (note it is a large multi-file
   patcher, not a trivial port).

#### What changed since 2026-08-20

`ARCHITECTURE-MIGRATION-PLAN.md` resolved this on 2026-08-20 in favour of
eugr's `docker exec` pattern, applied after the container reaches `RUNNING`
and before the health-check poll. That decision was reasonable for what was
known: it was made against the mod *concept*, without reading any `run.sh`.
Reading them showed the two named first candidates both fall outside what
that mechanism can do -- `gpu-mem-util-gb` patches an argument parsed at
startup (too late to exec), and `drop-caches` isn't a container-scoped
operation at all. The 08-20 ordering insight (mods before the health poll)
becomes unnecessary rather than wrong: baking means the image is already
correct before the container starts, so there is no race to sequence around.

**Depends on:** nothing blocking. `EUGR-REFERENCE-NOTES.md` has the mechanism
detail on eugr's side and should be updated to note that we adopt their
format but not their delivery.

---

### Host-side FS cache pressure relief during model load (`drop-caches`)

**New, 2026-08-29.** Split out of the mods design above, where it was
originally miscategorised as a mod to be ported.

**Context:** eugr's `mods/drop-caches/run.sh` starts a `nohup` background
loop running `sync; echo 3 > /proc/sys/vm/drop_caches` every 60 seconds for
the container's lifetime, writing a PIDFILE so it can be stopped. Per their
changelog it exists to stop `fastsafetensors` getting stuck mid-load on
large models when running close to the memory ceiling -- it must run
*during* loading, continuously, not once beforehand.

**Why it is not a mod in our architecture:** `/proc/sys/vm/drop_caches` is
not namespaced, so writing it inside a container acts on the host anyway. In
eugr's on-node design a container was the only execution surface available;
we have an off-node control plane that already runs commands against hosts
over SSH at deploy time (teardown, `nvidia-smi -lgc` clock lock, `mkdir -p`).
Wrapping this as a container mod would import their architectural constraint
into a system that doesn't have it.

**Why it is also not a `tuning:` knob:** unlike `gpu_clock_lock`, this isn't
a one-shot setting applied before launch. It's a daemon with a lifecycle --
start at deploy, stop at teardown -- which is real behaviour to build, not a
value to centralise.

**What's missing today:** nothing addresses FS cache pressure during model
load. Whether we actually need it is unconfirmed: it was introduced for
Qwen3.5-397B on eugr's hardware, and we have no recorded instance of a
`fastsafetensors` load stall on our own cluster. This entry exists so the
mechanism is understood and findable if that symptom ever appears, not
because it's a known gap.

**Shape of a real fix, if it's ever needed:**
- Start a host-side loop over SSH as part of the deploy sequence, gated by a
  per-recipe or per-deploy opt-in rather than always-on (dropping caches
  every 60s unconditionally has its own cost on a shared host).
- Tie teardown to it explicitly. A PIDFILE-per-deploy under the same
  `~/.cache/ray-logs/<deploy_run_id>` convention would make cleanup fit the
  existing teardown phases rather than adding a new tracking mechanism.
- Note the interaction with the teardown entries above: an orphaned
  `drop_caches` loop surviving teardown would be exactly the kind of stray
  host process the `ps aux` safety-net step was originally aimed at, and one
  of the few that step could actually see (it runs on the bare host, not in
  a container).

**Depends on:** nothing. Deliberately deferred -- do not build speculatively
before a real load stall is observed on our hardware.

---

### Multi-engine support: SGLang, llama.cpp

**New, 2026-09-03.** The deploy path assumes vLLM is the only possible
engine -- not as a documented constraint, as an absence. There is no
`engine:` field on the recipe schema at all.

**Context: real cases this session, not hypothetical.** Two separate
models hit walls that community reports show other engines sidestep
entirely. Nemotron 3 Nano 30B-A3B-FP8: vLLM and SGLang both failed on GB10
(SGLang's `:spark` image predates the model's config format; FP8
quantization kernels don't support compute capability 12.1 at all), and
the working path in that forum thread was llama.cpp. MiniMax M2.7-NVFP4:
this session's own attempt died on a corrupted upstream checkpoint (see
`TOMBSTONES.md`, this session), unrelated to engine choice, but the same
search pass surfaced that saricles' checkpoint family documents SGLang as
a first-class serving path alongside vLLM -- i.e. this is routine
enough in the community that recipes routinely ship both.

**What's actually coupled to vLLM, confirmed by reading the code, not
assumed:**
- `_execute_deployment_impl()` in `dgx-orchestrator.py` hardcodes the
  container entrypoint to `python3 -m vllm.entrypoints.openai.api_server`
  with vLLM's exact flag names (`--gpu-memory-utilization`,
  `--max-model-len`). No branch point exists for a different launcher.
- `common/phase_extract.py` is built entirely on vLLM's specific log
  vocabulary (`[weight_utils.py:540]`, `[default_loader.py:...]`,
  `[core.py:121] Initializing a V1 LLM engine`, the self-reported
  `torch.compile took Ns` line). None of it matches SGLang's or
  llama.cpp's own log formats. A non-vLLM deploy today would not fail --
  it would silently lose all phase/compile/download telemetry, the same
  failure shape `UnrecognizedLogShape` already exists to catch for
  genuinely garbled vLLM logs, but nothing currently raises it for "this
  is a different engine's log entirely."

**What already works and needs no change:** `image:` already accepts
arbitrary overrides (confirmed -- this session's recipes used
`eugr/spark-vllm-b12x:latest`; SGLang images like `lmsysorg/sglang:latest`
are a different family but the field doesn't care). The
recipe-catalog/config-hash/ledger machinery and the SSH/docker-run/host
management plumbing are engine-agnostic in practice, just never exercised
against anything but vLLM.

**llama.cpp is a materially different problem from SGLang, not just
another flag set.** SGLang is, like vLLM, an OpenAI-API-compatible server
that loads safetensors from an HF snapshot -- swapping it in is an
entrypoint/flag-translation problem. llama.cpp serves GGUF. `hf_path`
today assumes vLLM's snapshot-download-and-load-safetensors flow; a
GGUF-based deploy needs either a different artifact-resolution path or an
HF repo that happens to host a GGUF alongside safetensors. Worth scoping
llama.cpp separately from the SGLang question rather than solving both in
one pass -- they don't share a fix shape.

**Shape of a real fix:**
- Add an `engine: vllm | sglang | llamacpp` field to the recipe schema
  (`common/recipes.py`), defaulting to `vllm` so nothing existing changes.
- Branch entrypoint/flag construction in `_execute_deployment_impl()` on
  it.
- Either build an SGLang-equivalent of `phase_extract.py` or explicitly
  accept no phase telemetry for non-vLLM deploys until one exists --
  silent data loss is worse than a documented gap.
- Scope GGUF artifact resolution for llama.cpp as its own piece of work,
  not bundled into the SGLang entrypoint change.

**Depends on:** nothing structurally. Not urgent on its own, but the
DSpark and Qwen3.8-Flash-Next backlog items (see
`BACKLOG-dspark-sm120-image.md` and this session's Qwen3.8-Flash-Next
investigation) are both cases where a non-vLLM engine might have avoided
the problem entirely rather than needing a patched vLLM fork -- worth
keeping in mind if either of those gets picked up again before this does,
since the two backlogs could inform each other's priority.

---

## Recipe catalog integrity

### Recipe-level guardrails against known-bad flag combinations

**Priority: bumped, 2026-08-28.** Directly requested — several recipes in
the current catalog can be selected and launched in the dashboard/CLI but
are known or suspected to fail, which wastes a real cold-start cycle
(sometimes 30+ minutes) finding that out. This entry (the flag-combination
linter) and the new "recipe status marker" entry below are the two
concrete pieces of that ask.

**Context:** two separate incidents on 2026-08-23 were both, at root, a
recipe carrying a `vllm_args` combination that was invalid and had never
been validated against a real deploy before being committed:

1. `--kv-cache-dtype nvfp4_ds_mla` on an MLA-architecture model (DeepSeek
   V4) -- vLLM's own engine-config validation rejects any `nvfp4`-family
   KV cache dtype for MLA models outright. Already documented in
   `docs/TROUBLESHOOTING.md` #3 for the related trigger case.
2. `--quantization modelopt_fp4` combined with an explicit
   `--kv-cache-dtype`, which per that same troubleshooting entry triggers
   an internal container entrypoint hook that silently overrides the
   explicit KV cache dtype back to `nvfp4_ds_mla` -- recreating problem #1
   even when the recipe author correctly set `fp8` explicitly.
3. **Added 2026-09-03, updated same day once fixes landed.** A `2_node`
   topology whose `vllm_args` omits `--distributed-executor-backend ray`
   entirely, on a recipe relying on `default_image`
   (`nvcr.io/nvidia/vllm:26.07-py3`, confirmed not to ship the `ray`
   binary at all). `dgx-orchestrator.py`'s `use_ray` check requires the
   flag literally present; its absence silently routes the deploy onto
   the `--nnodes`/`--headless` multiproc path instead of failing closed,
   which on this cluster's default image hits the exact
   `collective_rpc should not be called on follower node` assertion
   `TOMBSTONES.md` #43 was written from. **`llama-3.3-70b`, `llama-4-fp4`,
   and `llama-4-fp8` all hit this, and all three are now confirmed fixed
   by a live deploy** (image switched to `eugr/spark-vllm-b12x:latest`,
   which does ship Ray, plus the flag added -- see `TOMBSTONES.md` #103).
   `qwen-3.6-27b-nvfp4` and `qwen-2.5-coder-32b` carry the identical fix
   in their recipe files but are **not yet individually confirmed by a
   live deploy** -- treat as drafted, not validated, until one actually
   runs.
4. **Added 2026-09-03.** Any MTP-family `speculative-config`
   (`qwen3_next_mtp`, and presumably any other MTP method) combined with
   `pp_size > 1`. Hard vLLM `NotImplementedError` at engine-config
   creation time -- the MTP draft-model class doesn't implement
   `SupportsPP`, independent of whether the target model does. Confirmed
   live on `qwen-3.6-27b-nvfp4::2_node`. Cheap to catch (fails before any
   compile/weight-load cost), but currently only discoverable by actually
   deploying and watching it crash. See `TOMBSTONES.md` #104 and
   `docs/TROUBLESHOOTING.md` #13.

All four are documented in `docs/TROUBLESHOOTING.md` now, but only as
after-the-fact incident write-ups -- nothing stops a fifth recipe from
reintroducing any of these patterns, and a recipe carrying one can sit in
git looking fine (valid YAML, passes schema validation) until the moment
someone actually deploys it.

**Shape of a real fix:**
- A lightweight recipe linter -- either folded into `load_recipes()`
  itself (warn at load time, don't hard-fail the whole catalog per the
  existing fail-closed behavior) or a standalone `tools/lint_recipes.py`
  run in CI -- checking `vllm_args` for a small, explicit list of known-bad
  flag combinations as they're discovered. Start with the four above;
  this list only grows by adding real incidents, not by trying to
  anticipate hypothetical ones.
- Cheap enough to start: a dict of `{flag_or_value: incompatible_with}`
  pairs and a regex/shlex-split check against `vllm_args` at recipe load
  time, surfaced the same way an unrecognized `recipe_version` already
  warns today (soft warning, not a hard failure, unless confidence is
  high).

**Depends on:** nothing. Small, and the four known cases above are already
fully specified.

---

### Per-recipe/topology validation status marker

**New, 2026-08-28.** A narrower linter only catches flag combinations
someone already knows are bad. It doesn't help with the broader,
currently-felt problem: a recipe can be schema-valid, carry no known-bad
flag pattern, and *still* fail on deploy simply because that specific
model/topology combination has never actually been exercised end-to-end
-- exactly the distinction `docs/TROUBLESHOOTING.md`'s
Validated/Known-bad/Unconfirmed framework already draws for individual
`vllm_args`, but nothing currently draws at the level of "should a person
even try deploying this recipe right now."

**Context:** the catalog is a living, growing set (see `README.md`'s
Model Catalog section) where new variants get added as needed. Nothing
distinguishes, in the dashboard dropdown or `dgx-config status`, a recipe
that's been deployed and confirmed serving traffic from one that's a
work-in-progress smoke test (e.g.
`deepseek-v4-flash-0731-dspark-sm120.yaml`, deliberately built small for a
fast yes/no and explicitly not expected to be production-ready) or one
that's simply never been tried. All three currently look identical in the
UI: selectable, same as any other model.

**Shape of a real fix:**
- Add an optional `status:` field to the recipe schema (`common/recipes.py`),
  e.g. `validated` / `unconfirmed` / `known-bad`, defaulting to
  `unconfirmed` when absent so existing recipes don't need an immediate
  mass edit.
- `build_catalog_response()` already has `PENDING_LAUNCH_STATE`'s "last
  launched successfully" tracking (landed 2026-08-24) keyed by
  `config_hash` -- a recipe/topology combination that has a recorded
  successful launch could auto-promote from `unconfirmed` to `validated`
  without a human touching the YAML at all, keeping the marker honest and
  low-maintenance rather than another thing to remember to update by hand.
- Surface the status as a visible badge/color next to each model in the
  dashboard dropdown and in `dgx-config status`'s catalog listing --
  something a person glances at before clicking Deploy, not something
  they have to already know to go check `docs/TROUBLESHOOTING.md` for.
- Does not replace the flag-combination linter above -- that catches a
  known-bad pattern before it's ever deployed once; this tracks whether a
  given recipe has actually been proven to work at all. Both are worth
  having.

**Depends on:** nothing structurally. Requires an actual pass over the
current `recipes/local/*.yaml` and `recipes/eugr/*.yaml` files to assign
initial status values -- not done as part of this roadmap entry, needs the
real files.

---

### Recipes carry `VLLM_*` env vars this build silently ignores

**New, 2026-08-30.** vLLM warns at boot about any `VLLM_`-prefixed
environment variable it doesn't recognise
(`WARNING [envs.py:2477] Unknown vLLM environment variable detected: X`).
Nothing surfaces those warnings, so cargo-culted variables accumulate in
recipes indefinitely, and every one of them feeds `compute_config_hash()` --
meaning inert config participates in the "has this exact configuration
launched successfully" join as though it mattered.

**Confirmed instances, 2026-08-29/30, on
`v0.1.dev20003+gad848fc41.d20260815`:**

| Variable | Source | Actionable? |
|---|---|---|
| `VLLM_CPU_OMP_THREADS` | recipe `env_vars` | yes -- removed from `gemma-4-31b.yaml` |
| `VLLM_ENGINE_INITIALIZATION_TIMEOUT` | recipe `env_vars` | yes -- removed |
| `VLLM_RPC_TIMEOUT` | recipe `env_vars` | yes -- removed |
| `VLLM_BASE_DIR=/workspace/vllm` | **image `ENV`** (eugr's Dockerfile) | **no** -- see below |

**Two more confirmed 2026-09-02/03, same build, from this session's
`minimax-m2_7-nvfp4-gb10.yaml` (which carried them over directly from the
checkpoint author's own documented deploy command without re-verifying
against this build):**

| Variable | Source | Actionable? |
|---|---|---|
| `VLLM_NVFP4_GEMM_BACKEND` | recipe `env_vars` | yes -- this build auto-selects (`Using 'FLASHINFER_CUTLASS' NvFp4 MoE backend`); the var never influenced that choice |
| `VLLM_USE_FLASHINFER_MOE_FP4` | recipe `env_vars` | yes -- same, no observed effect on backend selection |

`VLLM_CPU_OMP_THREADS` and `VLLM_BASE_DIR` recurred on the same run and on
`nemotron-3-nano-30b-a3b-nvfp4.yaml`'s run the same session -- consistent
with the existing table, not new information, but confirms the pattern
holds across a third and fourth recipe rather than being specific to
gemma-4-31b. **Community-sourced recipes are a real, recurring source of
these** -- both new instances above were copied from a working third-party
deploy command rather than authored fresh, and the community command
predates or targets a different vLLM build than what's actually running
here.

**The two categories need different handling, and conflating them makes the
check useless.** `VLLM_BASE_DIR` is baked into `eugr/spark-vllm-b12x` as an
image-level `ENV` (confirmed via `docker inspect .Config.Env`). It is
present in every container from that base and in every layer baked on top of
it, and no recipe edit can remove it. A checker that flags it will fire on
every eugr-based recipe forever; a warning that always fires gets ignored,
which then hides the ones that matter. **The check must inspect recipe
`env_vars` only**, and `VLLM_BASE_DIR` should be documented as
expected-and-not-actionable rather than suppressed silently.

**Shape of a real fix, cheapest option first:**

- **Parse the boot log, don't maintain an allow-list.** vLLM already emits
  the warning, for the build actually running, with no maintenance burden.
  A static list of known-good `VLLM_*` names has to be re-verified on every
  image bump and will still miss variables neither we nor the list author
  knew about. Scraping
  `Unknown vLLM environment variable detected: (\S+)` out of container logs
  post-deploy catches everything and stays correct by construction.
- Cross-reference each hit against the recipe's declared `env_vars`. A hit
  present there is actionable; a hit absent from it is image-inherited --
  report it differently or not at all.
- Fold into the per-recipe validation status marker work rather than
  building a separate reporting path: "launched successfully" and "launched
  without ignored config" are the same kind of signal about the same
  `config_hash`.

**A caution on removals.** Every `env_vars` change alters
`compute_config_hash()` and therefore resets the launch-success history for
that recipe/topology. That's correct behaviour -- the launched configuration
genuinely changed -- but it means a bulk strip across the catalog would
invalidate the entire validation history at once. Strip opportunistically
when a recipe is being re-validated anyway, not as a sweep.

**Related, unresolved:** `VLLM_USE_V1=0` is *not* in the unknown-variable
list -- vLLM recognises it -- but on this build it appears to have no
effect: the engine logged `Initializing a V1 LLM engine` with the variable
set, on both the 2-node Gemma deploy (2026-08-29) and subsequent runs. This
bears on `TROUBLESHOOTING.md` Incident #1, which mandates it for all 2-node
Ray cross-host topologies. Either the rule is stale for this build (no V0
path left to fall back to) or it still matters for topologies not retested
since. One data point is not enough to rewrite an incident rule that was
written from a real failure -- flagged here so it is neither trusted blindly
nor deleted prematurely. Resolving it wants a deliberate A/B on a 2-node
deploy, not an inference from logs.

**Depends on:** nothing. Naturally sequenced after the per-recipe status
marker work.

---

### `compute_config_hash()` hashes `vllm_args` as a raw string, not parsed

**Context, 2026-08-29.** `common/recipes.py`'s `compute_config_hash()`
deliberately hashes `vllm_args` as-is rather than `shlex`-splitting it --
its own docstring calls this out explicitly: *"simpler, and fine unless
flags get reordered without changing them in practice."* That caveat is
the bug: two functionally identical `vllm_args` strings that differ only in
flag order, or in incidental whitespace from a YAML folded-scalar (`>-`)
reflow, hash differently and spuriously reset a recipe's "validated"/launch-
history status even though nothing that reaches `docker run` actually
changed.

**Why this matters more once the per-recipe status marker (above) lands:**
right now a stale hash only means a slightly-too-conservative "last
launched successfully" display. Once recipes carry a `validated` /
`unconfirmed` / `known-bad` badge that auto-promotes off this same hash,
an incidental reflow -- someone re-wrapping a long `vllm_args` line, or a
future recipe-editing tool that reorders flags for readability -- would
silently demote a known-good recipe back to looking unvalidated in the
dashboard, for a change that altered nothing about actual behavior. A
false "this hasn't been tested" is a worse failure mode than a false
"this has" would be, since it either causes needless re-validation or --
worse -- trains people to stop trusting the badge at all.

**Shape of a real fix:**
- `shlex.split(topo.vllm_args)`, then sort the resulting token list (or
  parse into flag/value pairs and sort by flag name) before hashing, so
  flag order stops mattering the same way `env_vars` already gets sorted
  before hashing.
- Watch the interaction with `docs/TROUBLESHOOTING.md` Incident #4 (YAML
  folded-scalar comment pollution / `shlex.split()` choking on stray `#`)
  while touching this -- `compute_config_hash()` and the actual launch
  path's own arg-splitting should probably share one parsing helper rather
  than two independent implementations of "parse `vllm_args`" that could
  drift out of sync with each other.
- Decide whether to also normalize `--speculative-config`'s embedded JSON
  (currently just a substring of the larger `vllm_args` string) --
  key-order-insensitive JSON comparison would close the same class of gap
  one level deeper, though it's a smaller/rarer case than top-level flag
  reordering.

**Depends on:** nothing structurally. Worth landing before or alongside the
per-recipe status marker entry above, since that's the feature whose
correctness actually depends on this hash being order-insensitive.

---

### Near-duplicate catalog key detection

**Context:** `deepseek-v4-flash-nvfp4.yaml` and
`deepseek-v4-flash-0731-nvfp4.yaml` coexisted with catalog keys one
keystroke apart, serving genuinely different models. A prior session
silently repointed the former's `hf_path` to the *same* model as the
latter while also introducing the invalid kv-cache-dtype combination above
-- with no filename change to signal any of it happened. The wrong key got
deployed by simple typo-adjacent selection, not a deliberate choice.
(Resolution used at the time: the older recipe was deleted outright rather
than repaired, since it wasn't in active use -- see `docs/TOMBSTONES.md`
#57. That closed this specific instance but not the underlying class of
risk.)

**What's missing today:** `load_recipes()` already raises on an exact
stem collision between `local/` and `eugr/` directories, but nothing
flags two *different*, both-valid catalog keys that are suspiciously
similar -- by edit distance, by shared `hf_path`, or both.

**Shape of a real fix:**
- At `load_recipes()` time, after loading the full set: flag any pair of
  keys within a small edit-distance threshold of each other, or any pair
  that share an identical `hf_path`, as a soft warning (printed at load,
  maybe also surfaced in `dgx-config status` or the dashboard) -- not a
  hard failure, since legitimate near-duplicates (a `-nvfp4` suffix
  variant of the same base name) are common and expected.
- The `hf_path` collision check is the more actionable one: two catalog
  keys serving the identical HF model is either intentional (fine, but
  worth knowing) or exactly this incident (a stale entry silently
  repointed to overlap with a newer one).
- `find_cached_models()` (4.8.4) already computes a live
  `hf_path -> catalog_key` map for cache attribution purposes -- the same
  data structure this check would need, just applied at recipe-load time
  instead of cache-inspection time. Worth building this as a shared helper
  both call, rather than two separate implementations of the same lookup.

**Depends on:** nothing. Could land alongside the flag-combination linter
above, since both hook into the same `load_recipes()` pass.

---

### `benchmark_ledger.csv` key can silently mismatch the recipe actually benchmarked

**Context, 2026-08-29.** `benchmark.py --model-key` exists specifically so
the ledger's ability to join back to the catalog (`enrich_catalog()`'s
`historical_tps` lookup) doesn't depend on the served model ID matching the
catalog key -- per the script's own docstring, those are different strings
by default. During the DSpark validation run, the benchmark logged under
key `deepseek-v4-flash-0731-1M` despite validating a completely different
recipe (`deepseek-v4-flash-0731-dspark`) -- either `--model-key` wasn't
passed, or was passed with a stale value left over from a previous run.

**What's missing today:** nothing catches this at benchmark time. A wrong
or missing `--model-key` silently produces a ledger row that looks
legitimate but attributes historical throughput data to the wrong recipe --
which then quietly corrupts anything downstream that trusts
`historical_tps` for that catalog key (the per-recipe status-marker idea
above, dashboard display, capacity planning) without any error surfacing
anywhere.

**Shape of a real fix:**
- Have `execute_standalone_benchmark()`/`_run_benchmark_worker()` (the
  orchestrator's own caller of `benchmark.py`, per its comment about owning
  `benchmark_results.txt`) always pass `--model-key` explicitly, derived
  from whatever recipe/config is actually being benchmarked, rather than
  relying on it being supplied correctly by whoever invokes the script
  directly.
- Consider a sanity check at ledger-write time: if `--model-key` isn't
  provided and falls back to `model_id.split("/")[-1]`, and that fallback
  key doesn't match anything in the currently-loaded catalog, warn loudly
  rather than writing a silently-orphaned row.
- Worth an audit of existing `benchmark_ledger.csv` rows for other
  mismatches now that one's confirmed, since this could have been
  happening quietly before it was noticed.

**Depends on:** nothing structurally. Small, self-contained fix in the
benchmark invocation path.

---

### `tp_size: 2` vs `pp_size: 2` never actually A/B'd on this cluster -- community favors TP unanimously, our only working PP data point is one model

**New, 2026-09-03.** `qwen-3.6-27b-nvfp4.yaml` and `qwen-2.5-coder-32b.yaml`
both carry `pp_size: 2` for their `2_node` topology. That choice has zero
external precedent either way for these two models specifically, and it
runs against a strong, consistent community pattern: every GB10 2-node
vLLM deployment found in a broad search this session -- the `veloGB10`
Qwen3.6-27B card ("two-node TP=2 serving... proven to work" — different
engine, but consistent with the pattern), `bjk110/spark_vllm_docker`,
the DSpark concurrency patch, `mark-ramsey-ri/trt-dgx-spark` -- defaults
to `tensor_parallel_size`, not pipeline. Nobody documents PP=2 as an
option at all for GB10 2-node vLLM. The one thing pulling the other way
is in-house: `llama-4-fp8`'s confirmed-`ready` deploy this session is
real proof PP=2 + explicit Ray + `eugr/spark-vllm-b12x` works on this
stack -- but it's one model, not these two, and nobody has ever put PP
and TP head-to-head on the same model here.

**Real tool gap found while scoping this -- worse than first thought, now
fixed.** `tests/ab_test.py`'s override mechanism cannot express this
comparison via `--{side}-*` overrides on top of a catalog base --
`resolve_variant()` forces any override onto a 1-node ad-hoc path. The
originally-suggested workaround (a plain two-sided passthrough with no
override flags at all, shown below in the version of this entry written
2026-09-03 earlier the same day) turned out to be wrong too: with no
`--{side}-nodes` flag, `catalog_nodes` silently defaults to `1`, so a
"plain passthrough" command *silently deploys and compares the 1-node
topology on both sides* -- for `qwen-3.6-27b-nvfp4` and
`qwen-2.5-coder-32b`, whose `1_node` blocks are TP=1/PP=1 either way,
that's comparing two byte-identical configs and would have reported "no
difference" for a completely uninteresting reason, without erroring.
Adding `--{side}-nodes 2` to fix *that* then hit a second, separate bug:
passing `--{side}-nodes` at all (regardless of value) counted as an
override in its own right, which disqualified the passthrough branch and
hit the ad-hoc branch's own `nodes > 1` rejection -- i.e. explicitly
asking for the 2-node topology was structurally impossible too, for any
recipe, under any flag combination. Root-caused and fixed same session --
see `TOMBSTONES.md` #105. The corrected, actually-working command:

    docker exec dgx-orchestrator-api python3 tests/ab_test.py \
        --variant-a qwen-2_5-coder-32b \
        --variant-b qwen-2_5-coder-32b-tp \
        --a-nodes 2 --b-nodes 2 \
        --prompts all \
        --repeats 3

Note the model switched from `qwen-3.6-27b-nvfp4` to `qwen-2.5-coder-32b`
-- see below.

**`qwen-3.6-27b-nvfp4` pair is not a valid TP-vs-PP comparison anymore.**
Both files carry MTP (`qwen3_next_mtp`) speculative decoding, and MTP is
hard-incompatible with `pp_size > 1` -- confirmed live, `NotImplementedError:
Pipeline parallelism is not supported for this model.` The PP side cannot
boot at all; TP "winning" would be by forfeit, not measurement. See
`TOMBSTONES.md` #104, `docs/TROUBLESHOOTING.md` #13, and the new
guardrails case #4 above. **The TP side itself is now fully confirmed,
not just manually load-tested** -- container log review (2026-09-03)
shows MTP genuinely engaging on both TP ranks (`Detected MTP model.
Sharing target model embedding weights with the draft model`, both
`Worker_TP0`/`Worker_TP1`), full engine init, and a real `GET /health`
`200 OK` at `03:40:58`. Not a benchmark result -- no throughput number
yet -- but a real, working, healthy deploy. **`qwen-2.5-coder-32b` is the
live pair now** -- no speculative-config on either side, clean of this
problem. Both its
files also needed an unrelated fix this session: `max_model_len: 262144`
exceeded the model's real 32768 context (YaRN would allow 131072, not
262144, and wasn't configured either way) -- both files corrected to
`32768`. Both sides have confirmed successful manual dashboard loads as
of 2026-09-03; the actual `ab_test.py` A/B run itself has still not
completed successfully as of this writing -- blocked in sequence by the
catalog-key naming mismatch (filenames use `qwen-2_5-coder-32b` with
underscores, not `qwen-2.5-coder-32b` with a dot), then the two tool
bugs above, then a third, unrelated tool bug: `ab_test.py`'s pre-pull
only ever refreshed the head's image cache, letting `spark-3` silently
drift to a stale build under the same `:latest` tag -- 6/6 deploy
attempts crashed identically on a Ray head/worker version mismatch as a
result. Root-caused and fixed same session, see `TOMBSTONES.md` #106 --
the actual A/B run has not been retried since that fix landed.

**Update, later same session: `qwen-3.5-122b.yaml`'s `pp_size: 2` + MTP
combo confirmed dead**, same failure as `qwen-3.6-27b-nvfp4`
(`TOMBSTONES.md` #104) -- not independently reproduced on this specific
model, but the mechanism (MTP draft model class, `SupportsPP`) is
model-agnostic, so treated as confirmed rather than re-tested for its
own sake. **`qwen-3.5-122b-tp` (n=3) now confirmed live with real
throughput, not just a health check** -- `benchmark.py` 3-pass, warm avg
**40.9 tok/s decode** (38.4 cold, TTFT 49.25s cold / ~0.19s warm -- the
large cold TTFT is a one-time first-request tax, consistent with the JIT
-compile-not-covered-by-warmup pattern already documented elsewhere in
this repo for other first-ever deploys, not a per-request cost). Logged
to `benchmark_ledger.csv` under key `qwen-3.5-122b-tp`. **Retirement
blocker cleared** -- `qwen-3.5-122b.yaml` (the dead PP file) can now be
safely removed; the replacement is confirmed working, not just assumed.
`qwen-3.5-122b-mtp2.yaml`/`-mtp4.yaml` (the token-depth sweep siblings)
rebuilt as `tp_size: 2` to match -- both were originally built as
`pp_size: 2` before the MTP+PP incompatibility was known, which would
have made the depth sweep meaningless even if it somehow booted (mixing
topology and depth as confounded variables). All three
(`mtp2`/`tp`[=n3]/`mtp4`) are topology-consistent now, and with `tp`
confirmed, the sweep in `pairs.txt` is unblocked -- `mtp2`/`mtp4`
themselves are still individually untested. See `TOMBSTONES.md` #107.

**Shape of a real fix/follow-up:**
- Get one clean completed `ab_test.py` run on the `qwen-2.5-coder-32b`
  pair using the corrected command above -- this hasn't happened yet as
  of this writing, despite the tooling now being fixed.
- Confirm/refute the `qwen-3.5-122b` PP+MTP crash before scheduling its
  overnight sweep -- likely dead on arrival per the guardrails case
  above, in which case that recipe's TP-vs-PP comparison is moot and only
  the MTP token-depth sweep (`-mtp2`/base/`-mtp4`, all TP or all 1-node)
  remains meaningful.
- If TP wins or ties on `qwen-2.5-coder-32b`, consider converting other
  PP=2 recipes to match -- but only after a real measurement, not on
  community precedent alone, since `llama-4-fp8` is direct proof PP can
  be the right call on this exact image/stack for at least one model.
- Separately, still worth deciding whether a `--{side}-tp-size`/
  `--{side}-pp-size` override pair on top of an existing recipe base is
  worth adding generally -- would make this kind of comparison a one-off
  command instead of requiring a permanent second catalog entry every
  time. Not scoped here, just noted as the thing that would make this
  cheaper going forward.

**Depends on:** nothing structurally. `ab_test.py`'s node-selection bug
(`TOMBSTONES.md` #105) is fixed; the actual live A/B run is the remaining
open step.

---

## Runtime robustness

### Retain the full vLLM container log per deploy

**New, 2026-08-30.** When a deployment is torn down, `docker rm` deletes the
container's log with it. The only record of what vLLM actually said --
resolved backends, quantization path, ignored env vars, the traceback of a
crash -- is gone. `TOMBSTONES.md` #7 solved exactly this for Ray crash logs
by bind-mounting the session dir to a persistent host path; nothing does the
equivalent for the vLLM container's own stdout.

This session alone, that log answered: which attention backend actually
resolved (`TRITON_ATTN`, 3x across cluster), whether `--quantization fp8`
took, that `VLLM_USE_V1=0` was being ignored, that `VLLM_BASE_DIR` is
image-inherited, and that the M0 probe image booted the NGC entrypoint
correctly. All of it would have been unrecoverable an hour later.

#### The flush-cadence question is mostly a non-question

**Docker already persists container stdout to disk continuously** via the
`json-file` logging driver
(`/var/lib/docker/containers/<id>/<id>-json.log`), on the Spark host, with
no involvement from us. There is nothing to flush during a run and no
buffering to worry about. Our containers are launched with `docker run -d`
and **not** `--rm`, so the log also survives the container exiting -- a
crashed engine's output stays readable until something removes the
container.

So the design is not a polling loop. It is:

1. **Capture at teardown, before `docker rm`.** This is the one moment the
   data is actually at risk. Copy (or `docker logs >`) into the existing
   per-deploy directory, then proceed with removal.
2. **A low-frequency incremental safety net**, because relying solely on
   teardown is fragile in *this* codebase specifically: teardown reporting
   success regardless of per-host failure is a documented bug class here
   (see the residual-gaps section), and a host reboot, a manual `docker rm`,
   or a `docker system prune` all bypass our teardown path entirely. Append
   `docker logs --since <last_capture>` on a slow cadence -- 60s is ample,
   and it must be incremental, not a full re-read, or a long deploy re-ships
   the whole log every minute.

**Do not stream `docker logs -f` from the daemon.** A long-lived follow
process per container is another thing to leak, and orphaned child processes
are already a known class of bug in this codebase (see the teardown entries).
The incremental `--since` poll is bounded, stateless, and reaps itself.

#### Storage and retention

- Reuse `~/.cache/ray-logs/<deploy_run_id>/<host>/` -- it already exists per
  deploy, is already bind-mounted persistently, and is already covered by
  `dgx-config prune-ray-logs`.
- **Needs its own retention knob**, not `crash_log_retention_days: 7`. Ray
  crash dumps are tiny; a full vLLM log is not. Add
  `vllm_log_retention_hours` to `cluster_config.yaml`'s `tuning:` block,
  default 24.
- **Keep whole logs initially. Do not truncate yet.** A head/tail policy
  (e.g. first 500 + last 500 lines) is the likely end state and would cover
  both "what config did it launch with" and "what was it saying when it
  died" -- but picking those numbers before profiling real sizes is a guess.
  Worse, the failure modes this is most valuable for are the *slow* ones
  (the Ray memory-monitor OOM kill, the multi-hour session freeze), where a
  head/tail policy would discard precisely the hours in which the
  degradation is visible. Measure first: capture whole logs across a
  DeepSeek 512K deploy and a short Gemma one, compare sizes, then decide.

#### Check before building

Whether the Docker daemon on the Sparks has `log-opts max-size`/`max-file`
configured in `/etc/docker/daemon.json`. If so, the json-file log is already
being rotated and older lines may be gone before we ever capture them --
which would change this from a retention problem into a logging-driver
configuration problem. Cheap to check, and it invalidates the "Docker
already persists everything" premise above if true.

#### Related, and better once this exists

The unrecognised-`VLLM_*`-env-var check described above becomes trivial with
retained logs: grep a file keyed to a `deploy_run_id` instead of racing a
live stream. Same for a dashboard warning/error surface -- but note that is
a genuinely different feature (live, about *during*) from this one
(post-hoc, about *after*), and this one is the prerequisite: a live scraper
with no persistence still loses everything at teardown.

**Depends on:** nothing. Small, and the teardown-side capture is the
majority of the value on its own.

---

### `/api/status` reports `orchestrator_version` but nothing about `common/`

**New, 2026-08-29.** The deploy-confirmation habit (check the dashboard
version badge or `orchestrator_version` in `/api/status` before trusting a
fix is live) covers `dgx-orchestrator.py` and nothing else. Every module
under `common/` -- `ssh.py`, `config.py`, `recipes.py`, `constants.py` --
can be updated on disk with no observable signal that the *running* daemon
is still executing the previous version.

**Context: this cost a real debugging round tonight.** A fix to
`common/ssh.py`'s `get_hf_token()` was deployed and verified present on disk
(`docker exec dgx-orchestrator-api grep ... common/ssh.py` matched), yet
deploys continued to launch containers with no `HF_TOKEN` set. Cause:
`docker-compose.yml` bind-mounts `.:/app`, so the file on disk updated
immediately, but Python does not reload an already-imported module. The
long-lived daemon kept executing the pre-fix code until
`docker compose restart orchestrator-api`. `orchestrator_version` was
correct and unchanged throughout, because `dgx-orchestrator.py` itself never
changed -- the one signal we have was structurally incapable of catching
this.

Note the interaction that makes this worse than it sounds: the bind mount
means there is no deploy step that would naturally restart the process. For
`dgx-orchestrator.py` a stale daemon is usually noticed because the version
badge doesn't move. For `common/`, nothing moves at all.

**What's missing today:** no visibility into which version of the `common/`
package the running process actually has loaded.

**Shape of a real fix:**
- Add a `modules` block to `/api/status` reporting, per `common/*.py`, a
  short content hash or mtime read from `module.__file__` at import time
  (i.e. what the running process loaded, NOT a fresh read of the file --
  the whole point is to detect divergence between the two).
- Optionally compare that against a live re-read of the same files on each
  status call and surface a `stale_modules: [...]` list when they differ.
  That is the signal that would have short-circuited tonight's confusion
  immediately. Watch the cost: `/api/status` is polled continuously, so a
  per-call re-read of every `common/` file is real I/O on a hot path --
  cache it against a directory mtime fingerprint the way `load_recipes()`
  already does, rather than stat-ing on every poll.
- Surface it next to the existing version badge in the dashboard, so the
  established habit ("check the badge after deploying") extends to the whole
  package without anyone needing to learn a second ritual.
- Consider whether the deploy procedure should simply always restart the
  API container, making staleness impossible rather than merely visible.
  Cheaper than the above and strictly more reliable, at the cost of a few
  seconds of API downtime per deploy -- worth weighing, since a signal
  nobody reads is worth less than a class of bug that cannot occur.

**Depends on:** nothing. Small, self-contained. Related to the
`get_hf_token()` silent-failure fixes recorded in `TOMBSTONES.md` #82 --
same incident, but this is the part that isn't a code bug in
`get_hf_token()` itself and so wasn't fixed there.

---

### Engine health monitoring: container RUNNING doesn't mean the engine is alive

**Context:** in a 2-node Ray deploy, the container's PID 1 is
`ray start --block`, not the vLLM engine. The engine itself runs as a
separate, detached `docker exec -d` process launched after Ray registers
its workers -- structurally decoupled from the container's own process
tree. Docker correctly reports the container `RUNNING` for as long as Ray
is alive, independent of whether the actual engine process crashed
seconds after starting.

**What 4.8.4 does:** `_detect_crash_signature()` catches this by scanning
container logs for an unhandled Python traceback OR an argparse-style CLI
usage error, and short-circuits to a `CRASHED` status before the
progress-keyword scanner gets a chance to misfire on words inside the
error text itself (two real incidents: a crash message containing the
literal phrase "kv cache," and a malformed `--speculative-config` that
exits via `parser.error()` with no traceback at all). This is a real fix
for both failure classes observed, but it's a log-scraping workaround, not
a structural one.

**What's still missing:** nothing directly checks whether the engine
process is actually alive. A crash that produces neither a traceback nor
an argparse error line (a segfault, an OOM-killed process, a hang with no
output at all) would not be caught by `_detect_crash_signature()` and
would fall through to the same misreporting risk this entry exists to
close.

**Shape of a real fix:**
- For the Ray-exec launch path specifically: track the PID (or a
  recognizable process signature) of the `docker exec -d`'d engine process
  at launch time, and have status checks verify it's still present via
  `docker exec <container> ps aux` (or `/proc` inspection) rather than
  relying on container-level state or log content at all.
- Treat "container RUNNING, engine process absent, health check never
  passed" as a distinct, unambiguous CRASHED state -- independent of
  whatever the logs do or don't contain.
- Keep `_detect_crash_signature()` as a fast-path/first-line check (it's
  cheap, one `docker logs` call) even after a process-liveness check
  lands, since it can report the *reason* for the crash where a bare
  liveness check can't.

**Depends on:** nothing structurally. Natural next step after the log-scan
fix, once there's appetite to touch the Ray-exec launch path.

---

### Cache integrity retrospection

**Context:** `cache-inventory` (4.8.4) and `prune-cache` (4.8.4) both
already walk every JIT cache entry directory on every host -- that's the
natural place to add integrity checks, since the traversal cost is already
paid.

**What's missing today:** neither command has any concept of "this entry
looks incomplete/corrupt," only age and size. The `cache-inventory` run on
2026-08-23 surfaced one concrete artifact worth generalizing from: a
`tilelang` entry literally named `tmp` with an implausible ~56-year age
(epoch-adjacent timestamp), consistent with a partial-extraction directory
from a process that died before its rename-into-place step.

**Shape of a real fix:**
- Heuristic pass in the inventory walk: flag entries whose name looks like
  a working/temp artifact (`tmp`, trailing partial-write suffixes the
  specific JIT libraries use -- needs checking their actual conventions,
  not guessed), or whose internal file set looks incomplete relative to
  what a healthy entry of that type normally contains (e.g. metadata json
  present without a paired `.so`/`.cubin`, if that pairing convention holds
  -- needs verifying against actual Triton/TileLang/DeepGEMM cache layouts,
  we don't currently have documented ground truth on this).
- Correlate against teardown history: if a `--log-teardowns` or similar
  timestamp record existed, flag any cache entry whose mtime falls within
  a narrow window of a past hard-kill teardown as "possibly interrupted,"
  independent of the structural heuristic above.
- Surface flagged entries in `cache-inventory` output as a distinct
  category (not folded into the LRU list), and let `prune-cache` optionally
  target flagged entries specifically regardless of the free-space floor --
  a suspected-corrupt entry is worth clearing even when disk space isn't
  tight, which is a different trigger than the LRU eviction path.
- This needs real ground-truth on Triton/TileLang/DeepGEMM's actual cache
  directory contracts before the heuristic can be trusted -- worth reading
  their source (or the `eugr/spark-vllm-b12x` image's bundled versions of
  them) rather than guessing the file-pairing convention.

**Depends on:** nothing structurally -- could land independently of the
teardown work above. Worth doing after a few more `cache-inventory` runs
across normal operation, so the heuristic is tuned against what a *healthy*
cache actually looks like, not just the one incident.

---

## Residual gaps from the 4.8.4 teardown work

### Teardown robustness: close the orphaned-compile-child gap

**Status:** partially addressed in 4.8.4 (`TEARDOWN_GRACE_SEC`, graceful
`docker stop` before `rm -f`, `--init` on both docker run paths). This
entry is what's left.

**Context:** the corruption incident on 2026-08-23 was most likely caused
by teardown's old unconditional `kill -9` + `docker rm -f` sequence hitting
a container mid-JIT-compile. Triton/TileLang/DeepGEMM shell out to
`nvcc`/`ptxas`/`cicc` as child subprocesses that write cache artifacts
non-atomically; SIGKILL on the parent doesn't propagate to those children,
so a hard-kill mid-compile can leave a half-written artifact at the path
the loader treats as a cache hit on the next load -- silent, one-time,
unpredictable recompiles with no error surfaced anywhere.

**What 4.8.4 does NOT fix:**

1. **The grace period has a ceiling.** 20s covers most shutdown paths but
   a compile still running past the window still gets hard-killed with the
   same risk as before. There's no way to know from teardown's side
   whether a compile is in flight, only that model_status *was*
   `NOT READY - COMPILING KERNELS` moments before teardown was called
   (visible in `get_cluster_status()`, not currently checked by
   `_execute_teardown_impl`).
2. **The host-level cleanup regex still won't match compiler children.**
   `ps aux | grep -E 'vllm|ray'` doesn't match a bare `ptxas`/`nvcc`/`cicc`
   process by name, so even the graceful SIGTERM path in 4.8.4 doesn't
   reach them directly -- they only get cleaned up if `--init`'s reaping
   and/or the container's own death takes them down as children.
3. **No teardown-time awareness of compile-in-progress.** Ideally,
   `execute_teardown()` would check `model_status` before acting and either
   warn the caller ("teardown requested mid-compile, proceeding after Ns
   grace") or extend the grace period specifically for that case, rather
   than using the same fixed 20s regardless of what's happening inside the
   container.

**Shape of a real fix:**
- Have teardown consult `get_cluster_status()` (or a cheaper direct check)
  before killing anything, and log/return a flag when it's interrupting an
  in-progress compile rather than a ready/idle container.
- Consider cgroup-based process-group kill instead of name-pattern
  matching, so compiler children are reachable regardless of process name.
- Decide whether an in-progress compile should ever be force-interrupted
  automatically, or whether teardown should require `--force` to proceed
  past the grace period when status shows `COMPILING KERNELS`, defaulting
  to wait-and-warn instead.

---

### Host-level teardown cleanup was inert for containerized processes

**Status:** mostly addressed in 4.8.4. `_teardown_host_container_internals()`
now reaches inside each container via `docker exec` to gracefully stop the
vLLM engine (targeted SIGTERM/SIGKILL by process pattern) and Ray (`ray
stop` / `ray stop --force`) before the container is ever stopped or
removed. This entry documents the gap that fix closes and what's still
imperfect about it.

**Context:** none of our `docker run` invocations set `--pid=host`, so
every container gets its own isolated PID namespace. `_teardown_host_processes()`'s
`ps aux | grep -E 'vllm|ray' | ...`, run over SSH directly against the bare
host, was consequently **inert for the actual deployment path** -- a host
without PID namespace sharing cannot see processes running inside a
container at all. That step only ever caught a genuinely bare-metal stray
process (leftover from manual debugging outside the normal deploy path),
not anything from a real deploy. This had been sitting in the code,
apparently doing its job, for the entire lifetime of the graceful-teardown
rewrite -- nobody had traced the actual PID namespace implications until
directly investigating a Ray-related deploy failure surfaced it.

Compounding this: in a 2-node Ray deploy, the vLLM engine runs as a
*separate*, `docker exec -d`'d process, detached from the container's own
PID 1 (`ray start --block`). Even `docker stop`'s SIGTERM -- which
correctly reaches PID 1 via `tini` -- never reached the engine either. The
engine was, in effect, never gracefully signaled by anything prior to
4.8.4: only ever killed via the abrupt kernel-level namespace teardown at
`docker rm -f` time.

**What's still imperfect:**
1. `pkill -f 'vllm.entrypoints.openai.api_server'` matches by command-line
   pattern, not PID tracking -- correct for how this orchestrator always
   launches the engine today, but would silently miss it if that
   invocation ever changes shape without updating this pattern too.
2. The original bare-host `ps aux` step is kept only as a safety net for
   the rare genuinely-bare-metal stray process case -- it remains
   structurally unable to see anything containerized, which is fine now
   that it's understood, but worth remembering if someone "fixes" it later
   assuming it was the mechanism actually protecting against orphaned
   Ray/vLLM processes.

**Depends on:** nothing further required for the common case. `--pid=host`
sharing would be the more structural fix (host-visible PIDs, reachable by
ordinary `ps`/`kill` without needing `docker exec` at all), but that's a
real isolation tradeoff -- less containment between this workload and
anything else on the host -- not something to add casually alongside
`--privileged` and `--ipc=host`, which already reduce isolation
significantly on the 2-node path. Not pursued for 4.8.4; the docker-exec
approach gets the same practical result without that additional tradeoff.

---

### IPC/shared-memory leak risk under `--ipc=host`

**Status:** mostly addressed in 4.8.4. A new `sweeping` phase runs at the
end of every teardown (`sweep_ipc_orphans()`), removing SysV shared memory
segments with `nattch == 0` -- provably unattached, a hard kernel-tracked
count, not a heuristic. Since this lives inside `_execute_teardown_impl`
itself, every deploy's own pre-deploy teardown gets it too, not just a
manually-triggered one -- this is what "clean slate on every deploy"
actually means in practice now, rather than being a manual/optional step.

**Context:** every `docker run` invocation uses `--ipc=host`. Ray's plasma
object store is shared-memory-backed, and vLLM/PyTorch's own multiprocessing
also leans on shared memory for zero-copy inter-process tensor passing.
Under `--ipc=host`, none of that is container-scoped -- it lives in the
*host's* own SysV IPC table and `/dev/shm`, independent of any one
container's lifecycle. Combined with the process-signaling gap above (the
vLLM engine specifically was never gracefully signaled before 4.8.4), any
shared memory segment not cleanly unlinked by its owning process before
death would simply persist on the host indefinitely -- unlike ordinary
process memory, it isn't reclaimed automatically on process exit. This was
suspected as the cause of at least one real deploy failure on 2026-08-23,
not just a theoretical risk.

**What's still NOT covered:**
1. **POSIX `/dev/shm` files are never auto-deleted.** `ipc_inventory()`
   (4.8.4, read-only) lists them with size and age, but nothing removes
   them. Verifying one is truly orphaned -- versus still legitimately
   mmap'd by some process -- needs cross-referencing every process's open
   file descriptors (`/proc/*/fd`) *and* memory maps (`/proc/*/maps`)
   across the whole host. That's a real, buildable check, but riskier to
   get subtly wrong without testing against the actual hosts than the
   SysV `nattch` check, which is a direct kernel guarantee requiring no
   inference at all. Deliberately deferred rather than rushed.
2. **The sweep depends on the graceful-stop step actually working.** If
   `ray stop`/the targeted `pkill` somehow fails to reach a process (see
   the pattern-matching caveat in the previous entry) and it only dies via
   the abrupt `docker rm -f` namespace teardown, any resulting unlinked
   segment wouldn't be caught until the *next* teardown's sweep runs --
   self-healing within one cycle rather than an instant guarantee, since
   the sweep is a post-hoc check on the whole host's SysV table rather
   than a targeted per-process confirmation.
3. **SysV semaphore arrays are inventoried but not swept.** `ipc_inventory()`
   reports semaphore counts; nothing removes stale ones. Semaphores don't
   have as direct a "definitely orphaned" signal as `nattch` -- worth
   researching whether an equivalent safe check exists before adding this,
   rather than assuming the same pattern trivially extends.

**Shape of remaining work:**
- Build the `/proc/*/fd` + `/proc/*/maps` cross-reference for POSIX
  `/dev/shm` files, test it against the actual hosts before wiring it into
  an automatic sweep (unlike the SysV case, this one needs real validation
  before being trusted to delete anything).
- Consider whether `--pid=host` (see previous entry's tradeoff discussion)
  would also make the process-liveness side of this more directly
  verifiable, if the isolation tradeoff is ever revisited.

**Depends on:** nothing for what's already shipped. The `/dev/shm` file
sweep is independent follow-up work, not blocked by anything else here.

---

## Phase 3 inputs: topology and schema stability

### Recipes hardcode host-specific NCCL/Gloo interface names

**New, 2026-08-29.** Every 2-node recipe carries
`NCCL_SOCKET_IFNAME=enp1s0f0np0` and `GLOO_SOCKET_IFNAME=enp1s0f0np0` in its
`env_vars`. `cluster_config.yaml` *also* declares `network.interface:
enp1s0f0np0` and `network.nccl_ib_hca: rocep1s0f0`. Same value, two places,
and the recipe copy is what actually reaches `docker run`.

**Why this matters:** this is the same class as the `PRIMARY_HOST` /
`SECONDARY_HOST` hardcoding eliminated in V4.8.5 -- host identity living in
a file that shouldn't own it. It's invisible today because there is exactly
one pool with one NIC name. The moment `spark-5`/`spark-6` come online as a
second fabric pool with different interface names, every 2-node recipe in
the catalog is silently wrong for that pool, with no mechanism to vary it
per-pool because it's baked into per-model YAML. That's a
break-every-recipe migration, and it is already written.

**Confirmed prior art:** eugr does not do this. `launch-cluster.sh`'s
`get_env_flags()` injects `NCCL_SOCKET_IFNAME`, `MN_IF_NAME`,
`UCX_NET_DEVICES` and per-node `VLLM_HOST_IP`/`RAY_NODE_IP_ADDRESS` from
values `autodiscover.sh` derived at launch time. Their recipes carry no
interface names at all. Their discovery also handles a case we will
eventually hit: four active CX7 interfaces means *mesh* topology, which
needs a different NCCL variable set entirely
(`NCCL_NET_PLUGIN=none`, `NCCL_IB_SUBNET_AWARE_ROUTING=1`,
`NCCL_IB_MERGE_NICS=0`). `cluster_config.yaml` already reserves that
distinction with `network.topology: switched`.

**Shape of a real fix:**
- Derive the interface env vars in the deploy path from
  `cluster_config.yaml`'s `network:` block (eventually per-pool), and strip
  them from recipe `env_vars`.
- While in there, confirm whether our 2-node loop sets `VLLM_HOST_IP` and
  the Ray node-IP vars *per host* or once for all hosts. eugr sets them
  per-node explicitly. Getting this wrong across pools would be subtle and
  hard to attribute.
- Decide whether the mesh-vs-switched variable set belongs in
  `cluster_config.yaml` as data or in code keyed off `network.topology`.

**Depends on:** nothing to start. Deadline is external -- this wants to land
*before* `spark-5`/`spark-6` become a real second pool, not after.

---

### `compute_config_hash()` stability ahead of Phase 3's topology-key change

**New, 2026-08-29.** `compute_config_hash(recipe, topo_key)` takes the
topology key as an input, and every historical hash is therefore bound to
today's `1_node`/`2_node` naming. Phase 3's N-node generalization is
expected to restructure exactly that -- to pool-aware keys, or `N_node`, or
something else not yet decided.

**Why this matters:** the hash is the join key for launch-success tracking
and `historical_tps` lookups. If the key scheme changes, every accumulated
hash silently stops joining, and the "has this exact configuration ever
launched successfully" history is lost -- which is precisely the data the
per-recipe status marker entry above intends to auto-promote from. There is
already evidence this join is fragile (see the `benchmark_ledger.csv` key
mismatch entry). Losing months of accumulated validation data at the moment
Phase 3 lands would be a bad and entirely foreseeable surprise.

**Shape of a real fix:** decide *now*, before more data accumulates, either
to version the hash (so old and new schemes coexist and old rows remain
attributable) or to make it topology-key-independent. This is a decision to
record, not code to write today -- but it gets more expensive with every
month of data written under the current scheme.

**Depends on:** informs Phase 3 rather than blocking it. Cheapest to settle
before Phase 3 design work starts, not during.

---

### Second cabled ConnectX-7 port exists between spark-3/spark-4, unused and undocumented

**Context, 2026-08-29.** While debugging an unrelated `ethtool` false-positive
(a driver/firmware quirk returning stale link state and cached module EEPROM
data on the *other* port -- see `docs/TROUBLESHOOTING.md` Incident #9), it
came out that each Spark has two ConnectX-7 ports, and confirmed
(`ethtool -m`, matching vendor serial across both nodes' `enP2p1s0f0np0`)
that one is genuinely cabled between spark-3 and spark-4, currently entirely
unused -- `enp1s0f0np0` (the RoCE link everything actually runs on today) is
a separate physical port from the idle one.

**Why this belongs here, not just in the incident log:** `README.md`'s
network fabric section and `docs/TROUBLESHOOTING.md` both already flag that
"a deployed pair must actually share a fabric" isn't yet expressed anywhere
in code (see the Phase 3 section below and the host-identity entries in
TROUBLESHOOTING). A second real, physically-cabled link between the current
production pair is exactly the kind of physical-topology fact that
constraint will eventually need to reason about -- whether as failover
capacity, a second NCCL channel, or simply something to document so a
future person doesn't waste time re-discovering it the way tonight did.

**What's missing today:** nothing consumes this. It's not wired into
`cluster_config.yaml`, not used by NCCL/Gloo binding, not referenced
anywhere. Currently pure headroom.

**Shape of a real fix:** not a fix so much as a scoping question for
whoever picks up Phase 3 (`ARCHITECTURE-MIGRATION-PLAN.md`) -- worth
deciding then whether the second port becomes a second NCCL channel for
bandwidth, a failover path, or is left alone deliberately. Not urgent on
its own.

**Depends on:** nothing directly. Informs Phase 3 planning rather than
blocking anything today.
