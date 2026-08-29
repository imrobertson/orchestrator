# Roadmap: v4 -> v5

Tracks work against the control plane's own API version (`FastAPI(...,
version="4.8.x")` in `dgx-orchestrator.py`), separate from
`ARCHITECTURE-MIGRATION-PLAN.md`'s Phase 1-5 numbering, which tracks the
models.yaml -> recipes/ migration specifically. Put things here when they're
about runtime robustness/behavior rather than the config-format migration.

Each entry: what's wrong today, why it matters, and a rough shape for the
fix -- not a full spec. Flesh out into a real prompt (see
`PHASE-2-PROMPTS.md` for the style) when it's actually picked up.

---

## Open

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

Both are documented in `docs/TROUBLESHOOTING.md` now, but only as
after-the-fact incident write-ups -- nothing stops a third recipe from
reintroducing either pattern, and a recipe carrying one can sit in git
looking fine (valid YAML, passes schema validation) until the moment
someone actually deploys it.

**Shape of a real fix:**
- A lightweight recipe linter -- either folded into `load_recipes()`
  itself (warn at load time, don't hard-fail the whole catalog per the
  existing fail-closed behavior) or a standalone `tools/lint_recipes.py`
  run in CI -- checking `vllm_args` for a small, explicit list of known-bad
  flag combinations as they're discovered. Start with the two above; this
  list only grows by adding real incidents, not by trying to anticipate
  hypothetical ones.
- Cheap enough to start: a dict of `{flag_or_value: incompatible_with}`
  pairs and a regex/shlex-split check against `vllm_args` at recipe load
  time, surfaced the same way an unrecognized `recipe_version` already
  warns today (soft warning, not a hard failure, unless confidence is
  high).

**Depends on:** nothing. Small, and the two known cases above are already
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
