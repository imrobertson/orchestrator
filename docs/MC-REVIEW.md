# Task MC — Deploy-path integration — Review

> **Revision note (after initial delivery):** The person supplied the real
> `pydantic`/`pydantic_core`/`pyyaml`/`typing_extensions`/
> `typing_inspection`/`annotated_types` wheels this repo's `common/recipes.py`
> actually depends on. The original "No hardware" section below was run
> against hand-rolled Python stand-ins for `common/recipes.py` and
> `common/mods.py` (no pydantic available at the time), not the real
> modules. That section has been replaced with a second, materially
> stronger pass: the REAL `common/recipes.py` (genuine pydantic
> `RecipeConfig` validation, real YAML parsing), the REAL `common/mods.py`,
> and the REAL `common/ssh.py` are now imported and exercised unmodified —
> only `common/config.py` and `common/constants.py` are still faked, since
> neither was ever supplied to this task. Nothing in the original run's
> pass/fail *outcome* changed (43/43 passed before, 41/41 pass now — a
> couple of harness-only checks were consolidated), but the earlier run's
> claim of "no hardware" coverage overstated how much of the real codebase
> it actually touched, and this correction is worth recording rather than
> quietly swapping the numbers. A deliverable smoke test script,
> `smoke_test_mc.py`, is now included per the person's request.

## Status

**Complete.** Verified against a scripted/mocked transport only (no real
hardware in this session — no live `spark-3`/`spark-4` SSH access was
available). The two "real deploy" verification items in the task prompt
(one live 1-node deploy, one live 2-node deploy of a known-good `mods: []`
recipe) are **not yet done** and need to be run against the actual cluster
before this is fully signed off per the task's own verification section.

## What was built

`dgx-orchestrator.py`'s `_execute_deployment_impl()` now resolves each
target host's image tag through one new shared function,
`_resolve_host_image_tag(host, ip, base_image, mod_names, dry_run)`
(module-level, defined immediately above `_execute_deployment_impl()`).
It is called once per host from both the 1-node branch and the 2-node
loop, immediately before that host's `docker_cmd` is assembled:

- `dry_run=True` → calls MB's `common.mods.resolve_mod_tag()` (pure,
  no SSH).
- `dry_run=False` → calls MB's `common.mods.ensure_mods_baked()` (bakes
  on that host first if the tag isn't already there).

`_execute_deployment_impl()` itself gained: a `mod_names` lookup (via
`load_recipes().get(model).mods`, see "Contradictions" below for why not
from the catalog dict), a `mods_report` dict populated only for hosts with
a non-empty mod set, per-host `host_image_tag` substituted into
`docker_cmd` in place of the old shared `image_tag`, and a `mods` key
conditionally added to the `--dry-run` response.

No other file was modified. `common/mods.py`, `common/recipes.py`, and
`common/ssh.py` are byte-for-byte what was uploaded (diffed to confirm,
see "Scope check").

## What was verified, and how

### No hardware

`smoke_test_mc.py` (delivered alongside this review) imports the REAL
`dgx-orchestrator.py`, the REAL `common/recipes.py` (genuine pydantic
`RecipeConfig`/`TopologyConfig` validation against real YAML fixture
files, not a schema stand-in), and the REAL `common/mods.py` and
`common/ssh.py`, all unmodified. Only `common/config.py` and
`common/constants.py` are faked — neither was ever part of any of
MA/MB/MC's deliverables, so there was no real version to use. The mocking
boundary was pushed one layer deeper than the first pass of this review:
rather than replacing `common.ssh.run_ssh()` wholesale, the script patches
`subprocess.Popen` inside the real `common.ssh` module (so `run_ssh()`'s
own argument construction — `ControlMaster`/`ControlPersist` flags,
`ConnectTimeout`, capture handling, the quoted-remote-command assembly —
runs for real and is parsed back out of the fake `Popen`'s constructor
args) and `subprocess.run` inside the real `common.mods` module (narrowly,
only for `cmd[0] == "scp"`, since MB's `_scp_to_host()` calls
`subprocess.run` directly rather than through `run_ssh()`). A small
in-memory Docker-state double tracks `docker commit <tag>` calls so a
subsequent `docker image inspect <tag>` — MB's own post-bake verification
step — round-trips correctly instead of always failing.

