# Smoke-test playbook

How we built `smoke_test_mc.py` and what it cost to get there, so the next
phase's smoke test (MD, ME, ...) starts from this instead of re-deriving it
turn by turn. This is a process document, not a code module — nothing here
is imported by anything.

## The dependency closure, once and for all

Importing `dgx-orchestrator.py` standalone (outside the real daemon/CLI)
requires the full chain below. Every item was discovered one at a time
across several turns this session (a new `ImportError`/`AttributeError`
each time) — that discovery process shouldn't repeat for MD/ME. If a
future phase adds a new `common/*.py` module or a new top-level config
file, add it to this list in the same PR that introduces it.

**Files needed to import `dgx-orchestrator.py` at all, in dependency
order:**

1. `common/config.py` — real or faked. If real: needs a real
   `cluster_config.yaml` on disk at `BASE_DIR / "cluster_config.yaml"`
   (`load_cluster_config()` raises `FileNotFoundError` otherwise, no
   silent default).
2. `common/constants.py` — real or faked. **If real, it runs an
   `_assert_matches_cluster_config()` check at IMPORT TIME** (not call
   time) that cross-validates `ContainerRole`'s values against
   `cluster_config.yaml`'s `container_names` block, and calls
   `load_cluster_config()` to do it — so real `constants.py` transitively
   requires the same real `cluster_config.yaml` as real `config.py`, even
   if you only wanted to fake `config.py`. `ContainerRole` is a
   `StrEnum` — its members stringify to their plain value (`"vllm-head"`,
   not `"ContainerRole.HEAD"`), which matters because `run_ssh()` does
   `shlex.quote(str(arg))` on every command-list element.
3. `common/recipes.py` — real (needs `pydantic`, `pydantic_core`,
   `typing_extensions`, `typing_inspection`, `annotated_types`, `pyyaml`
   installed; see wheel gotcha below). Reads `RECIPES_DIR = BASE_DIR /
   "recipes"` (subdirs `local/`, `eugr/`) and defines `MODS_DIR = BASE_DIR
   / "mods"`. **Does not surface `mods:` in `build_catalog_response()`'s
   output** — if a smoke test needs a model's mod list, read it via
   `load_recipes()`, not the catalog dict.
4. `common/mods.py` — real. Imports `MODS_DIR`/`validate_mod_name` from
   `common/recipes.py` and `resolve_user_identity_key`/`run_ssh` from
   `common/ssh.py`. Calls `subprocess.run` directly (not via `run_ssh`)
   for `scp` — mock at that layer if a live bake needs to run in a smoke
   test, not the network.
5. `common/ssh.py` — real. `BASE_DIR` here is resolved independently of
   `common/config.py`'s `BASE_DIR` (same env var, same fallback pattern,
   separately computed) — don't assume patching one patches the other.
   `run_ssh()` shells out via `subprocess.Popen`; mock at that layer, not
   by replacing `run_ssh` itself, if you want the real
   `ControlMaster`/timeout/capture logic under test.
6. A real or synthetic `cluster_config.yaml` — required transitively by
   (1) and (2) above whenever either is real. Needs, at minimum: `hosts`
   (each needs `alias`, `role`, `management_ip`, `backplane_ip`,
   `volume_mount`), `container_names` (`standalone`/`head`/`worker`),
   `ports`, `default_image`, `ssh_user`, `ssh_key_name`,
   `gpu_util_ceiling`, `network` (`topology`/`interface`/`nccl_ib_hca`).
   `tuning:` may be omitted — every field has a default.
7. `dgx-orchestrator.py` itself.

**What's still never real in any smoke test to date:** the actual `ssh`/
`scp` binaries, and anything Docker-daemon-side. Everything above that
line is genuine project code; everything below it is a transport double.

## Rule: never hardcode a value the config supplies

