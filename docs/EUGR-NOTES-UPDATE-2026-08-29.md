# EUGR-REFERENCE-NOTES.md — update 2026-08-29

Merge instructions for `docs/EUGR-REFERENCE-NOTES.md`. Two existing passages
are now wrong and should be **replaced**; the rest is new material to
**append** as a dated update section. I don't have the full current file
(only the sections surfaced by search), so this is written as targeted edits
rather than a full replacement — check for other references to mods delivery
while merging.

---

## REPLACE — "Two distinct patch mechanisms — don't conflate them"

The existing section says `mods/<name>/run.sh` is *"runtime, applied via
`docker exec` after the container launches. This is what our `recipe.mods:`
field maps to."* That framing is misleading in a way that led to a wrong
design decision, and the second sentence is now only half true.

Replace with:

> ## Two distinct patch mechanisms — don't conflate them
>
> - **`mods/<name>/run.sh`** — a directory containing a shell script plus
>   payload files. eugr applies these via `docker exec` after the container
>   launches, which is why they were originally described here as "runtime
>   patches." **That description is about eugr's delivery timing, not about
>   what the mods are.** Reviewed properly on 2026-08-29: of ~20 mod
>   directories, all but one are *build-time modifications of the installed
>   vLLM* — `git apply` of source patches, in-place Python rewrites, a
>   `.pth` site-packages hook, file drops. They only work in eugr's system
>   because their containers start idle and vLLM is launched afterwards.
>   **We borrow the format and reject the delivery** — see `ROADMAP.md` →
>   "Model-specific mods: bake a derived image layer."
> - **`docker/patch_vllm_*.py` + top-level `*.patch`** — applied during
>   `docker build` when compiling vLLM from source. Still not relevant to
>   us: we don't compile vLLM. Note the *reason* previously given here
>   ("we don't build our own images — we pull prebuilt tags") is now
>   partially false: we will build derived layers on top of prebuilt tags.
>   The conclusion stands, the reasoning doesn't.

---

## REPLACE — the "Adapt" bullet under "Borrow / adapt / skip"

Currently reads: *"Adapt: mods execution — same copy-in-and-run idea, driven
by our off-node SSH call instead of their local/SSH dual path."*

Replace that bullet with:

> - **Adapt**: mods — **borrow the format** (a directory containing `run.sh`
>   plus vendored payloads), **reject the delivery**. Their
>   copy-in-and-`docker exec` approach depends on containers starting idle;
>   our 1-node path starts the container with the vLLM entrypoint as PID 1,
>   so an exec'd mod races vLLM's own startup and loses for anything
>   patching code vLLM imports or arguments it parses. We bake a derived
>   image layer instead. Recipe topology representation — unchanged, we keep
>   explicit `tp_size`/`pp_size`.

---

## APPEND — new update section

