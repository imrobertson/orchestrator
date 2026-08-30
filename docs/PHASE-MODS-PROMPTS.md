# Mods Implementation Prompts

Give `recipe.mods:` a real execution mechanism, so models requiring patched
vLLM source can be deployed. Hand these to an implementing model **one at a
time, in order**, in a fresh conversation each time.

Design rationale, the survey of eugr's mod library, and the rejected
alternatives live in `ROADMAP.md` → "Model-specific mods: bake a derived
image layer". That entry is authoritative. This file is only the sequencing.
Read it before starting M0.

**Prerequisite:** none structurally. `RecipeConfig.mods` already exists and
round-trips through `load_recipes()`; this is an execution problem, not a
schema migration.

**Stop condition:** M0 is a gate on the *design*, not a formality. If it
fails, do not proceed to MA — reopen the delivery decision.

---

## Three constraints that must survive implementation

Found by reading eugr's actual mod library. Anyone implementing without
knowing them will ship something that works on one host and not the other.

**1. Mod payloads must be vendored in-repo. No network fetches at bake
time.** `eugr/mods/fix-gemma4-tool-parser/run.sh` does
`curl .../pull/38909.diff | git apply`. Because each host bakes its own
layer, a mod whose content can change between two bakes can produce
*different images on head and worker* — a class of failure that would
present as an inexplicable rank-dependent crash. It also fails outright
under `cluster_config.yaml`'s `global_hf_hub_offline` /
`global_transformers_offline` switches. Porting a mod that fetches means
pinning the artifact into the repo, never copying the `curl`.

**2. Per-host bake is safe only because of constraint 1.** We deliberately
do not bake once and distribute (no registry, no multi-GB
`docker save | ssh docker load`). That choice depends entirely on mods being
deterministic. If constraint 1 is ever relaxed, this must be revisited.

**3. `run.sh` must execute with `WORKSPACE_DIR` set to the image's real
`WorkingDir`.** eugr's chat-template mods do
`cp chat_template.jinja $WORKSPACE_DIR/fixed_chat_template.jinja`. Get this
wrong and the file bakes into a directory vLLM never looks in, with no error
at bake time and a confusing `--chat-template` failure at serve time.

---

## Task M0 — `docker commit` fidelity spike (no code)

> ### Context
>
> You are validating an assumption before a design is implemented. This task
> writes no production code. It answers one question: does `docker commit`
> preserve image configuration faithfully on the vLLM images this cluster
> actually runs?
>
> The images are arm64, running on NVIDIA GB10 (DGX Spark) hardware, derived
> from NVIDIA NGC bases. The relevant tag is `eugr/spark-vllm-b12x:latest`.
>
> ### Why this matters
>
> A planned feature bakes model-specific patches into a derived image layer
> (`docker commit`) rather than applying them to a running container. That
> only works if the derived image behaves identically to its base except for
> the patched files. This is documented `docker commit` behaviour and is
> expected to hold — but it has never been checked on these images, and if
> it doesn't hold the whole delivery mechanism is wrong.
>
> ### What to do
>
> 1. Start a throwaway container from the base image, change nothing, and
>    `docker commit` it to a scratch tag.
> 2. Diff `docker inspect --format '{{json .Config}}'` between base and
>    committed. Report any difference in `Entrypoint`, `Cmd`, `Env`,
>    `WorkingDir`, `Labels`, `ExposedPorts`.
> 3. Record the image's actual `WorkingDir` value explicitly — a later task
>    depends on it (see constraint 3 above).
> 4. Deploy the committed scratch tag through the *existing* orchestrator
>    deploy path (set `image:` on a scratch recipe) and confirm it serves
>    identically to the base. The NGC-derived entrypoint prints a CUDA
>    banner and performs driver checks at startup; confirm that still
>    happens and that the NVIDIA container runtime hooks still fire.
> 5. Repeat 1–2 after making one trivial filesystem change inside the
>    container (e.g. `touch /tmp/marker`), to confirm a real commit behaves
>    the same as a no-change commit.
>
> ### What to report back
>
> A plain pass/fail on each of the six `.Config` fields, the observed
> `WorkingDir`, and whether the committed image deployed and served
> successfully. If anything differs, report exactly what — do not work
> around it, do not adjust the plan to accommodate it. A failure here means
> the design changes, not the implementation.
>
> Do not assume behaviour by analogy to x86 Docker experience. This stack
> has already produced one x86-only surprise (see
> `BACKLOG-dspark-sm120-image.md`).

