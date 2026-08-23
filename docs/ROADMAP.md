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