This was the single most repeated bug this session — four separate false
failures, all from the same root cause. `smoke_test_mc.py`'s early drafts
hardcoded `"eugr/spark-vllm-b12x:latest"` (a fake fixture's `default_image`)
and `"100.64.0.3"`/`"100.64.0.4"` (a fake fixture's host IPs) directly into
assertions. The moment a real `cluster_config.yaml` was swapped in — with
a different real image and real IPs — those assertions failed even though
the code under test was correct.

**The fix, generalized:** after importing the module under test, read
every config-derived expected value back out of the *loaded* module
(`dgx.load_cluster_config().default_image`, `dgx.HOSTS["spark-3"]["ip"]`,
etc.) rather than writing the fixture's own literal back into an
assertion. The fixture and the assertion must never both hardcode the
same value independently — one of them should derive it from the other at
runtime. When adding assertions to a future phase's smoke test, if you
catch yourself typing a literal that also appears in the fixture YAML,
stop and read it from the loaded module instead.

## The wheel-filename gotcha

If dependency wheels arrive with underscores where dots should be in a
multi-tag platform field (e.g.
`manylinux_2_17_x86_64_manylinux2014_x86_64.whl` instead of
`manylinux_2_17_x86_64.manylinux2014_x86_64.whl`), `pip` will reject them
as "not a supported wheel on this platform" even on a fully compatible
system — the platform-tag field is dot-joined for multiple tags, and an
underscore there is unparseable, not just cosmetic. Fix: copy the file to
a name with the dot restored between platform tags (leave the tags
themselves, which legitimately contain underscores, alone) and `pip
install` the renamed copy with `--no-deps`. Pure-Python wheels
(`-py3-none-any.whl`) aren't affected — this only bites
platform-specific wheels with more than one manylinux tag glued together
(`pydantic_core`, `pyyaml`, and anything else with compiled extensions
targeting multiple manylinux baselines).

## Handling a directory where a file path was expected

`--orchestrator .` (pointing at a directory) used to crash with a bare
`AttributeError` three calls deep in `importlib`. `smoke_test_mc.py` now
resolves a directory argument by looking for `dgx-orchestrator.py` inside
it and using that, printing which file it picked — and exits with an
explicit message (not a traceback) if that lookup fails too. Generalize
this instinct for future scaffolding: prefer resolving a slightly-loose
CLI argument over crashing on it, but always print what was resolved to,
so a wrong guess is visible rather than silently substituted.

## Graceful degradation when a real dependency is missing

`smoke_test_mc.py`'s `--common-dir` accepts real `config.py`/`constants.py`
if present, and falls back to bundled fakes if not — but critically, if a
real `config.py` is found and NO `cluster_config.yaml` can be located
(neither via `--cluster-config` nor auto-discovered next to
`--common-dir`), it does not attempt to import the real `config.py`
anyway and let it crash. It falls back to the fake for BOTH `config.py`
and `constants.py` together (never a mixed real/fake state) and prints
why. Apply the same pattern to any future phase's smoke test: know which
real files are mutually load-bearing (here: real `config.py` requires
`cluster_config.yaml`; real `constants.py` transitively requires it too),
and fail down to a fully-fake-for-this-run state as a unit, not
file-by-file, when a load-bearing dependency is missing.

## Scaffolding checklist for the next phase's smoke test

1. Start from `smoke_test_mc.py`, don't write a new one from scratch —
   the dependency closure above doesn't change phase to phase unless a
   phase adds a new `common/*.py` module.
2. If the new phase adds a `common/*.py` module (none have yet beyond
   MA/MB/MC's), add it to `_REQUIRED_REAL_MODULES` or the optional-real
   list (whichever matches how load-bearing it is), and add it to the
   dependency closure list above in this doc.
3. If the new phase needs new recipe/mod fixture data (e.g. MD's
   `mods/_noop/`), add it to `build_fixture()` alongside the existing
   `test-model-{nomods,mods,badmod}` fixtures rather than building a
   parallel fixture-construction path.
4. Before writing new assertions, grep the draft for any literal that
   also appears in the fixture YAML or the fake `config.py` — that's the
   bug pattern from this session recurring.
5. Run the new/extended smoke test against BOTH the fully-fake path and
   (once available) the real `common/` + real `cluster_config.yaml` path,
   the same as this session did for MC — the fully-fake run alone did not
   catch the four false failures; only the real-config run did.
6. If a check ever needs to be trusted, sabotage-test it once: revert the
   line it's supposed to catch and confirm the check actually fails. This
   session did this for MC's tag-substitution line; worth doing again for
   whatever MD's/ME's most load-bearing new check turns out to be.