---

## Task MA — Mod format and loader schema

> ### Context
>
> Python control plane for a two-node NVIDIA DGX Spark cluster, running
> off-node in Docker, reaching the Sparks over SSH. In daily production use.
>
> Recipes are per-model YAML under `recipes/{local,eugr}/`, loaded and
> validated by `common/recipes.py` (pydantic v2). `RecipeConfig` already has
> a `mods: list = []` field that is documented INERT — it validates and
> round-trips, but is excluded from `_TOPOLOGY_OUTPUT_FIELDS`, never read by
> `build_catalog_response()` or the deploy path, and excluded from
> `compute_config_hash()`.
>
> Attached: `common/recipes.py`, a representative `recipes/local/*.yaml`.
>
> ### Goal
>
> Give `mods` a real type and a resolution rule. No execution yet — this
> task only makes the field meaningful and validated.
>
> ### Requirements
>
> - A mod is identified by a **directory name only**, resolved by the
>   orchestrator against a repo-root `mods/` directory. Recipes must never
>   contain host paths. Reject any value containing `/`, `\`, `..`, or a
>   leading path separator, at load time.
> - Shape validation belongs at load (pydantic). **Existence validation does
>   not.** `build_catalog_response()` fails closed — one bad recipe empties
>   the entire catalog (this has caused a real production incident; see
>   `ARCHITECTURE-MIGRATION-PLAN.md` Phase 2's status note). A mod directory
>   that is missing must fail the *deploy*, not the *catalog load*.
> - Every existing recipe has `mods: []`. Verify that against
>   `recipes/local/*.yaml` and `recipes/eugr/*.yaml` before assuming it —
>   report if any file has content there.
> - Do **not** add `mods` to `compute_config_hash()`. The mod set will reach
>   the hash indirectly via the resolved image tag in Task MB; adding it
>   directly would double-count and is explicitly not the design.
> - Create the `mods/` directory with a README stating constraint 1 above
>   (vendored payloads, no network fetches at bake time). Confirm
>   `.gitignore` does not exclude `.patch`, `.pth`, `.jinja`, or `.diff`
>   files — this repo's `.gitignore` was tightened after a credentials
>   scrub and may be broader than intended.
>
> ### Verification
>
> Existing catalog loads unchanged. A recipe with a nonexistent mod name
> still loads and still appears in the catalog. A recipe with a path-shaped
> mod value fails at load with a clear message.

---

## Task MB — Bake, cache, and tag resolution

> ### Context
>
> Continues from Task MA. `recipe.mods` is now typed and validated but still
> has no execution path.
>
> Attached: `common/recipes.py` (post-MA), `common/ssh.py`,
> `dgx-orchestrator.py`, `cluster_config.yaml`. Read Task M0's report before
> starting — it records the image's real `WorkingDir`.
>
> ### Goal
>
> A function that, given a base image tag and a mod set, returns the tag of
> an image with those mods applied — baking it on the target host first if
> it does not already exist there.
>
> ### Requirements
>
> - Deterministic tag: `<base-image>-mods-<hash>`, where the hash covers the
>   mod set (names **and** file contents, so editing a payload produces a
>   new tag). Sanitise the base tag into something legal in an image name.
> - If the tag already exists on the host, skip the bake entirely. This makes
>   repeat deploys free and the operation idempotent.
> - Bake sequence per host: `docker create`/`run` a throwaway container from
>   the base with its normal entrypoint overridden to something inert, copy
>   the mod directories in, run each `run.sh` in declared order with
>   `WORKSPACE_DIR` set per constraint 3, then `docker commit` to the tag,
>   then remove the throwaway container.
> - **Fail loudly and abort the deploy if any `run.sh` exits non-zero.**
>   eugr's own `apply_mod_to_container()` does exactly this. A half-applied
>   patch set is worse than a refused deploy. Surface the failing mod name,
>   the host, and the script's stderr.
> - Mod directories ship from the orchestrator container (they are visible at
>   `/app/mods/` via `docker-compose.yml`'s `.:/app` bind mount) to each
>   host. `common/ssh.py` has `run_ssh()` but **no stdin plumbing** — it is
>   `subprocess.Popen` with no stdin kwargs — so use `scp` with the key from
>   `resolve_user_identity_key()`, or extend `run_ssh()` deliberately rather
>   than base64-stuffing file contents into argv.
> - Unit-testable without live hardware where possible; the bake itself
>   needs a host.
>
> ### Verification
>
> Bake a mod set twice; confirm the second call is a no-op. Change one byte
> in a payload file; confirm the tag changes and a rebake occurs. Make a
> `run.sh` exit 1; confirm the operation aborts with a useful message and
> leaves no dangling container.

---

## Task MC — Deploy-path integration

> ### Context
>
> Continues from MB. Bake-and-resolve works standalone but nothing calls it.
>
> Attached: `dgx-orchestrator.py`, plus MB's helper.
>
> ### Goal
>
> `_execute_deployment_impl()` resolves the recipe's mod set to an image tag
> and deploys from that tag instead of the base.
>
> ### Requirements
>
> - **One shared helper, called from both the 1-node and 2-node branches.**
>   These are parallel, not shared, code paths. This repo has already been
>   bitten by logic that existed in one branch and not the other; do not
>   write the resolution twice.
> - Empty mod set must be a strict no-op — same image tag, same behaviour,
>   zero extra SSH round trips. Every existing recipe has `mods: []`, so a
>   regression here breaks the whole catalog.
> - The bake happens per target host, before that host's `docker run`.
> - Respect `--dry-run`: report the resolved tag and what would be baked;
>   make no SSH connections and bake nothing.
> - Mod resolution failure aborts the deploy before any container starts.
>
> ### Verification
>
> `--dry-run` on every existing recipe produces byte-identical output to
> before this change. One real 1-node deploy and one real 2-node deploy of a
> known-good recipe with no mods, confirming no behaviour change.

---

## Task MD — End-to-end proof with a no-op mod

> ### Goal
>
> Prove the full chain on a mod that cannot break anything: schema parse →
> hash → ship → bake → tag resolution → deploy → serve.
>
> ### Requirements
>
> - Create `mods/_noop/` containing a `run.sh` that writes a single marker
>   file and exits 0. Nothing that touches vLLM.
> - Deploy a scratch recipe carrying it, on both 1-node and 2-node.
> - Confirm: the derived tag exists on each host, the marker file is present
>   inside the running container, the model serves normally, and a second
>   deploy skips the bake.
>
> ### Why a no-op first
>
> If the delivery mechanism is broken, this finds it on a harmless file
> rather than entangled with "did the real patch work." Do not skip
> straight to ME.

---

## Task ME — Wrap the first real mod

> ### Goal
>
> `mods/gemma4-nvfp4/` — the patched `gemma4_patched.py` from
> `bg-digitalservices/Gemma-4-26B-A4B-it-NVFP4`, vendored, plus a `run.sh`
> that copies it over the installed `gemma4.py`.
>
> ### Requirements
>
> - `run.sh` must resolve the vLLM package root **inside the container** at
>   runtime — `python3 -c "import vllm, os; print(os.path.dirname(vllm.__file__))"`
>   — never a hardcoded `python3.12/dist-packages` path. A base-image Python
>   bump would otherwise land the patch where nothing imports it, stock
>   loader runs, and the original error returns while the mod appears to
>   have applied cleanly.
> - Verify the file it is replacing actually exists before replacing it.
>   Exit non-zero if not: the patch is aimed at nothing.
> - Make it idempotent — re-running must not fail or double-apply. eugr's
>   mods do this with `already=` guards; follow the same pattern.
> - Vendor the payload. Do not fetch it at bake time (constraint 1).
>
> ### Verification
>
> The model loads, which it demonstrably does not without the patch. Also
> confirm the boot log shows the expected NVFP4/MoE backend selection rather
> than only that the process survived startup.

---

## What to send back for review

After each task: the changed files in full (not diffs), what was verified
and how, and anything encountered that contradicts this plan. The last one
matters most — this design was rewritten three times tonight, twice because
an assumption survived longer than it should have. A contradiction found
during implementation is more valuable than a task completed smoothly.

## A note on scope

Do not implement runtime mod application, a `phase:` field, or mod
distribution via a registry. All three were considered and deliberately
excluded; the reasoning is in `ROADMAP.md`. If a real need for any of them
appears during implementation, report it rather than building it.
