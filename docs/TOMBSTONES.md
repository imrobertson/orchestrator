# Control Plane Release Tombstones & Fix Log

### 83. Smoke test's independent-verification SSH calls silently broke on multi-word `--format` strings (tests/smoke_test_mods.py)

* **The Trap:** `tests/smoke_test_mods.py` hand-rolled its own `ssh_run()`
  helper for "independent verification" -- checking `common/mods.py`'s
  output via plain `docker inspect` calls that don't go through the code
  being tested. Reasonable goal, wrong layer: the helper duplicated
  `common/ssh.py`'s `run_ssh()` almost verbatim but dropped its
  per-argument `shlex.quote()`-then-join step before handing the command
  to `ssh`. OpenSSH re-concatenates multiple argv elements into ONE string
  for the remote shell regardless of how carefully the local Python list
  was built -- there is no wire-level argv boundary preservation. Any
  argument containing an internal space silently splits into extra, wrong
  tokens on the far end. `--format {{json .Config.Entrypoint}}` (space
  between `json` and `.Config.Entrypoint`) became `--format {{json` plus a
  stray sixth `.Config.Entrypoint}}` argument to `docker inspect`, which
  failed and printed nothing to stdout -- but the check only compared
  `base_ep == derived_ep`, both empty strings, with no `returncode` check.
  Two broken calls silently agreeing with each other read as a PASS.
  `--format {{.Config.WorkingDir}}` (no internal space) happened to survive
  unquoted and gave a real, correct result, which is exactly what made the
  Entrypoint/Cmd false-pass easy to miss at a glance -- most of the same
  function's checks were genuinely fine.

  This is the identical failure class `common/ssh.py`'s own module
  docstring already describes fixing once (`run_ssh()` "previously existed
  as two near-verbatim-but-drifted copies"). Writing a second SSH
  transport for "independence" reintroduced the exact bug that
  consolidation was meant to prevent.

* **The Fix:** `ssh_run()` in `tests/smoke_test_mods.py` is now a one-line
  adapter over `common.ssh.run_ssh()` instead of a parallel implementation.
  "Independent verification" means not calling `ensure_mods_baked()` to
  check its own output -- it does not mean re-deriving SSH transport
  correctness from scratch. Every check that reads `.stdout` after an
  `ssh_run()` call now also asserts `.returncode == 0` explicitly, so a
  broken verification call fails loudly instead of two empty strings
  quietly agreeing.

  General rule going forward: any new script that shells out to a remote
  host via `ssh <args...>` as a Python argv list, rather than through
  `common.ssh.run_ssh()`, needs the same `shlex.quote()`-per-arg-then-join
  treatment -- or it needs to just call `run_ssh()`. There is no
  "obviously safe" format string; the trigger is a single internal space,
  which arbitrary Go template format strings, `bash -c` one-liners, or any
  argument containing a shell metacharacter can carry without warning.
  ### 82. Silent HF Token Failures in get_hf_token() (common/ssh.py)
* **Symptom:** vLLM 401'd against a gated/private HF repo with zero
  indication a token was ever the issue — the container launched with no
  HF_TOKEN set, no warning at deploy time. Surfaced during a Gemma 4
  recipe debugging session (2026-08-29) and stayed misleading through
  two separate root causes before it was actually fixed: first looked
  like the bad hf_path alone, then after that was fixed, looked like a
  missing token entirely.
* **Cause (two distinct bugs in the same function, found sequentially):**
  1. The .secrets-parsing branch used a bare `except Exception: pass`,
     silently swallowing any parse failure (the original malformed-token
     case) and falling through to the ~/.cache/huggingface/token fallback
     with no trace. The ~/.cache fallback had the same bare-except
     pattern. An HF_TOKEN= line that parsed fine but stripped to an empty
     string also returned "" immediately from inside the loop, skipping
     even the function's own generic "no token found" warning at the
     bottom.
  2. After (1) was fixed and re-deployed, the SAME symptom recurred from
     a second, previously-undiscovered gap: `line.startswith("HF_TOKEN=")`
     is exact-case, so a `.secrets` line written as `HF_Token=` (or any
     non-canonical casing) never matched at all. No exception, no partial
     match, no warning — the for-loop just completed normally having
     matched nothing, and fell through silently exactly like bug (1) did,
     just via an entirely different, still-uncovered code path.
* **Fix:** Both except blocks now log `type(exc).__name__: exc` instead
  of passing silently. An empty-after-strip token value in .secrets no
  longer returns early — it warns and falls through to the next source
  instead of masquerading as "found." Key matching now uses
  `line.partition("=")` + `key.strip().upper() == "HF_TOKEN"` instead of
  `startswith("HF_TOKEN=")`, so any casing/whitespace variant of the key
  matches correctly instead of silently missing.
* **Lesson:** A silent-failure fix that closes one code path can still
  leave a structurally identical silent-failure path right next to it
  uncovered — worth explicitly asking "are there other ways to reach the
  same silent-empty-return, not just the one that just bit me" before
  calling a fix like this complete.
* **File:** common/ssh.py, get_hf_token()

### 81. Long-Lived Daemon Slowly Exhausted the Container's Process Table (V4.8.7)
* **The Trap:** `common/ssh.py`'s `run_ssh()` uses SSH's `ControlMaster=auto` / `ControlPersist=60s` for connection reuse — by design, a `ControlPersist` master detaches from the SSH client that spawned it so it can outlive that client. When its original parent process exits, standard Unix reparenting hands it to PID 1 of its namespace. The Dockerfile's `CMD` runs `python3 dgx-orchestrator.py daemon` directly as PID 1, with no `tini`/`dumb-init`/`--init` — and a bare Python process has no general-purpose logic to reap arbitrary reparented children (it only ever waits on processes it directly spawned itself, which is correct for those, but says nothing about orphans reparented to it from elsewhere). Every `ControlPersist` master that got reparented and later exited became a zombie nothing ever collected. Over enough days of continuous status polling (SSH calls against every host, every 4-10s, forever), this is a slow, steady climb toward the container's PID ceiling. Surfaced as `dgx-config` failing with `OCI runtime exec failed ... nsexec-0[...]: unable to spawn stage-1: Resource temporarily unavailable` — `runc` failing to fork a new process inside the container because there was no room left. Both the dashboard's teardown button and `dgx-config teardown` failed identically (same underlying container, same PID exhaustion) until a full `docker compose down`/recreate cleared the whole PID namespace and gave temporary relief — a strong tell in hindsight that should have pointed straight at container-level resource exhaustion rather than anything in `dgx-orchestrator.py`'s own logic.
* **The Fix:** Added `init: true` to `orchestrator-api` in `docker-compose.yml`, which runs Docker's built-in `tini`-based init as PID 1 instead of the raw Python process. `tini` correctly reaps any reparented orphan regardless of what spawned it, closing the leak at its actual source with a one-line config change and zero code changes. `run_ssh()`'s own docstring had already correctly identified this exact leak mechanism in the abstract (see its "Process-tree cleanup on timeout" section) without yet being connected to this specific production symptom — worth rereading that docstring in full if this class of issue ever resurfaces elsewhere.

### 80. `ACTIVE_DEPLOYMENT_STATE`'s In-Memory Cache Didn't Survive `dgx-config`'s Own Execution Model (V4.8.7)
* **The Trap:** #77 introduced `ACTIVE_DEPLOYMENT_STATE` as a disk-backed *but also in-memory-cached* dict — loaded once at daemon startup, then mutated in place by `_set_active_deployment()`/`_clear_active_deployment()`, with every read going through that cached global. This assumed every writer was the long-running daemon process itself. It isn't: `dgx-config` is a bash wrapper that `docker exec`s straight into the *same* running `dgx-orchestrator-api` container to run `python3 dgx-orchestrator.py cli ...` — a genuinely separate process from the daemon's own PID 1, every single invocation. A CLI-triggered deploy wrote the correct record to the JSON file on disk and exited; the daemon's in-memory copy never saw that write and kept serving `active_recipe_key: null` via `/api/status` indefinitely, even though `docker exec dgx-orchestrator-api cat active_deployment_state.json` showed the correct data sitting right there the whole time. Confirmed directly: file on disk had the right `catalog_key`, live API response for the same host was `null`, simultaneously.
* **The Fix:** Removed the in-memory global entirely. `_set_active_deployment()`/`_clear_active_deployment()` now do a read-modify-write straight against disk on every call; the sole read site (inside `_resolve_active_recipe()`) now calls `_load_active_deployment_state()` fresh each time instead of touching a cached dict. This is the same pattern `model_ledger.json` and `hf_path_ledger.json` already used via `_read_json_state()` — `ACTIVE_DEPLOYMENT_STATE` was the one file given different (and, it turned out, wrong) treatment for no real reason. Verified with a standalone test simulating two fully independent processes (one writing, one reading with zero shared memory) confirming the always-fresh-read approach sees the write correctly. **General lesson for this codebase specifically:** any in-memory cache of state that `dgx-config` can also write is unsafe by construction, because `dgx-config` invocations are never the same process as the daemon, even though they run inside the same container.

### 79. `ORCHESTRATOR_VERSION`'s Hash Suffix Was Broken Two Different Ways Before It Actually Worked (V4.8.6)
* **The Trap:** First attempt appended a short `git rev-parse --short HEAD` commit hash (+ `-dirty` flag) to `ORCHESTRATOR_VERSION`, computed at daemon startup via `subprocess`. This assumed a live git checkout with `.git` history was reachable at runtime — not true for this daemon: the Docker image is built via `COPY . .`, and while `.git` wasn't deliberately excluded, the more fundamental problem is portability — this makes the code awkward to hand to anyone without access to this specific git history, and confirmed in production it degraded to a permanently-silent `"+unknown"` on every real startup regardless, since the bare `except Exception: return "unknown"` swallowed whatever the actual failure was with zero diagnostic trail. Second attempt replaced the git dependency with a hash of the running file's own bytes (`hashlib.sha256(Path(__file__).read_bytes())`) — no git needed, portable to anyone regardless of repo access — but *also* silently returned `"+unknown"` in production, for a different and still-undiagnosed reason at the time, because the same silent-except pattern was carried over into the replacement code.
* **The Fix:** `Path(__file__).resolve()` instead of a bare `Path(__file__)` — `__file__` isn't guaranteed to already be an absolute path, and a relative path resolved against a working directory that doesn't match where the file actually lives (a real risk in a containerized daemon's launch command) fails a bare `read_bytes()` silently. Also added `print()` logging to the `except` block itself, so a third failure (if one ever happens) is diagnosable instead of another silent `"unknown"`. Broader lesson, already called out elsewhere in this log but worth restating: a version-identity mechanism that can silently fail closed into looking fine (`"+unknown"` doesn't error, it just quietly stops being useful) needs its own failure path logged from the moment it's written, not added later once it's already shipped broken twice.
* 
### 78. Dashboard Teardown Silently Reported Success on Real Per-Host Failures (V4.8.6)
* **The Trap:** `_execute_teardown_impl`'s `finally` block unconditionally set the completion message to `"Teardown complete for {hosts}"`, regardless of what the per-host `results` dict actually recorded — a `docker rm` timeout or non-zero exit was captured correctly in `results[h]` (e.g. `"Error: docker rm timed out..."`) but never reflected in `TEARDOWN_STATE`'s final message. `/api/teardown` then returned that `results` dict as a bare 200 OK with no inspection at all, so the dashboard's own `!response.ok` error-toast branch in `index.html` — which already existed and already worked correctly — never had a reason to fire. The CLI's `teardown` subcommand hit the exact same possible per-host failures (same `execute_teardown()` call) but `print(json.dumps(...))`s the raw results dict, so failures were visible there and only there. This is what made dashboard teardown look markedly less reliable than CLI teardown despite both going through identical backend logic: a host where `docker rm` genuinely failed kept its container running, ACTIVE_DEPLOYMENT_STATE still got cleared unconditionally (see #77), and the dashboard reported "done" — leaving the Model Deployer panel showing nothing active while the host's own panel still showed a loaded model, with no indication anything needed manual attention until someone noticed the mismatch and killed the container by hand.
* **The Fix:** Added `_teardown_results_are_clean()` — a missing or empty `results` entry for any target host counts as failure, not success, since an exception raised before the "removing" phase populated `results` was previously indistinguishable from a clean run. The `finally` block now composes an accurate completion message listing exactly which hosts failed and why, and sets `TEARDOWN_STATE["phase"] = "error"` when that happens (new value alongside the existing idle/signaling/stopping/removing/sweeping/done). `/api/teardown` now raises `HTTPException` on real failure — a per-host error, or `CLUSTER_OP_LOCK` busy — bringing it in line with `api_deploy()`/`api_benchmark()`, which already both checked their result's status; this endpoint was the one overlooked outlier. No frontend changes were needed: `index.html`'s error-toast handling already existed and simply needed a non-2xx response to react to. Not yet verified against a live per-host failure on production (no failing teardown to test against at the time of the fix) — worth confirming the error toast and message actually render correctly next time a host genuinely fails to tear down.

### 77. Dashboard Model-Select Dropdown Silently Showed the Wrong Deployed Recipe (V4.8.6)
* **The Trap:** The dashboard (and `SESSION_TRACKER`'s lifetime-token attribution, and the ledger auto-detect CLI's `_detect_live_model_topo_metrics()`) all resolved "which recipe is currently running" the same way: fuzzy-matching the served checkpoint's display name (`active_model`, e.g. `"DeepSeek-V4-Flash-0731"`) against catalog keys and `hf_path`s via `_resolve_catalog_key()`. Two recipes serving the identical checkpoint under materially different configs — `deepseek-v4-flash-0731-1M` and `deepseek-v4-flash-0731-dspark-sm120` both report the same served name — collide under that match by construction, so the code silently took whichever catalog entry it happened to iterate to first, independent of which recipe was actually deployed. Reported symptom: the model-select dropdown kept snapping back to `-1M` regardless of what was actually launched. Same ambiguity also risked misattributing lifetime prompt/generation token counts to the wrong recipe's `model_ledger.json` entry, silently — a data-correctness bug, not just a UI one.
* **The Fix:** Added `ACTIVE_DEPLOYMENT_STATE`, a disk-backed (`active_deployment_state.json`) per-host record of `{catalog_key, topo_key, config_hash}`, written by `execute_deployment()` at the exact moment it knows what it launched — reusing the same `config_hash` already computed for `PENDING_LAUNCH_STATE` (see #76's launch-success tracking), not a new hashing mechanism. Cleared per-host on teardown (unconditionally, same "always resets" reasoning as `TEARDOWN_STATE`), and loaded into memory once at daemon startup so it survives a restart — memory-only was considered and rejected, since that would silently reintroduce the exact ambiguous-guessing failure mode after every restart. Only trusted when live container discovery confirms something is actually running on that host (`active_container != "None"`), so a stale record can't survive an out-of-band `docker rm` done outside the orchestrator. The three independent copies of "prefer the exact record, fall back to fuzzy match" this fix initially produced (one each in `_finalize_host_status`, `_compute_cluster_status_impl`, `_detect_live_model_topo_metrics`, each with subtly different gating) were consolidated into a single `_resolve_active_recipe()` helper in the same pass, once the duplication was noticed. `/api/status` now exposes `active_recipe_key`/`active_config_hash` per host; `index.html`'s `fetchStatus()` reads that directly instead of re-deriving it via string matching. Also added a git-commit-hash suffix (+ `-dirty` flag) to `ORCHESTRATOR_VERSION`, computed once at daemon startup — the existing hand-written### 76. `SessionTracker` Self-Deadlock — The Real Cause of the Multi-Hour Dashboard Freezes (V4.8.5)
* **The Trap:** `SessionTracker.lock` was a plain `threading.Lock()`. `update()` holds this lock for its entire body, and when its periodic "flush while still active" condition fires (>1hr of sustained activity — true of essentially any real serving session), it calls `self._commit_session()`, which *also* acquires the same lock. A plain `Lock` cannot be re-acquired by the thread already holding it: permanent, deterministic, self-inflicted deadlock, with no recovery short of restarting the process. Since `get_cluster_status()` only ever keeps one `_STATUS_INFLIGHT` future in flight at a time (see #40), this single wedged thread froze *all* status polling indefinitely — the actual root cause of the dashboard-frozen-for-hours incidents on 2026-08-25, 08-27, and 08-28, pre-existing before any of this session's other fixes and misdiagnosed each time as something else (see #74, and the ruled-out SSH/thread-pool theories below). This is also why restarting the daemon only ever gave temporary relief: a fresh `SessionTracker` starts unlocked, then reliably deadlocks again roughly an hour into the next real session.
* **The Fix:** `threading.Lock()` → `threading.RLock()` — reentrant-safe for the same-thread re-acquire case, no change to cross-thread contention semantics. Found via a live `py-spy dump` against the actually-wedged production process, not inferred — the stack trace showed the exact `_commit_session` → `update` → `_compute_cluster_status_impl` chain, blocked. Proven with a reproduction of the real production call sequence: confirmed the original `Lock()` deadlocks within 5s every time, and `RLock()` completes and flushes correctly every time. Two theories investigated and ruled out along the way, kept here so they aren't re-walked: SSH subprocess-level hangs in `run_ssh()` (its `subprocess.run(..., timeout=...)` reliably kills its child and returns within its own timeout, even against a detached grandchild holding stdout/stderr open); and naive `WORKER_POOL` backlog growth from serial polling (tested over 150s against a deliberately slow host — the pool self-limits, backlog stabilizes). A third, `WORKER_POOL` starvation from a stuck teardown, is real (see #70) but bounded to roughly 5 minutes worst-case, not hours — a genuine but secondary bug, not this one.

### 75. `get_cluster_status()` Silently Served a Frozen Snapshot Forever (V4.8.5)
* **The Trap:** When the in-flight status computation failed or timed out, `get_cluster_status()` fell back to serving the last successful cached snapshot — reasonable for a transient blip, but with no way to tell "slightly slow poll" apart from "this has been dead for hours." Combined with #76, this is exactly what let the deadlock run for hours across three separate incidents without the dashboard visibly indicating anything was wrong — it just kept looking like a live, if boring, snapshot.
* **The Fix:** Every response from `get_cluster_status()` now carries `stale` / `stale_for_seconds`, computed against how long the currently-served cache has actually been in use. Nothing consumes this to change behavior yet — it's just present in the JSON — but it's what let the #76 investigation catch the deadlock live and prove it was real and ongoing (`stale_for_seconds: 14884.6` against an actual wedged process) instead of a one-off screenshot. Surfacing it as a dashboard banner is open — see `ROADMAP.md`.

### 74. `server_time` Mislabeled `EST` While Actually Emitting Naive UTC (V4.8.5)
* **The Trap:** `server_time` was built from `datetime.datetime.now()` — naive, but correct in value since the container's system clock is genuinely UTC — with a hardcoded literal `" EST"` suffix appended. The value was never actually Eastern time, just mislabeled. Almost certainly the cause of the dashboard clock appearing roughly 5 hours in the future: something downstream saw the `"EST"` label and applied its own (also DST-unaware) conversion to a value that didn't need one. This bug and #76's frozen-status bug were stacked and only became distinguishable once #76 was fixed — a frozen wrong value and a live-but-mislabeled wrong value both just look "wrong," for completely different reasons.
* **The Fix:** `server_time` now emits real, explicit, tz-aware UTC (`datetime.now(datetime.timezone.utc)`) with an unambiguous `"UTC"` suffix, removing any excuse for a consumer to "correct" it further. Reminder: the dashboard clock's purpose is a UTC reference for log comparison, not a wall clock.

### 73. Ten Hardcoded `spark-3`/`spark-4` Literals — Primary/Secondary Host Refactor (V4.8.5)
* **The Trap:** Ten separate places in `dgx-orchestrator.py` hardcoded the literal strings `"spark-4"` / `"spark-3"` (one single-quoted and missed on the first grep pass — neither quote style alone is a complete search) or the literal management IP `10.0.14.43`, instead of deriving host identity from `HOSTS` / `cluster_config.yaml`. One of these was a genuine landmine, not just a code-smell: the 2-node deploy path's `target_hosts` was hardcoded regardless of the `head` argument actually passed, meaning a 2-node deploy aimed at any host pair other than spark-3/4 would have silently targeted — and torn down — spark-3/4 instead. This mattered now specifically because the cluster is no longer guaranteed to be exactly two nodes on one interconnected segment; a future host pair may live on a different network segment with no ConnectX-7 fabric between it and spark-3/4, so hardcoding the pair anywhere in the deploy or teardown path is unsafe by construction, not just inflexible.
* **The Fix:** Added `PRIMARY_HOST`, `SECONDARY_HOST`, and `PRIMARY_HOST_IP` constants, derived once from `HOSTS` (first/second listed host in `cluster_config.yaml`), and replaced every hardcoded literal with them. Proven both directions: loaded the same code against two synthetic `cluster_config.yaml`s (spark-3/4, and a hypothetical spark-5/6) and confirmed byte-identical behavior to the original hardcoded values for the existing config (zero regression), and fully correct independent derivation for the other. This is what makes it safe to eventually run a second, independent orchestrator instance against a different host pair with zero further code changes — see the `maestro2` discussion in `ROADMAP.md`. Note this refactor is naming/derivation only: it does not yet express the network-segment-pairing constraint (a host pair must share a fabric) anywhere in code — see `ARCHITECTURE-MIGRATION-PLAN.md`'s Phase 3 section, which still needs this written in before any N-node allocator code gets built against it.

### 72. `SessionTracker` Re-Baselined to Zero on Every Restart, Discarding History (V4.8.5)
* **The Trap:** A freshly-instantiated `SessionTracker` (e.g. after any daemon restart) unconditionally re-baselined its running counters to "whatever vLLM's live cumulative counter reads right now" — silently discarding the tracker's entire prior view of reality. Confirmed in production: one orchestrator restart reduced a session's real lifetime totals of ~29,000,000 prompt tokens and ~730,000 generation tokens to 190/763 in the ledger — a roughly 152,000x undercount, not a rounding error.
* **The Fix:** Added `_load_last_seen_raw()`, which reads the ledger's persisted `last_seen_raw` checkpoint (now written on every `_commit_session()` call) on the active-transition and resumes from there if vLLM's live counters are still ≥ the checkpoint — i.e. a restart correctly resumes instead of re-baselining to zero. Falls back to the original fresh-start behavior only when counters are genuinely lower than the checkpoint (a real engine redeploy, not just an orchestrator restart). Proven with a reproduction covering all three cases: resume-after-restart, genuine-redeploy, and fully-explicit args. Added `dgx-config correct-ledger` (also `dgx-config cli correct-ledger` and `POST /api/correct-ledger`, plus a standalone `correct_ledger.py` break-glass script) as a one-off repair tool for an already-corrupted ledger entry — auto-detects the currently-serving host/key/topology/live metrics when run with no arguments, refuses to overwrite with smaller values unless `--force`, and backs up the whole ledger file (timestamped) before any write. The specific 190/763 entry from this incident was corrected with it in production (dry-run previewed, then applied for real).
* * **Repair semantics (documented 2026-08-30):** `correct-ledger` *sets*
  `lifetime` and `last_seen_raw` rather than adding to them. For a key with
  a single continuous engine lifetime this is correct — the surviving
  (wrong, small) `lifetime` numbers are a subset of what `/metrics` reports,
  not a separate amount to sum with it, so adding would double-count. **This
  only holds for a single launch.** If a key has accumulated across multiple
  engine lifetimes, the live counters cover just the most recent one, and
  set-semantics would silently discard the earlier history. The `--force`
  guard won't catch that case: the new values will usually still be larger
  than the corrupted ones, so the sanity check passes while the result is
  wrong. Check launch history before repairing a key that may have had more
  than one.

### 71. Crashed-Worker Logs Didn't Survive Teardown, Making Diagnosis Impossible After the Fact (V4.8.5)
* **The Trap:** Ray's session directory, including a crashed worker's stdout/stderr, lived only inside the container's own `/tmp/ray` with no persistent mount. A container that crashed and was subsequently torn down took its own crash evidence with it — this is precisely why the first of the two 08-25 OOM crashes (06:15 UTC) could never be conclusively diagnosed: its logs were already gone by the time anyone went looking, and it's now believed to be the same OOM mechanism as the second, confirmed crash, but this remains unconfirmed and probably always will be.
* **The Fix:** Every deploy now binds a per-run, per-host directory (`~/.cache/ray-logs/<deploy_run_id>/<host>`) to each container's `/tmp/ray`, so the session directory survives container teardown. This is what made the second 08-25 crash (16:24 UTC) diagnosable at all, and root-caused it for real: Ray's own node-memory monitor OOM-killed a worker at 95% host memory usage (`threshold_memory_monitor.cc`), not a driver or kernel fault — `gpu_util: 0.82` on the 1M-context 2-node recipe left only ~22GB of headroom on the 121.69GB unified memory pool for JIT caches, page cache, and Ray/Python overhead over a multi-hour session (see the related `gpu_util` correction already in `USER MEMORY`/recipe history). Also added: `tuning.debug_launch_blocking` (default `false`), which sets `CUDA_LAUNCH_BLOCKING=1` for forcing synchronous CUDA kernel launches when actively chasing a repro (costs real decode throughput — not meant to be left on), and `dgx-config prune-ray-logs [--retention-days N] [--dry-run]`, age-based cleanup defaulting to `tuning.crash_log_retention_days` (7 days), since these logs are small enough that waiting for disk pressure would let them accumulate indefinitely. New `cluster_config.yaml` / `TuningConfig` fields (`debug_launch_blocking`, `crash_log_retention_days`) both default to today's implicit behavior, so an older config file without them keeps working unchanged.

### 70. `_execute_teardown_impl`'s Futures Had No Timeout At All (V4.8.5)
* **The Trap:** Unlike the rest of the file's `_collect_bounded()` pattern (see #40), `_execute_teardown_impl`'s `.result()` calls across its four phases had no `timeout=` whatsoever. A genuinely stuck sub-operation could wedge teardown indefinitely — and since teardown shares `WORKER_POOL` with status polling, a wedged teardown could starve `get_cluster_status()` too. Investigated as a candidate root cause for the #76 multi-hour freezes; demonstrated real with an artificially infinite stuck sub-operation, but with realistically bounded slow operations (matching what the leaf `run_ssh()` calls' own timeouts already allow), the resulting staleness is a single episode that self-heals once the stuck operation completes — bounded to roughly 5 minutes worst case, not hours. A real, fixed bug, but not the #76 root cause.
* **The Fix:** Added explicit timeouts to each phase's `.result()` calls, matching each phase's own already-established worst case (sum of its sequential `run_ssh` timeouts): roughly 35s / 180s / 90s / 15s across the four phases, ~320s worst case total.

### 69. `sweep_ipc_orphans` Required a Terminal & Over-Broad Sudo Scope (V4.8.5)
* **The Trap:** The IPC sweep introduced in #64 ran as `sudo python3 -c <whole script>`. In production this failed outright with "a terminal is required," since `run_ssh(capture=True)` never allocates a TTY. Even with a TTY, a sudoers rule matching `python3 -c *` would grant passwordless root execution of arbitrary Python — far broader than the sweep itself needed.
* **The Fix:** `sudo` now wraps only the actual `ipcrm -m <shmid>` call inside the script; the outer invocation (reading `/proc/sysvipc/shm`) needs no privilege at all and runs unprivileged. This lets the sudoers entry be scoped to exactly `NOPASSWD: /usr/bin/ipcrm -m *`. Verified in production: the corresponding sudoers entry was added on both hosts, and `dgx-config sweep-ipc-orphans --dry-run` ran clean on both.

### 68. `common/ssh.py` Hardening — Real, But Narrower Than First Suspected (V4.8.5)
* **The Trap:** Investigated as a candidate cause of the #76 freezes before the real root cause was found. `subprocess.run()`'s single-process kill on timeout doesn't clean up an orphaned child that has forked further, and a stale-but-still-"established" TCP connection can sit for a long time before either side notices.
* **The Fix:** Added `ServerAliveInterval=5` / `ServerAliveCountMax=2`, letting SSH itself detect and tear down a genuinely-dead-but-still-established connection within ~10-15s. Added process-group kill (`os.setsid` on launch, `os.killpg` on timeout) instead of relying on `subprocess.run`'s single-process kill — this correctly reaps an ordinary orphaned child with zero downside, but **cannot** reach a child that deliberately detaches into its own session (a real SSH `ControlPersist` master almost certainly does this — no process-group trick from the calling side can reach it, by design). `ControlPersist=60s` is self-bounding regardless (the master exits on its own after 60s idle), so this was never actually an unbounded leak in production, just modeled that way in an overly pessimistic synthetic test. Neither fix turned out to be the #76 root cause, but both are solid hardening on their own merits.

### 67. Daemon Never Cleaned Up Its Own Stale SSH Multiplex Sockets (V4.8.5)
* **The Trap:** `dgx-config`'s CLI wrapper already flushes stale SSH multiplex sockets (`/root/.ssh/cm-*`, `/tmp/cm-*`) before every invocation, but the long-running daemon process itself never did this for its own multi-day uptime — an accumulation risk noted but not root-caused as part of the #76 investigation (four simultaneous SSH mux/master processes against the same `ControlPath`, spawned within about a minute of each other, were observed in `py-spy`/`docker top` output during that investigation and flagged as unusual, but not conclusively tied to any specific incident).
* **The Fix:** The daemon now flushes its own stale multiplex sockets every ~5 minutes, piggybacked on the existing 10s telemetry loop, matching the hygiene the CLI wrapper already performed.

### 66. Benchmark Always Targeted `spark-4` Regardless of What Was Actually Deployed (V4.8.5)
* **The Trap:** `index.html`'s `headSelect` dropdown value was only ever set once, from its hardcoded HTML default — and its containing row is fully removed (`display: none`) for any model whose recipe defines only a single topology, which every model exercised this session turned out to be. `triggerBenchmarkNow()` read that stale, often-unreachable value unconditionally, so a dashboard-triggered benchmark could silently target the wrong host regardless of where the model was actually serving. The CLI path (`dgx-config deploy --benchmark`) was checked separately and confirmed **not** independently affected — it threads its own explicit `--head` value straight through correctly.
* **The Fix:** Every status poll now syncs `headSelect`'s value to the backend's real, live-discovered `serving_host` — a field already used elsewhere on the dashboard, just never read by the frontend before. Since benchmarking's entire purpose is testing what's already deployed, this makes the target correct by construction instead of depending on a control the user often couldn't even see. Not yet manually verified in a real browser (only `node --check`'d for syntax) — worth a click-through pass, especially for a single-topology model where the row itself stays hidden.

### 65. No Reliable Way to Confirm Which Code Was Actually Running (V4.8.5)
* **The Trap:** A fix could be built, tested, and handed over, edited into the repo, and still not actually be running — the deploy workflow (`git push` locally → `git pull` on `maestro` → copy into the container → restart) has a step for a forgotten `git push` to silently no-op at. This happened for real during the #76 investigation: a fix landed in the repo but the daemon kept running the old, still-broken code for a while before anyone noticed, because there was no way to check "is my latest fix actually running" short of `grep`-ing the live container for a known string.
* **The Fix:** Added `ORCHESTRATOR_VERSION`, a string constant meant to be bumped by hand on every meaningful change, surfaced in four places: daemon startup logs, every `/api/status` response, the CLI `status` command's summary line, and a new badge in the dashboard header next to Server Time. "Is my latest fix actually running" is now a one-glance check instead of a remembered `grep` incantation.

### 64. Suspected IPC/Shared-Memory Leak From `--ipc=host` Under Abrupt Process Kills (V4.8.4)
* **The Trap:** Every container runs with `--ipc=host`, so Ray's shared-memory-backed plasma object store and vLLM/PyTorch's own multiprocessing shared memory live in the *host's* own SysV IPC table and `/dev/shm`, not an isolated per-container one. Combined with #62 above — the vLLM engine was never gracefully signaled — any shared memory segment not cleanly unlinked before an abrupt kill simply persisted on the host indefinitely, since SysV/POSIX shared memory isn't reclaimed automatically on process death the way ordinary process memory is. Suspected as the actual cause of at least one real deploy failure, not just a theoretical risk.
* **The Fix:** Added a final "sweeping" phase to every teardown (`sweep_ipc_orphans()`) that removes SysV shared memory segments with `nattch == 0` — a hard kernel-tracked attach count, not a heuristic, so this can never touch a segment still genuinely in use by anything on the shared host. Runs automatically inside `_execute_teardown_impl`, which every deploy's own pre-deploy teardown already calls — so this is now a guarantee on every deploy, not a manual step. Added `dgx-config ipc-inventory` (read-only) and `dgx-config sweep-ipc-orphans --dry-run` for ad-hoc inspection. Deliberately does NOT touch POSIX `/dev/shm` files yet — verifying those are truly orphaned needs a costlier cross-reference against every process's open file descriptors and memory maps, which wasn't safe to rush without testing against the real hosts.

### 63. Teardown Never Actually Reached Ray or the vLLM Engine Inside a Container (V4.8.4)
* **The Trap:** Teardown's host-level process cleanup (`ps aux | grep -E 'vllm|ray'` run directly over SSH against the bare host) had zero visibility into anything running inside a container — none of our `docker run` invocations set `--pid=host`, so every container has its own isolated PID namespace, invisible to the host's own process table. This had been true for the entire lifetime of the graceful-teardown rewrite without anyone tracing the actual namespace implications — it ran without error on every teardown, which made it look like it was doing something. Worse: even `docker stop`'s SIGTERM, which correctly reaches a container's real PID 1, only reaches `ray start --block` in a 2-node deploy — the vLLM engine itself runs as a *separate*, detached `docker exec -d` process, never a child of PID 1, so it was never signaled by anything at all. It was only ever killed via the abrupt kernel-level namespace teardown at `docker rm -f` time.
* **The Fix:** Added `_teardown_host_container_internals()`, which reaches inside each container via `docker exec` — the only mechanism that can see and signal container-internal processes without changing PID namespace sharing — to gracefully stop the vLLM engine (targeted `pkill` by process pattern, TERM then KILL) and Ray (`ray stop`, then `ray stop --force`) before the container is ever stopped or removed. The old host-level step is kept as a harmless safety net for genuinely bare-metal stray processes, not the primary mechanism it had been mistaken for.


### 62. FlashInfer SM120 Autotuner Failure on Speculative Shapes (V4.8.4)
* **The Trap:** Combining `--attention-backend B12X_ATTN` with DSpark speculative tokens caused FlashInfer's JIT autotuner (`sparse_mla_sm120_decode_dsv4`) to encounter input shapes (`(7, 32, 512)`) outside its pre-compiled tuning buckets during `_dummy_run`, causing startup timeouts and process cancellation.
* **The Fix:** Swapped to `--attention-backend FLASH_ATTN` for speculative builds, bypassing the JIT autotuning pass while maintaining reliable engine initialization.

### 61. DSpark Speculative Minimum Token Validation Failure (V4.8.4)
* **The Trap:** Attempting to lower memory consumption by reducing DSpark speculative draft tokens to 2 or 3 triggered a hard Pydantic validation failure during `SpeculativeConfig` instantiation: `Value error, DSpark requires num_speculative_tokens >= dspark_block_size (5)`.
* **The Fix:** Enforced a strict minimum of `num_speculative_tokens: 5` in recipes using DSpark. To fit within VRAM budgets under this token floor, sequence concurrency must be capped at `--max-num-seqs 1` or context length reduced.

### 60. Crashed Engine Misreported as Indefinite Warmup — Keyword Collision in Log Scanner (V4.8.4)
* **The Trap:** `detect_model_stage()` scanned container logs in reverse for progress keywords, including `"kv cache"` as a WARMUP signal. A vLLM startup crash (`ValueError: nvfp4 KV cache is not supported with MLA backends...`) contains that exact phrase inside its own error message, so the scanner matched the crash report itself as legitimate progress and reported `NOT READY - WARMUP` forever, with an ETA that counted up for over an hour with no historic data. Compounded by 2-node Ray deploys: the container's PID 1 is `ray start --block`, not the vLLM engine — the engine runs via a separate detached `docker exec -d`, so Docker correctly reports the container `RUNNING` long after the actual engine process has died. Container-level health alone can't be trusted for this launch path.
* **The Fix:** Added `_detect_crash_signature()`, which checks the log tail for an actual Python traceback *before* the keyword scan runs, and short-circuits to `CRASHED (ENGINE EXITED: <exception>)` if found — regardless of what substrings a future crash's error text happens to contain. `_finalize_host_status()` now skips ETA computation entirely for a crashed status instead of computing a countdown against a dead process.

### 59. No Mutual Exclusion Between Deploy, Teardown, and Benchmark Controls (V4.8.4)
* **The Trap:** Nothing on the dashboard stopped clicking Deploy while a teardown was mid-flight killing containers, or clicking Teardown while a deploy was mid-launch — both race against the same containers and can leave the cluster in a state neither operation intended. `CLUSTER_OP_LOCK` protected this server-side (a second call gets a "cluster busy" error), but the UI gave no indication a click would fail until the confusing error came back.
* **The Fix:** Added `applyOperationLocks()` in `index.html`, which cross-locks Deploy, Teardown, the entire deploy form (model/topology/head/user-id), and both benchmark controls whenever either deploy or teardown is in flight, driven by a local `isDeploying` flag and the polled `is_tearing_down` status field.

### 58. Teardown Became a Multi-Phase ~60s Operation With Zero Progress Visibility (V4.8.4)
* **The Trap:** After the grace-period rewrite (see #54), teardown could legitimately take up to ~60s across three phases, but the dashboard button just showed a static "Tearing down..." label with no indication of which phase it was in or whether it was progressing.
* **The Fix:** Added `TEARDOWN_STATE` (mirroring the existing `BENCHMARK_STATE` pattern), written by `_execute_teardown_impl` at each phase transition (`signaling` → `stopping` → `removing` → `done`) and surfaced via `/api/status` as `is_tearing_down`/`teardown_message`. Teardown itself stays synchronous (deploy depends on that ordering) — the dashboard's existing 4s status poll picks up live phase text concurrently with the blocking POST.

### 57. Near-Duplicate Recipe Catalog Keys — Silent Model Repoint (V4.8.4)
* **The Trap:** `recipes/local/deepseek-v4-flash-nvfp4.yaml` (an older, distinct NVIDIA build) and `recipes/local/deepseek-v4-flash-0731-nvfp4.yaml` (the correct, working auroter 0731 build) coexisted with catalog keys one keystroke apart. A prior session silently repointed the former's `hf_path` to the *same* model as the latter while also swapping in an unvalidated `--kv-cache-dtype nvfp4_ds_mla` (see #56) — with no filename change to signal any of it happened. Deploying the wrong key crashed on a config that had never actually been tested end-to-end.
* **The Fix:** Deleted `deepseek-v4-flash-nvfp4.yaml` outright rather than reverting it to its original config — the older NVIDIA build it used to serve wasn't in active use, so removing it closes both the collision risk and the invalid kv-cache-dtype landmine in one move. No structural fix for the underlying class of error yet — near-duplicate catalog keys with no loader-level collision warning remains an open gap, tracked in `ROADMAP.md`.


### 56. vLLM Rejects `nvfp4`-Family KV Cache Dtype for MLA Models (V4.8.4)
* **The Trap:** `--kv-cache-dtype nvfp4_ds_mla` — a custom identifier intended as a GB10/`flashinfer_b12x` MLA cache bypass — crashes at engine-config-creation time with `pydantic_core.ValidationError: nvfp4 KV cache is not supported with MLA (Multi-head Latent Attention) backends`. This is a vLLM core validation guard (landed alongside upstream PR #40177, "Add nvfp4 kv cache support"), not an orchestrator or image issue — confirmed the pulled `eugr/spark-vllm-b12x:latest` image digest was unchanged across the working and broken deploys.
* **The Fix:** No code fix — this is a hard vLLM constraint. DeepSeek V4 Flash (and any other MLA-architecture model) must use `--kv-cache-dtype fp8` or `auto`. Any recipe attempting an `nvfp4`-family dtype on an MLA model needs to be caught before deploy, not after — a candidate for future recipe-schema validation.

### 55. Sequential Per-Host Teardown Left Worker NCCL-Connected to a Vanished Head (V4.8.4)
* **The Trap:** The grace-period rewrite (#54) processed hosts one at a time in a `for` loop. Head could complete its entire ~20-40s graceful shutdown and be fully gone before worker's teardown cycle even began — worker spent that whole window still alive and still NCCL/Ray-connected to a rank-0 head that had already vanished mid-collective-op, observed on the dashboard as the worker going "confused" and crashing on its own well before teardown ever touched it directly.
* **The Fix:** `_execute_teardown_impl` now runs each phase (signal, stop, remove) across all target hosts *concurrently* via `WORKER_POOL`, not one host fully torn down before the next starts, so head and worker are signaled and come down together.

### 54. Teardown Hard-Kill Risked Corrupting In-Flight JIT Compiles (V4.8.4)
* **The Trap:** Teardown SIGKILL'd host processes and ran `docker rm -f` with zero grace period. JIT compilation shells out to `nvcc`/`ptxas`/`cicc` as child subprocesses that write cache artifacts non-atomically; SIGKILL on the parent doesn't propagate to those children, so a hard-kill mid-compile could leave an orphaned compiler process writing into the persistent, shared cache directory unsupervised, or leave a half-written artifact at the path the loader treats as a cache hit on the next load — silent, one-time, unpredictable recompiles with no error surfaced anywhere. Compounded by the absence of `--init` on `docker run`, meaning the container's PID 1 had no proper zombie-reaping or signal-forwarding for exactly this kind of subprocess tree.
* **The Fix:** Added `TEARDOWN_GRACE_SEC` (20s): host processes now get SIGTERM and a real grace period before escalating to `-9`, and `docker stop --time N` runs before `docker rm -f` rather than skipping straight to it. Added `--init` to both the 1-node and 2-node `docker run` construction paths.

### 53. `historical_tps` Never Resolved — Ledger Key Mismatch (V4.8.4)
* **The Trap:** `enrich_catalog()` looked up `ledger_tps.get(m_key, "N/A")` where `m_key` is the catalog/recipe key, but `benchmark.py` logged the raw served HF model basename into the ledger's `Model` column — two different string spaces that never matched. `historical_tps` was silently `N/A` for every model, always.
* **The Fix:** Added `--model-key` to `benchmark.py`, threaded through from both the deploy-triggered and dashboard-triggered benchmark paths, so the ledger logs the catalog key directly. Old ledger rows predate this and won't retroactively match; `benchmark_ledger.csv` was rotated rather than backfilled.

### 52. Benchmark TTFT/Decode Speed Reported Zero for Reasoning Models (V4.8.4)
* **The Trap:** `run_benchmark_pass()` only started the TTFT clock and counted tokens when a stream chunk's `delta.content` was truthy. DeepSeek V4 (run with `--reasoning-parser deepseek_v4`) streams its initial tokens into `delta.reasoning_content` instead — for a reasoning-heavy response, `first_token_time` never fired and `decode_tps` reported `0.0`.
* **The Fix:** `run_benchmark_pass()` now starts the clock on either `content` or `reasoning_content`.

### 51. JIT Cache Pruning Deleted Individual Files, Risking Half-Written Cache Entries (V4.8.4)
* **The Trap:** `prune_cluster_cache()` ran `find ~/.cache/{tilelang,deepgemm,triton} -type f -atime +N -delete`. A Triton/TileLang cache entry is a *directory* of co-dependent artifacts (metadata JSON + compiled binary); deleting files piecemeal can leave a half-entry that the loader treats as a hit and then fails to load — corruption, not a clean miss. Also relied on `atime`, which some filesystem mount options don't reliably update.
* **The Fix:** Replaced with a remote Python script that evicts whole entry *directories*, strictly oldest-first (`max(atime, mtime)` per entry, degrading gracefully to mtime-only), only when a host is below `--min-free-gb`. Added a fully read-only `cache-inventory` command (entry counts, sizes, LRU order, mount options) safe to run against production at any time, and `--dry-run` on the prune path itself.

### 50. Orchestrator Daemon Self-Destruction via Broad Teardown `pkill` & Missing Lock Wrapper (V4.8.3)
* **The Trap:** Triggering a deployment executed `_execute_teardown_impl()`, which ran `sudo pkill -9 -f 'vllm|ray|python3'`. Because `dgx-orchestrator` runs as a `python3` process on `spark-4`, it killed its own HTTP server mid-request, causing browser deployment calls to fail with `Failed to fetch`. Additionally, `execute_deployment()` was missing its `CLUSTER_OP_LOCK` wrapper definition during refactoring, raising a backend `NameError`.
* **The Fix:** Re-implemented `execute_deployment()` with `CLUSTER_OP_LOCK` enforcement, and updated host teardown commands to explicitly filter out `dgx-orchestrator` PIDs (`grep -v 'dgx-orchestrator'`) before issuing process kill signals.

### 49. Speculative Metric Key Mismatch & Strict Recipe MTP Checks (V4.8.3)
* **The Trap:** vLLM and DeepSeek-V4 speculative models expose draft/accepted token counters under `vllm:spec_decode_num_draft_tokens_total` and `vllm:spec_decode_num_accepted_tokens_total`. The scraper was listening for legacy `vllm:num_spec_tokens_*` keys, resulting in zeroed draft stats. Furthermore, `enrich_catalog()` strictly checked CLI flags for `mtp_enabled`, hiding speculative UI metrics for models using integrated draft heads or alternative flags like `--speculative-config`.
* **The Fix:** Updated `get_vllm_metrics()` to parse `vllm:spec_decode_num_*` Prometheus metrics, and expanded `enrich_catalog()` to check case-insensitively for speculative keywords (`speculative`, `mtp`, `draft`, `nextn`, `proposal`) or historical ledger activity.

### 48. In-RAM Telemetry Loss on Daemon Shutdown (V4.8.3)
* **The Trap:** Real-time token counts, session durations, and MTP (Multi-Token Prediction) hit rates were accumulated in memory to protect host NVMe drives from continuous disk writes. Unplanned daemon restarts, container updates, or host reboots wiped uncommitted session analytics.
* **The Fix:** Implemented a 1-hour periodic delta-checkpoint flush in `SessionTracker` and attached OS signal traps (`SIGTERM` / `SIGINT`) to `dgx-orchestrator.py` to force stateful commits to `model_ledger.json` before process termination.

### 47. Idle-Time Metric Pollution in Long-Running Sessions (V4.8.3)
* **The Trap:** Calculating Tokens Per Second (TPS) across a continuous session by subtracting session start time from session end time caused long idle periods (e.g., a 600-second quiet wait before closing a session) to dilute high-speed token generation bursts into artificially low averages.
* **The Fix:** Decoupled the 10-minute idle tripwire from the active compute timer. Active TPS is calculated strictly as `Total_Generated_Tokens / (Last_Active_TS - First_Active_TS)`. The 600-second idle period triggers session commits to `model_ledger.json` but is completely excluded from the time divisor.

### 46. Wrapped `bash -c` Shell Command Inspection Failure (V4.8.3)
* **The Trap:** Docker container inspection on Ray head nodes returned array-wrapped shell strings (e.g., `["bash", "-c", "ray start ... && python3 -m vllm ... --model <path>"]`). The inspection parser attempted to find `--model` as a discrete array element, threw an exception, and defaulted to labeling the active container as `"Active Container"`, preventing catalog matching.
* **The Fix:** Upgraded `_discover_host_container()` to execute a regex pattern (`--model\s+([^\s]+)`) across string-wrapped entrypoint commands to extract the model path regardless of shell layering.

### 45. Multi-Node Asymmetric Logging & Worker UI State Desync (V4.8.3)
* **The Trap:** In multi-node Ray deployments, the Head node (`spark-4`) acts as driver and logs shard-loading progress to Docker `stdout`. The Worker node (`spark-3`) only runs a passive `ray start` process and receives weights into VRAM over NCCL without outputting log progress. The orchestrator's log stage detector left `spark-3` permanently stuck on `Active Container` / `INITIALIZING` with a generic fallback ETA.
* **The Fix:** Updated `_compute_cluster_status_impl()` to treat active worker nodes as UI slaves to the head node during boot sequences—broadcasting the head node's detected model name, stage (`LOADING SHARDS`, `COMPILING KERNELS`), and active ETA across all worker UI cards simultaneously.

### 44. Grace Blackwell (GB10) Power Limit String Parsing (`PWR: 6/N/AW`) (V4.8.3)
* **The Trap:** On Grace Blackwell (GB10) unified superchips, power is dynamically managed across the package. `nvidia-smi` returns `N/A` or `[Not Supported]` for GPU-isolated power limits. The UI blindly concatenated the draw, limit, and unit strings, rendering `PWR: 6/N/AW`.
* **The Fix:** Sanitized telemetry parsing in `dgx-orchestrator.py` and string construction in `index.html`. If the hardware driver returns `N/A` for the limit, the dashboard gracefully falls back to displaying active draw only (`PWR: 6W`).

### 43. Multi-Node V1 Shared Memory (`/dev/shm`) Followers Crash (V4.8.1)
* **The Trap:** Attempting to deploy multi-node models using `--distributed-executor-backend mp` on the vLLM V1 engine triggered `AssertionError: collective_rpc should not be called on follower node` on `spark-3`. The `mp` backend relies on host IPC shared memory (`/dev/shm`), which cannot cross physical nodes.
* **The Fix:** Multi-node topologies across physical hosts must use `--distributed-executor-backend ray` and set `VLLM_USE_V1=0` in `env_vars` to force the V0 cross-host Ray executor.

### 42. YAML Folded Scalar Comment Pollution in `vllm_args` (V4.8.1)
* **The Trap:** Inlining bash comments (`#`) inside a YAML folded block scalar (`vllm_args: >-`) resulted in all newlines being flattened into a single space. `shlex.split()` parsed the comments as literal CLI arguments, causing `api_server.py: error: unrecognized arguments: # ...`.
* **The Fix:** Shifted all operational notes and comments strictly outside of the `vllm_args: >-` string scalar.

### 41. Recipe Catalog Empty Due to Filename/`name:` Field Drift (V4.8.0)
* **The Trap:** The original recipe schema carried both a filename and an internal `name:` field, required to match. During a merge, two recipes' `name:` fields drifted out of sync with their filenames. Because `build_catalog_response()` fails closed on any single bad recipe, the entire model catalog silently went empty — not just the two broken files — with no error surfaced anywhere the dashboard user could see.
* **The Fix:** Removed `name:` from the schema entirely. The filename is now the only identifier a recipe has, so there's structurally nothing left for it to disagree with. The whole-catalog-fails-on-one-bad-recipe behavior itself is unchanged and is tracked separately.

### 40. Dashboard Polling Hang & Duplicate Load-Time Recording (V4.8.0)
* **The Trap:** Two separate bugs, both invisible under light use. (1) `get_cluster_status()` made several sequential SSH round trips per host with no overall deadline; under frequent dashboard polling, a single unreachable host could cause requests to back up faster than they drained, presenting as a fully hung dashboard. (2) `record_load_time()` was called on every single status poll while a container sat idle-but-ready, not just once at actual readiness — `load_times.json` entries grew without bound instead of capturing one real cold-start duration, silently corrupting the ETA estimator's historical data.
* **The Fix:** `get_cluster_status()` now single-flights concurrent callers and enforces a hard wall-clock ceiling (`STATUS_CALL_TIMEOUT_SEC`) via bounded per-host futures instead of unbounded `as_completed()`. `record_load_time()` now tracks which container instance has already been recorded and fires at most once per instance.

### 39. Docker Control Plane & Delegate Wrapper (V4.7.0)
* **The Trap:** Host-level PEP 668 constraints and local `venv` drift on `codepolice` made cross-environment management fragile and difficult to upgrade.
* **The Fix:** Containerized the full control plane stack inside a `python:3.12-slim` Docker image on `maestro`. Re-engineered the `dgx-config` CLI wrapper into a context-aware Docker delegate that transparently forwards TTY flags, injects host identity, auto-stages external key files, and handles socket cleanup directly inside the container namespace.

### 38. Grace Blackwell (GB10) MXFP4 MoE Engine & Activation Patch (V4.6.3)
* **The Trap:** On Grace Blackwell (GB10) GPUs under vLLM `0.21.0`, TRTLLM, DeepGEMM, and Triton MXFP4 MoE kernels fail device compatibility checks. Marlin fails with a `KeyError: 'layers.0.ffn.experts.w13_input_scale'` on raw HuggingFace safetensors. `FlashInferExperts` (`--moe-backend flashinfer_cutlass`) is the only valid GB10 kernel, but defaults to `FLASHINFER_CUTLASS_MXFP4_BF16` (BF16 activations) while DeepSeek-V4 requires FP8 activations (`FLASHINFER_CUTLASS_MXFP4_MXFP8`). Passing `flashinfer_cutlass_afp8` is rejected by vLLM's CLI parser.
* **The Fix:** Configured the recipe to use `--moe-backend flashinfer_cutlass` and injected a container entrypoint `sed` patch (`sed -i "s/FLASHINFER_CUTLASS_MXFP4_BF16/FLASHINFER_CUTLASS_MXFP4_MXFP8/g" ...`) to force the FP8 activation Cutlass engine at runtime.

### 37. Multi-Node vLLM V1 Engine `--nnodes` & `--node-rank` Flags (V4.6.3)
* **The Trap:** Multi-node container launches using vLLM V1 (`nvcr.io/nvidia/vllm:26.05.post1-py3`) omitted `--nnodes` and `--node-rank`. V1's `multiproc_executor` defaulted to single-host execution and attempted to allocate all pipeline-parallel ranks onto `spark-4`'s single physical GPU, triggering a `local_world_size <= visible_device_count` crash.
* **The Fix:** Updated `execute_deployment()` in `dgx-orchestrator.py` to inject `--nnodes <nodes>` and `--node-rank 0/1` explicitly into `docker run` commands.

### 36. HuggingFace Auth Token Discovery (`get_hf_token`) (V4.6.3)
* **The Trap:** Unauthenticated HuggingFace Hub requests triggered rate-limiting warnings or failed private checkpoint downloads.
* **The Fix:** Implemented `get_hf_token()` in `common/ssh.py` to search environment variables, the project's `.secrets` file, or `~/.cache/huggingface/token`, injecting `-e HF_TOKEN=<token>` directly into vLLM containers.

### 35. SSH Multi-Token Argument Quoting via `shlex.quote` (V4.6.3)
* **The Trap:** Passing complex CLI arguments or JSON configuration strings (`--attention-config '{"use_fp4_indexer_cache": true}'`) through `run_ssh()` allowed remote shell layers to strip inner quotes or mangle JSON formatting.
* **The Fix:** Updated `run_ssh()` to process command lists using `shlex.quote` on each token, ensuring preserved remote evaluation across OpenSSH shell boundaries.

### 34. Web Dashboard Dynamic Hostname Routing (V4.6.2)
* **The Trap:** Hardcoding `10.0.14.43` or `localhost` as `API_BASE` in `index.html` caused cross-origin requests or connection failures when opening the dashboard from external workstations.
* **The Fix:** Updated `index.html` to evaluate `window.location.hostname` dynamically.

### 33. Grace Blackwell (GB10) Unified Memory Telemetry Parser (V4.6.2)
* **The Trap:** Grace Blackwell LPDDR5x Unified Memory returns `[N/A]` for standard `nvidia-smi` memory queries, causing strict integer parsing checks to reject telemetry lines and return empty metrics.
* **The Fix:** Updated `get_lightweight_telemetry()` in `dgx-orchestrator.py` to parse temperature and GPU utilization independently while assigning `Unified / 131072 MB` for VRAM fields.

### 32. SSH Remote Shell Pipeline Syntax Expansion Bug (V4.6.2)
* **The Trap:** Executing `docker ps --format '{{.Names}}|{{.Image}}'` over SSH caused the remote Bash shell to interpret `|` as a shell pipe, throwing `Exit 2` or `Exit 127` errors.
* **The Fix:** Replaced `|` delimiters with double colons (`::`) across remote Docker format strings.

### 31. Wrapper Subcommand Passthrough for Daemon Execution (V4.6.1)
* **The Trap:** Running `dgx-config daemon` caused the wrapper to pass `cli daemon` to `dgx-orchestrator.py`, triggering `argparse` choice validation errors.
* **The Fix:** Updated `dgx-orchestrator.py` to map `args.subcommand == "cli"` and `args.cli_action == "daemon"` directly into daemon execution mode.

### 30. Virtual Environment Bootstrap & Ownership (V4.6.1)
* **The Trap:** Running `pip install` as a non-service user on the host failed with permission errors.
* **The Fix:** Standardized on the containerized stack (see Tombstone #39) rather than a host-level `venv`, sidestepping this class of problem entirely.

### 29. Web Dashboard Full-Width Logs & Dynamic Topology Selector (V4.6.0)
* **The Trap:** Squeezed log panels obscured long trace outputs, and invalid topology options allowed users to attempt single-node deployments of 70B+ models.
* **The Fix:** Moved live logs to a full-width bottom panel and added dynamic catalog parsing in JavaScript to filter valid topology choices.

### 28. YAML Argument Comment Pollution & Syntax Sanitization (V4.6.0)
* **The Trap:** Inline bash comments inside folded YAML multiline strings (`>-`) were parsed as literal command-line flags, causing vLLM initialization to fail.
* **The Fix:** Stripped all inline comments and trailing formatting artifacts from the catalog source.

### 27. Multi-User Shared Key Auto-Staging & OpenSSH 0600 Strictness (V4.5.0)
* **The Trap:** Group-readable permissions (`0640`) on shared SSH keys triggered OpenSSH `bad permissions` rejections.
* **The Fix:** Implemented `resolve_user_identity_key()` to auto-stage key copies into `~/.ssh/id_dgx_orchestrator` with `0600` permissions.