> ## 2026-08-29 update — the mod library, read properly
>
> Prior notes characterised mods from the mechanism (`run.sh` + `docker
> exec`) without reading the scripts. Doing so changed a design decision.
> Findings, in rough order of consequence:
>
> ### Nearly every mod is build-time work in runtime clothing
>
> | Mod | What it actually does |
> |---|---|
> | `gpu-mem-util-gb` | in-place rewrite of **8** vLLM source files to add a `--gpu-memory-utilization-gb` CLI arg; self-validates with `ast.parse`, idempotent via `already=` guards |
> | `diffusiongemma` | several `git apply` patches (attention, content-channel sanitizer, streaming reasoning) + a chat-template drop |
> | `fix-gemma4-tool-parser` | `curl`s vLLM PR #38909 and `git apply`s it |
> | `fix-glm-4.7-flash-AWQ`, `fix-Salyut1-GLM-4.7-NVFP4` | vendored `.patch` files against vLLM source |
> | `fix-qwen3-coder-next` | ships `_triton_alloc_setup.pth` — a site-packages hook executed at Python startup |
> | `fix-qwen3.5/3.6-chat-template` | `cp chat_template.jinja $WORKSPACE_DIR/fixed_chat_template.jinja` |
> | `use-official-vllm`, `use-ngc-vllm` | base-image swap — **no equivalent needed**, our `image:` field already does this |
> | `drop-caches` | the sole genuine runtime mod — see below |
>
> **`gpu-mem-util-gb` is the decisive one.** It patches
> `vllm/engine/arg_utils.py` to register a CLI argument parsed at process
> startup. No exec-based mechanism can apply it in time. It was one of the
> two mods this document previously prioritised for porting, which is how
> the flaw surfaced.
>
> ### `drop-caches` is not a container mod
>
> A `nohup` loop running `sync; echo 3 > /proc/sys/vm/drop_caches` every 60s
> for the container's lifetime, with a PIDFILE. Per their changelog it
> exists to stop `fastsafetensors` stalling mid-load on large models near
> the memory ceiling — so it must run *during* loading, continuously.
>
> `/proc/sys/vm/drop_caches` is **not namespaced**: writing it inside a
> container acts on the host. eugr wraps it as a mod because an on-node
> container was their only execution surface. We have an off-node control
> plane that already runs commands against hosts over SSH. Tracked in
> `ROADMAP.md` as a host-side daemon, deliberately deferred — we have no
> recorded `fastsafetensors` stall on our own hardware.
>
> ### Three findings worth carrying into our own implementation
>
> 1. **A mod can fetch from the network at apply time.**
>    `fix-gemma4-tool-parser` `curl`s a GitHub PR diff. On our per-host bake
>    design that is a correctness hazard, not just a style problem — two
>    hosts baking at different moments could produce different images,
>    presenting as a rank-dependent crash. It also breaks under our
>    offline-mode switches. Any mod we port must have its payload vendored.
> 2. **Mods can have a hidden coupling to `vllm_args`.** Chat-template mods
>    drop a file, and the recipe must *separately* pass
>    `--chat-template fixed_chat_template.jinja`. Correct only together,
>    with nothing enforcing the pairing — same class as the known-bad
>    flag-combination linter already scoped in `ROADMAP.md`, and worth
>    folding into it.
> 3. **`WORKSPACE_DIR` is load-bearing.** Those same mods write to
>    `$WORKSPACE_DIR`, which eugr's launcher sets to the container's default
>    working directory. Any port must set it to the image's real
>    `WorkingDir` or the payload lands where vLLM won't look.
>
> ### Smaller observations
>
> - eugr's changelog mentions a `--keep-entrypoint` flag "to preserve the
>   image entrypoint." Worth knowing that entrypoint preservation is a real
>   concern in their tooling too — mild corroboration for our own
>   `docker commit` fidelity spike (`PHASE-MODS-PROMPTS.md` Task M0).
> - Their mods fail hard: `apply_mod_to_container()` exits non-zero on any
>   `run.sh` failure rather than continuing. Correct behaviour, worth
>   copying — a half-applied patch set is worse than a refused deploy.
> - Their Gemma4-26B-A4B recipe "no longer applies the obsolete tool parser
>   mod by default," and a `diffusiongemma` mod supplies Gemma4
>   reasoning/content-channel fixes. Relevant if we pursue Gemma 4 tool
>   calling or reasoning output; not needed for plain serving.
>
> ### Priority-list items now closed
>
> Items 4 and 5 of "If we pull more" are **done**. `mods/drop-caches` and
> `mods/gpu-mem-util-gb` have been read (findings above);
> `mods/use-official-vllm` / `use-ngc-vllm` turned out to be base-image
> swaps with no lesson for us, since our `image:` field already covers that
> case. Item 4's speculative hope — that `gpu-mem-util-gb` "may reveal a
> better fix than a static `gpu_util_ceiling`" — is **confirmed**: it does
> exactly that, letting memory be specified in absolute GiB rather than a
> fraction, which fits GB10's shared-memory model better. It is a large
> multi-file patcher, though, not a trivial port.
>
> Items 1–3 (N>2 cluster recipes, `tests/expected_commands.sh`,
> `.env.example`) remain unreviewed and unchanged in priority. Item 1 has
> gained relevance: `ROADMAP.md` now carries a Phase 3 entry on
> `compute_config_hash()` topology-key stability, and their 3x/4x/8x recipes
> are the closest thing to prior art we have.
>
> ### One thing to correct elsewhere
>
> `examples/diffusion-gemma-bf16.yaml` carries a TODO stating
> `mods/diffusiongemma` was "dropped, not translated" because no execution
> path existed and its contents were never reviewed. Both halves are now
> stale — the mod is characterised above, and an execution path is designed.
> Update that TODO rather than leaving it implying unknown content.
