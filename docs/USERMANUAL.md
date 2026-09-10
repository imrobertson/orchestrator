# User manual

Operating the Maestro control plane: deploying, monitoring and tearing down
models across the GB10 cluster.

- Architecture and configuration reference → [`../README.md`](../README.md)
- Setting up a control station → [`INSTALL.md`](INSTALL.md)
- **Which models exist, how fast they are, what they're for** →
  [`MODELS.md`](MODELS.md)

This document deliberately carries no list of models. The catalog changes far
faster than prose does, and every previous attempt to mirror it here went stale.
Examples below use real recipe names only as examples — check `dgx-config status`
or the dashboard dropdown for what is actually available.

Access is either the web dashboard at `http://<maestro>:5000` or the
`dgx-config` CLI over SSH to `maestro`.

> Reaching `maestro` over Tailscale SSH means your identity is verified and
> audited **for the SSH hop itself**. Per-deploy attribution beyond that — the
> dashboard's "User ID / Auditor" field, the CLI's forwarded `$USER` — is
> self-reported, not authenticated. Anyone can type anything there.

---

## Contents

- [The web dashboard](#the-web-dashboard)
- [The CLI](#the-cli-dgx-config)
- [Reserved hosts](#reserved-hosts)
- [Deploying](#deploying)
- [Adding a new model](#adding-a-new-model)
- [Teardown](#teardown)
- [Offline and air-gapped operation](#offline-and-air-gapped-operation)
- [Benchmarking](#benchmarking)
- [A/B testing two recipes](#ab-testing-two-recipes)
- [Troubleshooting](#troubleshooting)

---

## The web dashboard

### Header

Live cluster-wide throughput (`SPEED: X tok/s`) and request concurrency
(`THREADS: X active (Y queued)`) whenever a model is serving. Server time in
**UTC** — a log-comparison reference, not a wall clock. A version badge showing
the running `ORCHESTRATOR_VERSION`, which is how you confirm a fix you just
deployed actually landed rather than assuming it did. An **ONLINE MODE** /
**OFFLINE MODE** indicator, which is also the toggle.

### Per-host panels

One panel per host from the live inventory. Each shows Docker daemon status,
active container name and state, the loaded model, model status, an ETA while
loading, and live **TEMP** / **GPU** / **MEM** readings.

GB10 uses LPDDR5x shared between CPU and GPU, so standard VRAM queries return
`[N/A]`. The dashboard reports this honestly as **Unified / 131072 MB**. Watch
the `GPU: %` metric for compute saturation instead.

Model status is one of:

| Status | Meaning |
|---|---|
| `READY` | serving |
| a warmup/loading stage | still coming up; see the ETA note below |
| `CRASHED` | the engine process itself has died. Shown in red with a reason where one could be determined — check that host's logs |
| `ORPHANED` | a worker whose head crashed out from under it. Needs a teardown, not a wait |
| `NONE` | nothing deployed |

A host also shows a **RESERVED** badge when `cluster_config.yaml` marks it so,
next to its ONLINE/OFFLINE badge.

**Model status is evaluated per host**, against that host's own `/health`.
Before 2026-09-06 a non-serving host inherited the serving host's readiness, so
a still-compiling or dead model on the second node displayed as `READY`. If
older notes or screenshots disagree with what you see now, that is why —
TOMBSTONES #130.

### The ETA display

The ETA (`~340s remaining`) is a learned average of past load times for that
exact model+topology, stored in `model_ledger.json` — not a fixed guess. The
first time you deploy a given combination you'll see
`(Initial run - no history)`; every successful deploy after that refines it.

A long load switches to `Finishing startup (+Ns over est.)` rather than counting
down past zero. An engine crash during startup switches to `Check Docker Logs`
rather than continuing to count elapsed time against a dead process.

### Deploying from the dashboard

1. Pick a model from **Select Model** — populated live from the recipe catalog.
2. Pick the **Topology** (1-Node / 2-Node) and, for 1-node, the **Target** host.
3. Enter a **User ID / Auditor** (defaults to `dashboard_user`). Self-reported.
4. Click **Deploy Model**.

Invalid topologies are hidden automatically based on the recipe — a
`2_node`-only model shows no 1-Node option. The **Target** dropdown appears for
**every** 1-node deploy, including recipes that define only `1_node` and
therefore show no topology picker at all. (Until 2026-09-06 the Target picker
was nested inside the row the topology logic hid, so single-topology recipes
offered no way to choose a host and silently used `default_deploy_target`. If
you hit that, you weren't missing a control — you weren't given one. TOMBSTONES
#132.)

Both dropdowns follow what's currently running **only until you pick something
yourself**, after which they stay put. The target dropdown never auto-selects a
reserved host. (Before 2026-09-06 it re-pinned to `serving_host` on every
4-second poll, which made it unusable once anything resident occupied the
first-listed node — your selection reverted within seconds and the deploy went
somewhere you didn't choose. If you have older notes saying "use the CLI
instead" because of this, they're stale — TOMBSTONES #131.)

The **auto-run benchmark** checkbox only appears while nothing is deployed or
loading. That's the sole window in which it does anything: its value is captured
once, at the moment Deploy is clicked. After that it's replaced by a status line
reflecting whatever was actually decided. TOMBSTONES #137.

One known gap: that status line does not survive a page reload. Reload during an
in-flight launch and the checkbox comes back as an editable control for a deploy
whose choice was already sent — the exact confusion #137 exists to prevent.
Ticking it then still does nothing. Open in `WORKSTREAMS.md`.

### Teardown from the dashboard

A scope selector sits beside the red **Teardown Runtimes** button — **All
hosts**, or a single named host. Scoped teardown is the point of the whole
feature: cycle the scratch node while a resident model keeps serving elsewhere.

### Live logs

The full-width bottom panel shows a live trace of the selected host's container
logs. Health-check and metrics-polling lines are filtered out so it stays
readable. The host dropdown starts on the deploy target rather than a fixed
node — **if a 2-node deploy hangs, switch to the worker.** Worker logs usually
carry the actual stack trace for cross-node networking timeouts.

The Copy button falls back to `execCommand` on plain-HTTP origins, where the
Clipboard API isn't available.

> **`docker logs` does not show you a Ray worker's real output.** In a
> 2-node deploy the worker's stdout and stderr are redirected inside the
> container to `/tmp/ray/session_latest/logs/worker*.out`; `docker logs`
> sees only top-level daemon initialization. If the panel looks empty or
> uninformative on a worker that is clearly doing something, that is why.
> Every deploy bind-mounts that directory to a persistent per-run host path
> (`~/.cache/ray-logs/<deploy_run_id>/<host>`), so those logs survive
> teardown and can be read directly — which is the only reason a crashed
> worker is diagnosable at all after the fact.

---

## The CLI (`dgx-config`)

### Interactive menu

```bash
dgx-config menu
```

Prints cluster status including live throughput and queue depth, then walks you
through model, topology and target host, showing the estimated load time for
each topology before you commit.

### Command reference

| Task | Command |
|---|---|
| Status | `dgx-config status` |
| Deploy | `dgx-config deploy --model <key> --nodes 2` |
| Preview a deploy | add `--dry-run` |
| Block until healthy | add `--wait` |
| Benchmark on ready | add `--wait --benchmark` |
| Override a reserved host | add `--force` |
| Pick the 1-node target | add `--head <host>` |
| Tear down one host | `dgx-config teardown --host <host>` |
| Tear down several | `dgx-config teardown --host <host>,<host>` |
| Tear down everything | `dgx-config teardown --all` |
| Container logs | `dgx-config logs --host <host> --tail 50` |
| Authorize an SSH key | `dgx-config authorize-key --key ~/.ssh/id_ed25519.pub` |
| Inspect JIT caches (read-only) | `dgx-config cache-inventory` |
| Reclaim JIT cache space | `dgx-config prune-cache --min-free-gb 50 --headroom-gb 20 --dry-run` |
| Review cached weights (read-only) | `dgx-config list-cached-models` |
| Clear one model's weights cache | `dgx-config flush-model-cache --model <key> --dry-run` |
| Inspect IPC segments (read-only) | `dgx-config ipc-inventory` |
| Sweep orphaned IPC segments | `dgx-config sweep-ipc-orphans --dry-run` |
| Reclaim crash-log space | `dgx-config prune-ray-logs --retention-days 7 --dry-run` |
| Repair a ledger undercount | `dgx-config correct-ledger --dry-run` |

Notes on the less obvious ones:

- **`cache-inventory`** reports entry counts, sizes, age and LRU order for every
  JIT cache root (Triton, TileLang, DeepGEMM, vLLM, FlashInfer) plus the CUDA
  compute cache and HF weights cache, per host. Fully read-only — safe against a
  live cluster at any time.
- **`prune-cache`** evicts whole cache entries oldest-first, and only on a host
  currently below `--min-free-gb`. Above that floor nothing is touched. It never
  deletes an individual file *within* an entry — a partially-deleted
  Triton/TileLang entry can leave a state the loader treats as a hit and then
  fails to load.
- **`list-cached-models`** cross-references cached weights against the live
  catalog and deploy history, surfacing active, retired and orphaned caches
  worth reviewing. Read-only.
- **`flush-model-cache --model <key>`** clears one model's HuggingFace weights
  cache, to recover from a corrupt download or reclaim space for a retired
  model. Accepts a catalog key (live or historically recorded) or a raw HF
  repo id as a last resort. `--jit` also wipes **all** JIT and compute caches on
  the target hosts — every model's compiled kernels, not just this one's. It
  checks whether the model appears loaded before deleting, but that check is
  documented as unreliable on 2-node Ray deploys, so `--dry-run` first.
- **`sweep-ipc-orphans`** only removes SysV shared-memory segments with a
  kernel-tracked `nattch == 0`, so it can't touch anything still in use. It runs
  automatically as part of every deploy's pre-deploy teardown; this command is
  for ad-hoc inspection between deploys.
- **`authorize-key`** appends a public key to `~/.ssh/authorized_keys` on every
  configured host — following `cluster_config.yaml`, not a hardcoded pair.
  Rarely needed day-to-day; it's for onboarding an admin identity directly to
  the Spark hosts, separate from your Tailscale access to `maestro`.
- **`correct-ledger`** with no arguments auto-detects the serving
  model/topology and live `/metrics` values and previews the correction. It
  refuses to overwrite with a smaller value than what's recorded unless you pass
  `--force`, and backs up the ledger (timestamped) before any real write.

### Credentials

The HuggingFace token lives in `~/docker/orchestrator/.secrets` on `maestro` as
`HF_TOKEN="..."`, or as an exported `HF_TOKEN` in your shell. Resolution order
and the alternatives are in [`INSTALL.md`](INSTALL.md).

**That file exists on `maestro` only.** The Spark hosts have no
`~/docker/orchestrator` directory at all. If you need a token in a command run
directly on a Spark host — a manual `docker run` for weight staging, say — copy
the value across explicitly. The path will not resolve.

---

## Reserved hosts

A host is marked `reserved: true` in `cluster_config.yaml` when it carries
something that must not be disturbed by routine work: a resident agent, a shared
endpoint the team depends on. **Deploy and teardown both refuse to touch a
reserved host unless you explicitly override.**

Being reserved is a property of the **machine**, not of whatever happens to be
deployed on it — unlike `role:`, which describes a host's part in one particular
topology.

How you override depends on the surface:

- **CLI, non-interactive** — pass `--force` to `deploy` or `teardown`. The
  refusal names the host and what is recorded as running on it, so you can see
  what you're about to destroy before deciding.
- **CLI, `dgx-config menu`** — a refusal prints that same server message and
  asks a plain y/N before retrying with the override. You don't need to already
  know to drop to the non-interactive form. TOMBSTONES #135.
- **Dashboard** — nothing is armed in advance. Click Deploy or Teardown
  normally; a dialog shows the server's refusal message and requires a
  confirmation tick before **Proceed anyway** enables. Cancel, click the
  backdrop, or press Escape to back out.

**There is deliberately no persistent "force" checkbox or flag on any surface.**
A checkbox holds state — ticked once for a legitimate override and then
forgotten, it would silently skip the guard on a later deploy, possibly for
someone else at the same always-on dashboard. Every override holds for exactly
one request and cannot outlive it. TOMBSTONES #131.

Two things reserving a host does **not** do:

- **It does not make 2-node deploys safe.** A 2-node deploy always spans both
  hosts, so it needs the override and will take the reserved host's service
  down. That's intended — a decision, not a surprise. The dashboard warns about
  this under the topology selector before you click.
- **It does not cover raw `docker run` over SSH**, which bypasses the
  orchestrator entirely and never sees the guard. `tests/ab_test.py` does its
  own check for that reason; anything hand-rolled won't.

`--dry-run` is **not** exempt. It reports what a real deploy would do, so it
reports the refusal a real deploy would hit rather than printing a command that
would in fact be blocked.

### If you change which host is reserved

**Update `default_deploy_target` in the same edit.** Leaving it pointed at the
now-reserved host is self-contradicting — that field exists precisely so the
reserved node is never also the unqualified-deploy fallback — and
`common/config.py` rejects it at load. That takes the daemon down at startup
rather than merely misbehaving, and the dashboard just shows "API disconnected."
The actual reason is in `docker logs dgx-orchestrator-api`. TOMBSTONES #142.

**`HOSTS` / `PRIMARY_HOST` / `RESERVED_HOSTS` are computed once at daemon
start.** No `cluster_config.yaml` edit takes effect until the
`dgx-orchestrator-api` container restarts. If a host-order or reserved-flag
change doesn't seem to be respected, confirm the daemon actually restarted
before assuming the config is wrong.

Note that `hosts:` order is load-bearing beyond this: the first entry is
`PRIMARY_HOST`, the 2-node Ray head, and the host whose `/metrics` feed session
tracking.

---

## Deploying

```bash
dgx-config deploy --model <key> --nodes 2
```

**Where it lands if you don't say.** A 1-node deploy with no `--head` goes to
`cluster_config.yaml`'s `default_deploy_target` — the scratch node. A 2-node
deploy puts the head on the first host listed under `hosts:` regardless, so the
head stays consistent across topologies. `--head <host>` overrides the 1-node
case. Add `--force` if the target is reserved and you mean it.

The dashboard's Target dropdown follows the same default and is only meaningful
for a **1-node** deploy. For 2-node the head is always `PRIMARY_HOST` or your
explicit `--head`, and as of 2026-09-07 the dashboard no longer sends whatever
the hidden dropdown happened to still hold. If you have older notes about 2-node
deploys landing on the wrong node with no error, that was this — TOMBSTONES
#134.

### Some recipes need more than `hf_path` / `image` / `vllm_args`

Two schema fields exist for images that don't follow this cluster's conventions.
When a recipe sets them they are **prerequisites, not decoration**:

- **`entrypoint: ""`** (with the quotes) neutralizes an image's own ENTRYPOINT.
  Required for anything built on the official `vllm/vllm-openai` base, which
  sets `ENTRYPOINT ["vllm","serve"]` — Docker *appends* the orchestrator's argv
  to that rather than replacing it, and the resulting error names a flag that
  has nothing to do with the real problem. A bare `entrypoint:` parses as null,
  i.e. as if absent; the quotes matter.
- **`model_path_override:` / `extra_mounts:`** appear when an image's
  model-loading code needs a real local directory rather than an HF repo id. If
  a recipe's header names a `huggingface-cli download ... --local-dir` step, run
  it on **every** target host before deploying. Nothing pre-flights it, and
  skipping it fails with a `FileNotFoundError` inside the container.

Both are TOMBSTONES #133.

A third, `launch_argv_prefix:`, selects the entry point itself and matters only
for multi-node workers — see the `collective_rpc` entry under
[Troubleshooting](#troubleshooting).

### Previewing a deploy (`--dry-run`)

```bash
dgx-config deploy --model <key> --nodes 2 --dry-run
```

Builds and prints the exact `docker run` command(s) the real deploy would send —
every flag, every environment variable, every mount — **without opening a single
SSH connection or touching either host**. The fastest way to confirm a recipe
does what you expect, or to compare two recipes' generated commands side by side,
with zero risk to a running cluster.

**Dry-run output is safe to paste.** Credential values are masked before they
reach the response (`HF_TOKEN=***MASKED***`), so you don't have to trim it by
hand. The variable *name* is kept so you can see which credentials a deploy
expects, and an *empty* value is left as-is — `HF_TOKEN=` means the token
genuinely wasn't found, which is worth seeing. The response leads with
`orchestrator_version`, so you can confirm the daemon is running the code you
think it is without checking the badge separately. TOMBSTONES #138.

### GPU utilisation ceiling

`cluster_config.yaml` declares `gpu_util_ceiling` and
`gpu_util_ceiling_enforce`. **Enforcement is currently `off`** — exactly how
this behaved before 2026-09-08, when the ceiling was declared, required, and
read by nothing. Leaving it off is deliberate; the machinery exists so that
turning it on can be a decision.

A few recipes sit above the ceiling and carry `gpu_util_ceiling_exempt: true`.
Those are validated values, not oversights — the ceiling is held conservative
for the catalog as a whole rather than raised to match its highest member. See
[`MODELS.md`](MODELS.md) for which ones.

Deploying an exempt recipe prints a one-line **informational** note every time.
That is intentional and not a warning: it would fire on every deploy of those
recipes forever, and a warning that always fires is a warning nobody reads. A
recipe over the ceiling *without* the exemption is the case worth noticing, and
it warns or refuses depending on `gpu_util_ceiling_enforce`.

**Nothing ever clamps.** A recipe asking for more than the ceiling is either
permitted or refused — never quietly served a different number than it asked
for. TOMBSTONES #139.

---

## Adding a new model

Each model is its own recipe file under `recipes/local/`.

**1. Create `recipes/local/your-model-name.yaml`.** The filename is the model's
identity — exactly what you pass to `--model` and exactly what appears in the
dashboard dropdown. There is no separate internal name field to keep in sync
(an earlier schema had one; it caused a real outage when it drifted).

Prefix the filename with `_` if the recipe is experimental or under test. That
prefix is the only signal a dashboard user gets.

**Pick a name that still makes sense next to its siblings.** A fast-growing
catalog has repeatedly produced near-duplicate names differing only by a suffix,
easy to confuse or select by mistake. If you're adding a variant — a different
precision, a longer-context build, a tuning change — make the distinguishing
part unambiguous rather than typo-adjacent.

**2. Fill in the required fields:**

```yaml
recipe_version: '1'
hf_path: org/Your-Model-Name
gpu_util: 0.70
topologies:
  1_node:
    max_model_len: 32768
    tp_size: 1
    pp_size: 1
    env_vars:
      - OMP_NUM_THREADS=16
      - VLLM_CPU_OMP_THREADS=16
    vllm_args: >-
      --trust-remote-code --kv-cache-dtype fp8
```

Define only the topologies the model actually supports. A model needing both
Sparks' memory should have only a `2_node` block — `--nodes 1` then fails with a
clear error instead of misbehaving quietly.

`common/recipes.py` is the authoritative schema; the optional fields are
summarised in [`../README.md`](../README.md).

**3. Check `errata.yaml` before reusing another recipe's `vllm_args` as a
starting point.** Several real incidents came from a copied block carrying a
flag combination that had never been validated, and as of 2026-09-09 four
recipes in the catalog were violating `enforce: error` rules that had each
been fixed elsewhere at least once. Nothing enforces the file yet, so
reading it is the enforcement. The ones most often copied wrongly: **E005**
(MTP speculative decoding cannot combine with `pp_size > 1` — use TP or one
node), **E023** (never pass `--speculative-model` as its own flag; the
drafter goes in `--speculative-config`'s `model` key), **E024**
(`--moe-backend marlin` is NVFP4-only and is refused on a mixed
FP8/NVFP4 checkpoint), and **E025** (DeepSeek-V4 on a eugr image needs
`--moe-backend flashinfer_cutlass`, and inside `--speculative-config` too
when speculative decoding is on — but *not* on the hazyumps GB10 image,
which selects correctly by itself).

**4. Set `image:`** only if the model needs a non-default container. Omitting it
falls back to the cluster's `default_image` — which, for a 2-node topology,
means the fallback image must ship Ray. This has bitten four times (E003,
E004; TOMBSTONES #43, #103).

**`image:` is recipe-level, not per-topology**, and deliberately so: an
ENTRYPOINT is a property of an image, and letting two topologies of one
recipe disagree about it would describe an image that doesn't exist. The
practical consequence is that adding `image:` to fix a 2-node topology also
changes what 1-node runs, and resets **both** topologies' launch history —
`build_config_payload()` includes `image`, and the hash is computed per
(recipe, topology). Verify a library version rather than inferring it from
a tag while you're there: `eugr/spark-vllm-b12x:latest` ships transformers
5.15.0 despite eugr's `--tf5` build-flag naming, and
`docker run --rm <image> pip show transformers` settles it in a second.

**5. Confirm it loaded**, then preview:

```bash
dgx-config status
dgx-config deploy --model your-model-name --nodes 1 --dry-run
```

If the model doesn't appear, or the **entire** dropdown looks empty rather than
just your addition, see "The catalog looks empty" under Troubleshooting.

**6. Record it in [`MODELS.md`](MODELS.md)** — context, concurrency, arch, spec
decode method and depth. A recipe with no row there is invisible to everyone who
didn't write it.

---

## Teardown

```bash
dgx-config teardown --host <host>
dgx-config teardown --host <host>,<host>
dgx-config teardown --all
```

**Teardown requires an explicit scope.** A bare `dgx-config teardown` exits with
a usage error rather than clearing the cluster — destroying every runtime should
be something you typed on purpose, not something you got by default. An
unrecognised host name is an error listing the valid names, not a silent no-op
reporting a clean teardown of nothing. Add `--force` if the scope includes a
reserved host, which `--all` always does.

**Graceful, not instant.** It sends SIGTERM to the engine and Ray inside each
container, then `docker stop` with a grace period, and only falls back to
`docker rm -f` for anything still standing. This protects in-progress JIT
compiles from corruption, which a hard kill can leave half-written. Expect up to
roughly a minute on a 2-node cluster; the dashboard button shows live phase
progress throughout rather than a static label. Its final step sweeps orphaned
shared-memory segments left behind by Ray and vLLM on the targeted hosts.

While a teardown or deploy is in flight, the other dashboard controls lock to
prevent starting a conflicting operation mid-flight.

Scoping to one host leaves the other host's session tracking alone — session
stats and the model ledger keep accruing for whatever is still serving.

---

## Offline and air-gapped operation

**1. Pre-cache assets** before disconnecting:

```bash
cd ~/docker/orchestrator
python3 cache_cluster_assets.py
```

It reads the live recipe catalog, collects every distinct `image:` plus
`default_image`, and pulls each one on each host — then downloads each recipe's
`hf_path` **through that recipe's own image**, rather than one guessed image for
everything, so the download runs in the same Python/CUDA environment the model
will serve in. Work is **serial**: one image or repo on one host at a time, with
a 30-minute timeout per pull and 60 minutes per checkpoint. A full cold cache
takes hours, not minutes. Plan accordingly.

Two gaps worth knowing before you rely on it:

> **Speculative-decoding drafters are not fetched.** The script finds draft
> models by regexing `--speculative-model` out of `vllm_args`. Every working
> spec-decode recipe in the catalog declares its drafter inside
> `--speculative-config`'s JSON instead, so those checkpoints — the DFlash and
> MTP drafters — are missed entirely and the offline deploy fails at load.
> Download them by hand.

> **`extra_mounts` is not covered.** A recipe staging weights at a local path
> outside the shared HF cache mount is invisible to this script. Stage those per
> the recipe's own instructions.

Also note the script hardcodes the SSH user `tetrel` and the cache mount
`/home/tetrel/.cache/huggingface:/root/.cache/huggingface` rather than reading
`ssh_user` and each host's `volume_mount` from `cluster_config.yaml`. On this
cluster those agree; on a differently-configured one they would not.

**2. Toggle offline mode** via the dashboard's network-mode badge. This injects
`HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` into every **new** container
deployment from that point on. It does not retroactively affect an
already-running container.

---

## Benchmarking

Against an already-deployed, healthy model:

```bash
cd ~/docker/orchestrator
python3 benchmark.py --host <host-or-ip> --nodes <1|2> --model-key <recipe-key>
```

Or have a deploy trigger one once it's confirmed healthy:

```bash
dgx-config deploy --model <key> --nodes 1 --wait --benchmark
```

**Pass `--host` explicitly.** Its default is the hardcoded IP `10.0.14.43`, not
the host currently serving. A bare `python3 benchmark.py` benchmarks whatever
happens to be on that one address, or fails against nothing. The dashboard's
button gets the target right by construction (TOMBSTONES #66, #141); the
standalone script does not.

**Pass `--model-key`** — the recipe filename stem. Without it the ledger logs
the served model id instead, and `enrich_catalog()`'s `historical_tps` lookup
can't join the ledger back to the catalog, because those are different strings.
This has silently broken lookups more than once (TOMBSTONES #53), most recently
for the DSpark validation run.

**Pass `--nodes`.** It is a **ledger label only** — nothing functional depends
on it. Forget it on a 1-node run and the row is recorded as 2-node, permanently
and invisibly.

Other flags: `--port` (default 8000), `--max-tokens` (256), `--temperature`
(0.0), `--prompt`.

### What it does

Three passes. Run 1 is labelled cold, runs 2–3 are averaged as warm. It reports
cold TTFT and decode tok/s, plus the **mean** of the two warm runs. Token counts
come from vLLM's `stream_options` usage payload rather than counting chunks, so
MTP's multi-token chunks don't corrupt the arithmetic. First-token detection
covers `content`, `reasoning_content` **and** `reasoning` deltas — without all
three, reasoning models report `decode_tps` of 0.0 (TOMBSTONES #52).

**It writes only `benchmark_ledger.csv`** — Timestamp, Model, Warm_Decode_TPS,
Warm_TTFT_Sec, Nodes. It deliberately does **not** write
`benchmark_results.txt`; the orchestrator's `_run_benchmark_worker()` owns that
file and captures the full stdout including the per-pass lines. Two writers on
one path meant the loser silently won.

**Don't trust `benchmark_results.txt` to be current.** It's written only on the
orchestrator path, only on success — a failed run leaves the previous file in
place untouched — and the file contains no date anywhere, only the model id and
temperature. A stale result from a different model reads exactly like a fresh
one. The ledger CSV has timestamps; that file doesn't.

### Reading the result honestly

**TTFT and decode are separate signals — do not read one as evidence about
the other.** TTFT warms in stages over the first few requests after a
deploy (gemma-4-31b: 1.10 s → 0.30 s → 0.17 s) and then holds at the floor
across every later invocation, so the warmup is once per deploy, not once
per benchmark. **Decode speed is completely insensitive to that warmup.** A
slow first TTFT on a fresh deploy is expected and says nothing about
throughput; a slow decode is a real finding at any TTFT.

**Run it twice and record the range, not one number.** `benchmark.py` gives you
a mean of two warm passes, which reads as more settled than it is: two numbers
were nearly written into recipe headers as findings and neither survived a
repeat (TOMBSTONES #143). `tests/ab_test.py --variant-a <recipe> --repeats 3`
does this properly — independent redeploys, and an aggregate block printing `n`,
mean, range and every value.

**Do not read `Avg generation throughput` as throughput.** vLLM's
`loggers.py` prints that line periodically and it is tempting, because it
appears without you asking. It is a ~10-second window average diluted by
whatever idle and wait gaps fell inside the window, so it understates a
model that is serving intermittently and means very little either way.
`benchmark.py`'s `decode_tps` is measured strictly first-token-to-last and
is the number to compare.

**`--temperature 0.0` is greedy**, which matches vLLM's default verification
behaviour for speculative decoding — a draft token is accepted only if it
matches the target's argmax. If a recipe's spec config uses
`draft_sample_method: probabilistic`, raise the temperature to benchmark
acceptance under the regime the deployment actually uses instead of always
testing greedy.

**What it does not measure.** The default prompt is **one general-prose
request**, so it cannot see workload-dependent differences — DFlash beats MTP by
a wide margin on extraction and merely ties on prose, and this benchmark only
measures the tie. Use `--prompt`, or `tests/ab_test.py --prompts all`, to see
past that. It also **does not exercise tool-calling at all**, so a tools-enabled
recipe benchmarking fine says nothing about whether its parser works.

**If an auto-benchmark didn't fire,** check `benchmark_ledger.csv` for a row
near that deploy's timestamp before assuming the feature is broken. Two distinct
known causes: the checkbox was ticked after Deploy was already clicked
(expected — see the dashboard section), or `wait_for_cluster_ready()`'s
`tuning.deploy_wait_timeout_sec` budget expired silently on a slow boot, which
skips the trigger with no error anywhere. Either way, the manual **Run Benchmark
Suite Now** button gets you the numbers once the dashboard shows ready.

**How fast should this model be?** [`MODELS.md`](MODELS.md) has every measured
number from this cluster with ranges rather than means, plus what explains the
spread between the fastest and slowest recipes. Consult it before concluding a
model is misconfigured — a dense unquantized model at single-digit tok/s is the
correct number on this hardware, not a fault.

---

## A/B testing two recipes

`tests/ab_test.py` runs the whole deploy → health-check → boot-log scan →
benchmark sweep → teardown cycle itself, for one or two variants, logging
everything to `tests/logs/` regardless of pass or fail.

```bash
python3 tests/ab_test.py --variant-a <recipe> --variant-b <recipe> --prompts all
```

Either side can be an existing catalog recipe (deployed exactly as it is on
disk — no scratch file, full mods pipeline, sharing that recipe's ledger history
with a normal deploy), that recipe with fields overridden, a fully ad-hoc
config, or a raw `docker run`. `--variant-b` is optional; give only
`--variant-a` to profile one recipe.

Three things worth knowing before you start:

- **`--repeats N` is how you get a range.** Each repeat is a fully independent
  deploy, benchmark and teardown, and the aggregate block prints `n=`, `mean=`,
  `range=min-max` and every individual value, per prompt. This is the tool for
  the two-runs-minimum rule — `benchmark.py` on its own can only give you a
  single warm mean.
- **Prefer a recipe over `--a-entrypoint`.** If an image doesn't use the stock
  vLLM entry point, set `entrypoint:` and `launch_argv_prefix:` in a recipe and
  point the variant at that. The script's own docstring still says such an image
  "can only ever go through the raw-docker-run path... permanently, until/unless
  the recipe schema itself grows an entrypoint field." **The schema grew that
  field**, and `glm-5_3-flash-nvfp4-mtp` runs on the normal path. Raw-docker
  silently disables mods, forbids `--a-nodes 2`, and bypasses the reserved-host
  guard, so it is a worse path, not an equivalent one.
- **`--host` refuses a reserved host outright, with no `--force` of its own** —
  deliberately, so that forcing onto a reserved node stays a explicit
  `dgx-config deploy --force` rather than something that ends up saved in a
  benchmark invocation. A `--a-nodes 2` run spans both hosts and is refused one
  layer down by the orchestrator for the same reason. If both hosts are needed
  and one is reserved, fall back to sequential manual deploy-and-benchmark.

Everything it runs is captured whether it passes or fails: a full stdout **and
stderr** transcript to `tests/logs/run-<timestamp>.log`, plus each variant's
container log saved before teardown.

**Ledger note.** Benchmarks from this harness log under
`<model_key>-<prompt-name>` — `qwen-3.6-35b-a3b-nvfp4-coding`, not
`qwen-3.6-35b-a3b-nvfp4`. That keeps per-prompt rows distinguishable, but it
means these rows do **not** join back to the catalog for `historical_tps`, and
the docstring's claim that a catalog passthrough "shares its `historical_tps`
ledger entry with normal dashboard deploys" holds for the deploy, not for the
benchmark row.

Full flag reference and worked examples: [`AB_TEST_USAGE.md`](AB_TEST_USAGE.md) —
noting that document has carried at least one claim past its expiry, so prefer
the script's `--help` where they disagree.

---

## Troubleshooting

**A host shows OFFLINE or UNREACHABLE.** Docker does not start automatically
after a reboot — a deliberate safeguard against boot-looping crashed GPUs. SSH
in and start it:

```bash
ssh tetrel@<host-ip> "sudo systemctl start docker"
```

**"Out of memory" on a deploy that fails instantly.** Usually the previous model
wasn't cleaned up. Tear down with an explicit scope, then retry.

**A model shows CRASHED or ORPHANED.** `CRASHED` means the engine process died —
check that host's logs, where the error is usually near the very end.
`ORPHANED` means a worker's head counterpart crashed, leaving the worker alive
with nothing to serve; it needs a teardown, not a wait.

In a 2-node deploy, Docker reporting a container as "running" does **not** mean
the engine inside is alive — the container stays up running the cluster
coordination process. The status logic accounts for this by reading container
logs rather than trusting container state, but if something looks stuck in a
loading state far past its estimate with no error shown, read the logs directly.

**A deploy seems stuck on COMPILING KERNELS.** Expected on a genuinely fresh
container — Triton and CUDA JIT-compile on first run. If it's slow *every* time
rather than once, the persistent JIT cache mount may not be set up on that host.
Run `dgx-config cache-inventory` to see what's actually cached before assuming
something is broken; retrying doesn't help if the mount is the problem.

**The catalog looks completely empty** (not just missing one model). A single
malformed recipe file takes down the **entire** catalog, because the loader
fails closed rather than skipping the bad file. Most commonly a YAML syntax
error or a topology block missing `max_model_len`, `tp_size` or `pp_size`. If a
recipe was recently added or edited, remove it and confirm the catalog returns
before looking elsewhere. `dgx-config status` sometimes surfaces more detail
than the dashboard does.

**The dashboard is frozen — same numbers for a long time.** A stale backend
computation is possible, not just a slow poll. Check `stale` and
`stale_for_seconds` in `/api/status` before assuming it'll clear itself — those
fields exist because `get_cluster_status()` once served a frozen snapshot
indefinitely with no indication (TOMBSTONES #75). The multi-hour freezes of
2026-08 were a `SessionTracker` lock self-deadlocking on its own reentrant flush
path (TOMBSTONES #76).

**A 2-node deploy loads weights for ~10 minutes and then dies with
`collective_rpc should not be called on follower node`.** The worker is on the
wrong entry point. `vllm serve` honours `--headless`;
`python3 -m vllm.entrypoints.openai.api_server` parses it and ignores it, so the
worker starts an API server it should not have. The recipe needs
`launch_argv_prefix: ["vllm", "serve", "{model}"]`. **This is the same error
string as TOMBSTONES #43, which was a different cause with a different fix** (the
Ray flag) — that fix does not apply here. TOMBSTONES #140.

**A 2-node deploy dies on the worker immediately after Ray starts, with a
Ray version mismatch.** `RuntimeError: Version mismatch: The cluster was
started with ... Ray: 2.58.0 ... This process ... Ray: 2.57.0`. This is not
a recipe problem. `docker run` only auto-pulls when a tag is absent
entirely, so a stale image already cached under the same `:latest` name is
never refreshed — and the two hosts can silently end up on genuinely
different builds under an identical tag. Check both:

```bash
ssh tetrel@<host-ip> "docker run --rm <image> python3 -c 'import ray; print(ray.__version__)'"
```

Then `docker pull <image>` by hand on whichever host is behind. TOMBSTONES
#106 fixed `ab_test.py`'s pre-pull to target every host a 2-node deploy
will use, but any path that pulls on only one side can reintroduce this —
including the habit of SSHing into whichever host comes to mind first.

**Two things that look like failures and are not.** A DSpark deploy pays a
JIT tax on its first requests — kernels compiling mid-inference rather than
during warmup — so early throughput reads below steady state. And
`shm_broadcast.py:801 No available shared memory broadcast block found in
60 seconds`, logged twice during a Gemma-4-31b boot, is runtime FP8
quantization of a ~59 GB BF16 checkpoint still running when the queue
polled; it did not recur once serving started. **That one is only benign at
boot** — if it ever appears during active serving, it is worth chasing.

**A community-image recipe fails at startup with an error that doesn't match its
own flags** — an unrelated-looking argparse error, or a `FileNotFoundError` for
a path that should exist. Check whether the image is built on the official
`vllm/vllm-openai` base, or has custom model-loading code: these need
`entrypoint: ""` and `model_path_override` / `extra_mounts` respectively.
TOMBSTONES #133.

**"This exact configuration has not been confirmed to launch successfully yet"
on a recipe you've definitely launched.** If the launches were all from the CLI,
or all onto a non-first-listed host, the marker was genuinely never recorded — a
real bug fixed 2026-09-06, so history written before that date is incomplete
rather than merely sparse. Deploy once more and it populates. TOMBSTONES #129.

Separately, the config-hash schema has been bumped several times as fields that
change what actually launches were added to it. Every bump orphans existing
launch history and every recipe reverts to showing as never-launched. Harmless,
and expected after a schema change.

**Dashboard session speed looks wrong while two models are serving.** Only one
host is tracked at a time. The numbers `benchmark.py` reports are unaffected.

**A deploy or teardown is refused with "marked reserved".** Working as intended —
see [Reserved hosts](#reserved-hosts). Confirm in the dialog, the menu's y/N
prompt, or with `--force` if you mean it; or scope to the other host.

**"User ID / Auditor" isn't an identity check.** Both the dashboard field and the
CLI's attribution are self-reported labels. Your Tailscale SSH session to
`maestro` is verified and audited; per-deploy attribution beyond that is on the
honour system.
