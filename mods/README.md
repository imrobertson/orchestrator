# mods/

Each subdirectory here is a **mod**: a named, self-contained set of patches
applied to a base vLLM image by baking a derived image layer (`docker
commit`), rather than by patching a running container. A recipe references
a mod by directory name only, under `recipe.mods:` in its YAML -- never a
host path. See `common/recipes.py`'s `RecipeConfig.mods` and
`_validate_mod_name()` for the load-time shape check, and `ROADMAP.md` →
"Model-specific mods: bake a derived image layer" for the full design
rationale.

## The one constraint every mod must follow

**Mod payloads must be vendored in this directory. No network fetches at
bake time.**

Every file a mod's `run.sh` needs -- a patched `.py` module, a `.jinja`
chat template, a `.diff`/`.patch` file -- must be committed here in git,
not downloaded during the bake. `eugr/mods/fix-gemma4-tool-parser/run.sh`
is the cautionary example: it does `curl .../pull/38909.diff | git apply`
at bake time. Do not port that pattern.

Two reasons this matters, not one:

1. **Determinism across hosts.** Each host bakes its own derived image
   layer independently (see Task MB). If a mod's content can change
   between two bakes -- a moved branch, an edited gist, a PR that gets
   force-pushed -- the two hosts can end up with *different images* from
   the same recipe. That presents as an inexplicable rank-dependent crash,
   not as an obviously-wrong config. Vendoring the payload is what makes
   per-host baking safe at all; if this constraint is ever relaxed, the
   per-host bake design in Task MB needs to be revisited too.
2. **Offline clusters.** This cluster runs with `cluster_config.yaml`'s
   `global_hf_hub_offline` / `global_transformers_offline` switches
   available, and a mod that reaches out to the network at bake time fails
   outright when those are set, for reasons that have nothing to do with
   the mod itself.

## Directory shape

```
mods/
  <mod-name>/
    run.sh          # required -- executed inside the throwaway bake
                     # container with WORKSPACE_DIR set to the base image's
                     # real WorkingDir (see Task M0's result for the
                     # confirmed value on this cluster's current image).
                     # Must exit 0 on success and non-zero on failure --
                     # a failed run.sh aborts the whole bake (Task MB).
                     # Should be idempotent: re-running it (e.g. against an
                     # already-patched layer) must not fail or double-apply.
    <payload files>  # whatever run.sh needs: patched source, .jinja
                     # templates, .diff/.patch files, etc. -- all vendored,
                     # none fetched.
```

`<mod-name>` becomes part of the derived image tag's hash input (Task MB),
so both the name and every payload file's *contents* matter: editing a
payload changes the tag and forces a rebake.

## What does not belong here

Runtime mod application, a `phase:` field, and mod distribution via a
registry were all considered and deliberately excluded from this design.
See `ROADMAP.md` for why. If you find a real need for any of them, report
it rather than building it into a mod's `run.sh`.