41 checks, all passing on the final run, using real YAML recipe fixtures
(`recipes/local/test-model-{nomods,mods,badmod}.yaml`, each a genuine
`RecipeConfig`-validated file) and a real `models.yaml` for the
`USE_LEGACY_CATALOG=1` case. I also deliberately sabotaged the delivered
`dgx-orchestrator.py` (reverted the 1-node branch's `host_image_tag`
substitution back to `image_tag`) and re-ran the script against that
broken copy to confirm the harness actually catches a real regression
rather than rubber-stamping — it correctly failed 2 of 41 checks
(`docker run uses derived tag, not base` and the corresponding
`docker_run_commands` check). That sabotage run is not part of the
delivered script; it was a one-off check that the checks themselves have
teeth.

Concretely verified:

1. **`--dry-run`, `mods: []`, 1-node and 2-node** — zero entries in the
   SSH call log (`len(fake_ssh.CALLS) == 0`) in both topologies, and the
   returned dict's key set is exactly `{status, message, targets, head,
   docker_run_commands}` — no `mods` key present at all. `docker_cmd`'s
   image argument is the literal base image string
   `eugr/spark-vllm-b12x:latest`, unchanged.
2. **`--dry-run`, `mods: ["fake-mod"]`** — still zero SSH calls (confirms
   `resolve_mod_tag()` is genuinely pure/local). Response gained a `mods`
   key: `res4["mods"]["spark-3"]["resolved_tag"]` returned
   `eugr/spark-vllm-b12x:latest-mods-ce6df599c79a36c5`, and that exact
   string was also found as the image argument inside
   `docker_run_commands["spark-3"]`.
3. **Resolution failure (`mods: ["missing-mod"]`, directory doesn't
   exist)** — `status: "error"` in all four combinations tested
   (dry-run×1-node, live×1-node, live×2-node — dry-run×2-node was covered
   implicitly by the dry-run/1-node case sharing the same pure
   `resolve_mod_tag()` call), and in the two live cases, **zero** `docker
   run` calls appear anywhere in the SSH call log — confirmed by filtering
   `fake_ssh.CALLS` for `cmd[:2] == ["docker", "run"]` and asserting the
   filtered list is empty, not just checking the return status.
4. **Live deploy, `mods: []`, 1-node and 2-node** — exactly 1 and 2
   `docker run` calls respectively, both using the unmodified base image,
   and **zero** `docker image inspect` calls anywhere in the log (that
   call is only ever made by `ensure_mods_baked()` when `mod_names` is
   non-empty — its absence is the strongest available proxy for "zero
   extra SSH round trips" without live hardware to count real
   round-trip latency against).
5. **Live deploy, `mods: ["fake-mod"]`, 1-node** — exactly one `docker
   commit` (the bake happened), exactly one `docker run`, and that
   `docker run`'s image argument contains `-mods-` and does **not**
   equal the raw base image tag.
6. **Live deploy, `mods: ["fake-mod"]`, tag pre-seeded as already present
   on the host** — exactly one `docker image inspect` call and **zero**
   `docker commit` calls, confirming `ensure_mods_baked()`'s documented
   idempotency (no re-bake) reached through the new integration point
   unmodified.
7. **Live deploy, `mods: ["fake-mod"]`, 2-node** — exactly two `docker
   commit` calls, on the two different host IPs (`100.64.0.3` and
   `100.64.0.4`), both producing the **same** derived tag (confirms
   constraint 2 from `mods.py`'s docstring — independent per-host bakes of
   the same mod set converge on identical tags), and both `docker run`
   calls use that tag.

**What this harness cannot cover:** real SSH/network latency or failure
modes (a genuinely dead host, a real `ConnectTimeout`, a real
`ControlMaster` reuse), real Docker daemon behavior (actual layer
commits, actual `docker image inspect` output shape beyond what the
double fakes), real `scp` behavior for shipping a mod payload, GPU
scheduling/`nvidia-smi` interaction, and the Ray-cluster-registration
polling loop's real timing in the 2-node non-Ray-vs-Ray branches. It also
cannot confirm the literal "zero extra SSH round trips" wall-clock claim
for the empty-mods case beyond the proxy of "no mod-related SSH commands
appear in the log" — a real host round-trip count would need a live
capture.

**A check that initially gave a false result:** Test 7 (live deploy with
a non-empty mod set) first failed with a `ModBakeError` from inside MB's
own code — `docker commit ... reported success but docker image inspect
... fails on spark-3 immediately afterward`. This was not a bug in the
integration; it was the test double being too literal: my first version
of the fake `run_ssh` always failed `docker image inspect <tag>` for any
tag not explicitly pre-seeded, so it correctly-but-uselessly triggered
MB's own designed safety check (the post-commit verification `mods.py`'s
docstring explains at length) every single time a bake happened, since
nothing ever marked a freshly-committed tag as now "existing." Fixed by
having the fake `run_ssh` record tags from `docker commit <container>
<tag>` calls (tag is the real Docker CLI's last positional argument) into
an in-memory `BAKED_TAGS` set per host IP, and checking that set (union
with `PRESEEDED_TAGS`) on `docker image inspect`. After that fix, all
33 (later 43) checks passed. Reporting this because it is exactly the
kind of thing that would look like "MB's bake code is broken" to a
future reader skimming a partial run of this harness, when the actual
defect was in the harness's simulated Docker state, not in `mods.py` or
in this task's integration code. `smoke_test_mc.py`'s `Transport` double
carries this same commit→inspect tracking forward from the start, so the
rewritten pydantic-backed run didn't reproduce the false result — but the
fix is only in the harness there because this earlier debugging session
already found and named the failure mode.

**Also confirmed in the rewritten pass:** deliberately reverting one line
of the delivered `dgx-orchestrator.py` (the 1-node branch's
`host_image_tag` substitution, back to the old `image_tag`) and re-running
`smoke_test_mc.py` against that sabotaged copy correctly failed 2 of 41
checks rather than silently passing — see `smoke_test_mc.py`'s own "no
hardware" coverage note in its docstring, and the log of that one-off
sabotage run kept for this review: `docker run uses derived tag, not
base: FAIL` and the matching `docker_run_commands` check. This isn't
itself part of the delivered script (it's not something `smoke_test_mc.py`
runs on its own), but it's the honest answer to "does this test suite
actually have teeth," which felt worth checking given the last revision's
correction was specifically about the *first* harness overstating its own
coverage.

### Live hardware

**None.** No SSH/Tailscale access to `spark-3`/`spark-4` was available in
this session. The task's own verification section calls for "One real
1-node deploy and one real 2-node deploy of a known-good recipe with no
mods, confirming no behaviour change" — that has not been done. Given the
scripted-harness result showing byte-identical `--dry-run` output and
zero extra SSH traffic for `mods: []`, I'd expect these to pass cleanly,
but that is a prediction, not a verification, and should not be treated
as equivalent to the real thing.

## Contradictions and things the plan didn't specify

1. **`mods` is not in the catalog dict, so `model_config` can't supply
   `mod_names`.** `_execute_deployment_impl()` gets its recipe data from
   `load_model_catalog()` → `models_catalog[model]`, which for the
   non-legacy path is `build_catalog_response()`'s output. That function
   (in the uploaded `recipes.py`, Task MA/MB's own work) deliberately
   does **not** put `mods` into `model_entry` — it stays inert/hidden
   from the catalog response by design (see that module's docstring: "no
   equivalent in the old format... deliberately excluded"). A literal
   reading of "resolves the recipe's mod set" using only what's already
   being read out of `model_config` in this function would have silently
   resolved every recipe's mod set to `[]` regardless of what's actually
   declared in the YAML — passing every stated requirement (empty-set
   no-op, dry-run byte-identical, resolution-failure-aborts) trivially
   and uselessly, because `mod_names` would never be anything but `[]`.
   I caught this before writing any code by reading `recipes.py` in full
   rather than skimming for the shape of `model_config`. Fix: read
   `mod_names` from the raw `RecipeConfig` via `load_recipes().get(model)`
   instead, which is already imported in `dgx-orchestrator.py` and is the
   one place `.mods` actually lives.

2. **`load_recipes()` is called unconditionally, independent of
   `USE_LEGACY_CATALOG`.** Under `USE_LEGACY_CATALOG=1`, the *catalog*
   comes from `models.yaml`, not `recipes/{local,eugr}/`. But my mod
   lookup calls `load_recipes()` regardless of that flag. For the current
   repo state this is harmless (no `recipes/*.yaml` file declares a
   non-empty `mods:` list yet, per Task MA's "every existing recipe has
   `mods: []`"). But it means: if a model name happens to exist in *both*
   `models.yaml` (legacy catalog) *and* as a same-stem file under
   `recipes/local/` or `recipes/eugr/` with a non-empty `mods:` list, a
   legacy-mode deploy of that model would silently pick up mods from the
   unrelated recipe file it isn't even using for its own config. The task
   didn't specify which catalog source mods should be scoped to when the
   two rollback modes disagree, and this wasn't something I could resolve
   without a product decision (gating on `USE_LEGACY_CATALOG` adds a
   second special-case branch for a lever that's meant to be temporary/
   rare). I left it as-is and am flagging it here and in `TOMBSTONES.md`
   rather than silently deciding a scope question that wasn't posed to
   me.

3. **A recipe-load failure while resolving mods must not break unrelated
   deploys.** Not explicit in the task prompt, but load-bearing: if
   `load_recipes()` raised (e.g. some *other*, unrelated recipe file
   under `recipes/` is malformed), and I let that exception propagate
   from inside `_execute_deployment_impl()`, a completely unrelated
   model's deploy — including one already running fine today with
   `mods: []` — would start failing the moment anyone commits a broken
   recipe file anywhere in the repo. That's exactly the "one bad file
   takes down the whole catalog" failure class `recipes.py`'s own module
   docstring already describes and warns about for a different reason
   (the old name/filename mismatch). I wrapped the `load_recipes()` call
   in a `try/except`, log-and-fall-back-to-`mod_names = []` rather than
   raising, so an unrelated bad recipe file degrades this deploy to
   "mods not applied" instead of "deploy fails outright." This is a
   judgment call, not something the task specified; it seemed like the
   only choice consistent with "empty mod set is a strict no-op" being a
   *safe fallback*, not just the default-recipe-YAML case.

4. **Where "report the resolved tag and what would be baked" should live
   in the dry-run response.** The task doesn't specify a response shape
   for this. The resolved tag is already visible for free (it's the image
   argument embedded in `docker_run_commands`), so strictly speaking
   nothing extra was required to satisfy that half of the sentence. I
   added a conditional top-level `mods` key (only present when at least
   one host has a non-empty mod set) to make "what would be baked" — the
   mod names themselves, not just the resulting tag — explicit rather
   than something a caller has to reverse-engineer from a hash suffix.
   Since this key is entirely absent for every recipe today, it can't
   violate "byte-identical output," but a strict reading of "byte-
   identical" might have argued for not touching the response shape *at
   all*, even conditionally. I judged the conditional-key approach safer
   than leaving "what would be baked" fully implicit, but this is a
   design choice the task left open, not something dictated by it.

5. **Bake-failure partial-deploy semantics on 2-node are unchanged, not
   newly introduced.** The task says "resolution failure aborts the
   deploy before any container starts" but doesn't address *bake*
   failure (a real SSH/Docker error on one host, as opposed to a missing
   mod directory) occurring on the *second* host of a 2-node deploy after
   the *first* host's container has already started. I did not add any
   new rollback behavior for this case — it now fails exactly the way a
   plain `docker run` failure on the second host already failed before
   this task (return an error dict, leave whatever already started
   running). I'm calling this out explicitly rather than silently
   assuming it's fine: it seemed like the correct reading given the task
   distinguishes "resolution failure" (pure, host-independent, genuinely
   preventable pre-container) from bake (host-scoped, inherently
   real-world-fallible, same risk class as `docker run` itself), but a
   different implementer might have read "aborts the deploy before any
   container starts" more broadly and demanded a pre-flight bake-on-every-
   host pass before touching any host's `docker run` — which the task's
   own "bake happens per target host, before that host's docker run"
   line seems to explicitly rule out (that phrasing describes an
   interleaved per-host bake-then-run, not a bake-all-then-run-all
   ordering).

## Scope check

Nothing from the "note on scope" was built: no runtime mod *application*
beyond what MB already implemented (this task only wires MB's existing
bake/resolve into the deploy path — it doesn't touch `run.sh` execution
semantics), no `phase:` field, no mod distribution via a registry.

**Only `dgx-orchestrator.py` was touched.** `common/mods.py`,
`common/recipes.py`, and `common/ssh.py` (all supplied as attachments)
are unmodified — confirmed via `diff` against the uploaded originals
before writing this doc; all three diffs are empty. `TOMBSTONES.md` was
**not** touched — it wasn't provided as an uploaded file, so a
new-tombstone entry is included below in this review and as a separate
`TOMBSTONE-ENTRY-83-ADDENDUM.md` file for you to merge into the real
file, rather than my fabricating or guessing at the rest of that file's
contents.

The one item outside a literal reading of "wire it into
`_execute_deployment_impl()`": adding the `mods` key to the `--dry-run`
response dict (item 4 above) is a small response-shape addition beyond
the literal minimum. I judged it in-scope (it directly answers "report
... what would be baked") but am calling it out here per the review
format's instruction to flag anything beyond a strict reading.

**`smoke_test_mc.py`** (added in this revision, at the person's explicit
request for a smoke test script) is a new deliverable, not a change to
any of MA/MB/MC's existing files — it's a standalone verification tool
that imports the delivered `dgx-orchestrator.py` plus a `common/` dir the
person points it at. It is not part of the `_execute_deployment_impl()`
integration itself and doesn't touch `TOMBSTONES.md`-worthy repo
behavior on its own; noting it here only because "what changed" should
include every file this revision added, not just code changes to the
deploy path.

## Changed files, in full

`dgx-orchestrator.py` is the only file changed from the original task.
It is long (3,973 lines) — full contents attached as
`dgx-orchestrator.py` alongside this review rather than pasted inline
here, per repo convention (working files staged to `/home/claude/`,
delivered via `present_files`). The diff against the uploaded original is
reproduced below for review convenience; the attached file is the
authoritative full copy.

`smoke_test_mc.py` (new in this revision) is also attached in full,
separately — it's a new standalone file, not an edit to an existing one,
so there's no diff to show; see the file itself.

```diff
--- dgx-orchestrator.py (uploaded)
+++ dgx-orchestrator.py (this task's output)
@@ -44,6 +44,7 @@
 from common.config import legacy_hosts_dict, load_cluster_config
 from common.constants import ContainerRole
+from common.mods import ModBakeError, ModResolutionError, ensure_mods_baked, resolve_mod_tag
 from common.recipes import build_catalog_response, compute_config_hash, load_recipes
 from common.ssh import get_hf_token, resolve_user_identity_key, run_ssh

@@ -2962,9 +2963,56 @@
         results[host] = "Authorized" if res.returncode == 0 else f"Failed: {res.stderr.strip()}"
     return {"status": "success", "details": results}

+def _resolve_host_image_tag(host: str, ip: str, base_image: str, mod_names: list, dry_run: bool) -> str:
+    """ ... (see attached file for full docstring) ... """
+    if dry_run:
+        return resolve_mod_tag(base_image, mod_names)
+    return ensure_mods_baked(host, ip, base_image, mod_names)
+
+
 def _execute_deployment_impl(model: str, nodes: int, head: str, user_id: str, wait: bool = False, run_benchmark: bool = False, dry_run: bool = False) -> dict:
     deploy_start_time = time.time()
     docker_run_commands: dict = {}
+    mods_report: dict = {}

     if nodes not in (1, 2): return {"status": "error", "message": f"Invalid 'nodes' value {nodes!r}: must be 1 or 2."}
     if head not in HOSTS: return {"status": "error", "message": f"Invalid 'head' value {head!r}: must be one of {list(HOSTS.keys())}."}
@@ -3040,6 +3088,29 @@
     image_tag = model_config.get("image", default_img)
     compat_mount = "/dev/null:/etc/ld.so.conf.d/00-cuda-compat.conf"

+    mod_names: list = []
+    try:
+        recipe_obj = load_recipes().get(model)
+        if recipe_obj is not None:
+            mod_names = list(recipe_obj.mods)
+    except Exception as exc:
+        print(f"[!] _execute_deployment_impl({model}): failed to load recipe for mod resolution "
+              f"({type(exc).__name__}: {exc}); proceeding with no mods.")
+
     def _jit_cache_mounts_and_env(vol_mount: str, log_subdir: str) -> tuple[list[str], list[str]]:
         ...
@@ -3101,6 +3172,17 @@
             "--max-model-len", str(max_model_len)
         ] + vllm_args_list

+        try:
+            host_image_tag = _resolve_host_image_tag(head, ip, image_tag, mod_names, dry_run)
+        except (ModResolutionError, ModBakeError) as exc:
+            return {"status": "error", "message": f"Mod resolution failed for {model} on {head}: {exc}"}
+        if mod_names:
+            mods_report[head] = {"base_image": image_tag, "mod_names": mod_names, "resolved_tag": host_image_tag}
+
         docker_cmd = [
             "docker", "run", "-d", "--init",
             "--name", ContainerRole.STANDALONE,
@@ -3108,7 +3190,7 @@
             "--gpus", "all",
             "-v", vol_mount,
             "-v", compat_mount
-        ] + jit_mounts + env_flags + [image_tag] + container_args
+        ] + jit_mounts + env_flags + [host_image_tag] + container_args

         res = None if dry_run else run_ssh(ip, None, docker_cmd, timeout=60)
         if dry_run: docker_run_commands[head] = docker_cmd
@@ -3182,6 +3264,23 @@
             else:
                 entrypoint_cmd = container_args

+            try:
+                host_image_tag = _resolve_host_image_tag(host, ip, image_tag, mod_names, dry_run)
+            except (ModResolutionError, ModBakeError) as exc:
+                return {"status": "error", "message": f"Mod resolution failed for {model} on {host}: {exc}"}
+            if mod_names:
+                mods_report[host] = {"base_image": image_tag, "mod_names": mod_names, "resolved_tag": host_image_tag}
+
             docker_cmd = [
                 "docker", "run", "-d", "--init",
                 "--name", role_name,
@@ -3192,7 +3291,7 @@
                 "--gpus", "all",
                 "-v", vol_mount,
                 "-v", compat_mount
-            ] + jit_mounts + env_flags + [image_tag] + entrypoint_cmd
+            ] + jit_mounts + env_flags + [host_image_tag] + entrypoint_cmd

             res = None if dry_run else run_ssh(ip, None, docker_cmd, timeout=60)
             if dry_run: docker_run_commands[host] = docker_cmd
@@ -3214,13 +3313,24 @@
             run_ssh(head_ip, None, vllm_exec_cmd, timeout=30)

     if dry_run:
-        return {
+        dry_run_result = {
             "status": "dry_run",
             "message": f"Dry-run for {model} across {nodes} node(s) - no SSH connections made, nothing executed.",
             "targets": target_hosts,
             "head": head,
             "docker_run_commands": docker_run_commands,
         }
+        if mods_report:
+            dry_run_result["mods"] = mods_report
+        return dry_run_result
```

`common/mods.py`, `common/recipes.py`, `common/ssh.py`: **unchanged**,
not reproduced here (attached uploads are still authoritative).
