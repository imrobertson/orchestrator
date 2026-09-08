This is a quick reference for the DGX Spark cluster.

== Access ==

Go to `http://maestro:5000` or use `dgx-config menu` in the terminal.

=== Dashboard Usage Notes ===

* '''Grace Blackwell Unified Memory:''' GB10 uses LPDDR5x memory shared between the CPU and GPU. Standard memory queries return `[N/A]`, so the dashboard safely reports this as '''Unified / 131072 MB'''. Watch the `GPU: %` metric for compute saturation.
* '''Live Log Routing:''' The log host dropdown is populated from the live host inventory, and starts on the deploy target rather than a fixed node. If a 2-node deployment hangs, switch to the worker — worker logs often contain the actual stack trace for cross-node networking timeouts.
* '''Host cards:''' each host shows a `RESERVED` badge when `cluster_config.yaml` marks it so, next to its ONLINE/OFFLINE badge. Reserved is a property of the machine, not of what happens to be deployed on it.
* '''Model status is now per host.''' Each host's `READY` reflects that host's own `/health`, not the cluster's. Before 2026-09-06 a non-serving host inherited the serving host's readiness, so a still-compiling (or dead) model on the second node displayed as READY — if you are reading old screenshots or old notes, that is why they disagree. See `docs/TOMBSTONES.md` #130.
* '''User ID / Auditor Tracking:''' The input box defaults to `dashboard_user`. Type your identifier here to inject your identity into the Docker execution context. This is self-reported, not authenticated — it helps distinguish who ran what in casual review, but don't treat it as a verified audit trail.
* '''Version Badge:''' The header, next to Server Time (shown in UTC), displays the currently-running orchestrator version. Useful for confirming a fix actually deployed rather than assuming it did.
* '''Auto-run benchmark checkbox only appears when it can still do something.''' The checkbox is only shown while nothing is deployed or loading — that's the sole window where checking it does anything, since its value is captured once, at the moment Deploy is clicked. Once a deploy is sent, the checkbox is replaced by a status line ("Waiting for model to become ready — benchmark will run automatically...") reflecting whatever was actually decided; checking or unchecking anything during that window has no effect on the in-flight launch. See `docs/TOMBSTONES.md` #137.

== Essential Secrets & Key Management ==

To perform operations, your credentials must be configured.

* '''HuggingFace Token:''' Ensure `~/docker/orchestrator/.secrets` exists and contains `HF_TOKEN="your_token"` to prevent authentication errors when pulling gated models. Alternatively, `export HF_TOKEN="your_token"` in your terminal before using the CLI. Note this file lives on `maestro` only — the Spark hosts (`spark-3`/`spark-4`) have no `~/docker/orchestrator` directory at all. If you need a token in a command run directly on a Spark host (e.g. a manual `docker run` for weight-staging), copy the value across explicitly rather than referencing the file path — it won't resolve.
* '''SSH Keys:''' If operating directly on `maestro` (bypassing Tailscale SSO), authorize your personal SSH key once so the orchestrator can reach the Spark hosts on your behalf:

dgx-config authorize-key --key ~/.ssh/id_ed25519.pub


== Reserved Hosts ==

A host can be marked `reserved: true` in `cluster_config.yaml` when it carries something that must not be disturbed by routine work — a resident agent, a shared endpoint the team depends on. '''Deploy and teardown both refuse to touch a reserved host unless you explicitly override.''' How you override depends on which surface you're on:

* '''CLI (non-interactive):''' pass `--force` to `dgx-config deploy`/`dgx-config teardown` directly. The refusal message names the host and what is recorded as running on it, and tells you this.
* '''CLI (`dgx-config menu`):''' hitting a reserved-host refusal from the interactive menu now prints that same server refusal message and asks a plain y/N before retrying with the override — you don't need to already know to drop to the non-interactive `--force` form. Before this, the menu had no path past the refusal at all and just printed the raw error. See `docs/TOMBSTONES.md` #135.
* '''Dashboard:''' you don't arm anything in advance. Click Deploy or Teardown normally; if the operation would disturb a reserved host, a dialog appears showing the server's own refusal message, and you must tick a confirmation box before the '''Proceed anyway''' button enables. Cancel, click the backdrop, or press Escape to back out.

'''There is deliberately no persistent "force" checkbox or flag on any surface.''' A checkbox has state — tick it for one intentional override, forget it, and the next deploy hours later (possibly by someone else) quietly skips the guard too. Every override — dashboard dialog, menu retry, or `--force` — holds for exactly one request and cannot outlive it. See `docs/TOMBSTONES.md` #131 for the full reasoning.

The dashboard's Teardown button now has a '''scope selector''' next to it (`All hosts`, or a single named host). Scoped teardown is the point of this whole feature: cycle the scratch node while a resident model keeps serving on the reserved one. Picking a single non-reserved host means no dialog at all. `All hosts` includes the reserved host by definition, so expect the confirmation every time.

Two things a reserved host does '''not''' protect against:
* '''2-node deploys always span both hosts''', so any 2-node work needs the override and will take the reserved host's service down. That's intended — it should be a decision, not a surprise. The dashboard warns about this under the topology selector before you click, and the confirmation dialog says so explicitly.
* '''Raw `docker run` over SSH bypasses the orchestrator entirely''' and never sees this guard. `tests/ab_test.py` does its own check for that reason; anything hand-rolled won't.

Which host is reserved is a property of the machine, not of any deployment — see the comments in `cluster_config.yaml` itself.

'''If you swap which host is reserved, update `default_deploy_target` in the same edit.''' Leaving it pointed at the now-reserved host is a self-contradicting config — that field exists precisely so the reserved node is never also the unqualified-deploy fallback — and `common/config.py` rejects it at load, which takes the daemon down at startup rather than merely misbehaving. The dashboard just shows "API disconnected"; the actual reason is in `docker logs dgx-orchestrator-api`. Note also that `HOSTS`/`PRIMARY_HOST`/`RESERVED_HOSTS` are computed once at daemon start, so no `cluster_config.yaml` edit takes effect until the container is restarted. If you swap which host is reserved, also update `default_deploy_target` to point at the OTHER host — leaving it pointed at a now-reserved host is a self-contradicting config (the field exists specifically so the reserved/authoritative node is never also the default fallback target) and has been observed to crash the daemon at startup rather than merely misbehave. Confirm via the orchestrator log (`docker logs dgx-orchestrator-api`) after any such change, not just the dashboard's "API disconnected" symptom.

== Deploying ==

Use the dashboard or run `dgx-config deploy --model MODEL --nodes N`.

'''Where it lands if you don't say:''' a 1-node deploy with no `--head` goes to `cluster_config.yaml`'s `default_deploy_target` (the scratch node). A 2-node deploy still puts the head on the first host listed under `hosts:`, so the head node stays the same across topologies. Pass `--head <node>` to override either. Add `--force` if the target is reserved and you mean it.

'''`HOSTS`/`PRIMARY_HOST`/`RESERVED_HOSTS` are computed once, at daemon startup''' — editing `cluster_config.yaml` does not take effect until the `dgx-orchestrator-api` container is restarted. If a host-order or reserved-flag change in the file doesn't seem to be respected, check the daemon actually restarted after the edit before assuming the config itself is wrong.

The dashboard's Target dropdown follows the same default — it is built from the live host inventory and starts on `default_deploy_target`, not on a hardcoded node. Reserved hosts are labelled as such in the dropdown itself. Note that dropdown is only meaningful for a '''1-node''' deploy: for 2-node the head is always `PRIMARY_HOST` (or your explicit `--head`), and as of 2026-09-07 the dashboard no longer sends whatever the hidden dropdown happened to still hold. If you have older notes about 2-node deploys landing on the wrong node with no error, that was this — see `docs/TOMBSTONES.md` #134.

'''Some recipes need more than `hf_path`/`image`/`vllm_args` to launch.''' Two schema fields exist for images that don't follow this cluster's conventions, and when a recipe sets them they are prerequisites, not decoration:
* `entrypoint: ""` (with the quotes) neutralizes an image's own ENTRYPOINT. Required for anything built on the official `vllm/vllm-openai` base — without it the orchestrator's argv is appended to `vllm serve` rather than replacing it, and the resulting error names a flag that has nothing to do with the real problem.
* `model_path_override:` / `extra_mounts:` appear when an image's model-loading code needs a real local directory rather than an HF repo id. If a recipe's header names a `huggingface-cli download ... --local-dir` step, run it on '''every''' target host before deploying — nothing pre-flights it, and skipping it fails inside the container. See `docs/TOMBSTONES.md` #133. Note this dropdown is only meaningful for a '''1-node''' deploy: for 2-node, the head is always `PRIMARY_HOST` (or your explicit `--head`), and the dashboard no longer sends whatever the (hidden, for 2-node) target dropdown happens to hold — see `docs/TOMBSTONES.md` #134 if you're wondering why an older build sometimes put the head on the wrong node with no error.

Not sure a model/topology combo is valid, or want to sanity-check what will actually get sent before committing? Add `--dry-run` — prints the exact `docker run` command(s), no SSH connection made, nothing touched:

dgx-config deploy --model MODEL --nodes N --dry-run

'''Dry-run output is now safe to paste.''' Credential values are masked before they reach the response (`HF_TOKEN=***MASKED***`), so you no longer have to trim it by hand. The variable '''name''' is kept so you can still see which credentials a deploy expects, and an '''empty''' value is left as-is — `HF_TOKEN=` means the token genuinely wasn't found, which is worth seeing. The response also leads with `orchestrator_version`, so you can confirm the daemon is running the code you think it is without checking the badge separately. See `docs/TOMBSTONES.md` #138.

Note that `--dry-run` is '''not''' exempt from the reserved-host check. It reports what a real deploy would do, so it reports the refusal a real deploy would hit rather than printing a command that would in fact be blocked.

=== GPU utilisation ceiling ===

`cluster_config.yaml` declares `gpu_util_ceiling` (0.75) and `gpu_util_ceiling_enforce`. '''Enforcement is currently `off`''', which is exactly how this behaved before 2026-09-08 — the ceiling was declared, required, and read by nothing at all. Leaving it off is deliberate; the machinery exists so that turning it on can be a decision.

Four recipes sit above the ceiling and carry `gpu_util_ceiling_exempt: true`: `_glm-5_3-flash-nvfp4-tp2` (0.85) and the three DeepSeek-V4-Flash recipes (0.80). Those are validated values, not oversights — the ceiling is held at a conservative 0.75 for the catalog as a whole rather than raised to match its highest member.

Deploying one of those prints a one-line '''informational''' note every time. That is intentional and not a warning: it fires on every deploy of those recipes forever, and a warning that always fires is a warning nobody reads. A recipe over the ceiling '''without''' the exemption is the case worth noticing, and it warns or refuses depending on `gpu_util_ceiling_enforce`.

'''Nothing ever clamps.''' A recipe asking for more than the ceiling is either permitted or refused — it is never quietly served a different number than it asked for. See `docs/TOMBSTONES.md` #139.

'''Some recipes need more than `hf_path`/`image`/`vllm_args` to launch correctly.''' Two escape hatches exist in the recipe schema for images that don't fit the usual conventions:
* `entrypoint:` — set to `""` (with the quotes) if the recipe's `notes` or header comments say the image needs its ENTRYPOINT neutralized. This is required for any image built on the official `vllm/vllm-openai` base, which sets `ENTRYPOINT ["vllm","serve"]` — without it, the orchestrator's own argv gets silently appended to that instead of replacing it, and the failure looks unrelated to entrypoints at all (see `docs/TOMBSTONES.md` #133).
* `model_path_override:` / `extra_mounts:` — present on a recipe when its image's model-loading code needs a real local directory rather than an HF repo id. If a recipe's header mentions a `huggingface-cli download ... --local-dir` step, that step is a real prerequisite, not a suggestion — the deploy will fail with a `FileNotFoundError` inside the container if you skip it. Run it on '''every''' target host before deploying; there is currently no automated pre-flight check that catches a missing or partial download (see `docs/TOMBSTONES.md` #133).

=== Topology & Memory Guards ===
When selecting a model in the dashboard, invalid topologies are automatically hidden based on the model's recipe (e.g., hiding 1-Node options for models whose recipe only defines a `2_node` topology) to prevent Out-Of-Memory (OOM) errors. A '''Target''' dropdown, listing hosts from the live inventory, is shown for '''every''' 1-node deploy — including recipes that define only a `1_node` topology and therefore show no topology picker at all. (Until 2026-09-06 it was not: the Target picker was nested inside the row the topology logic hid, so on a single-topology recipe there was no way to choose a host and the deploy silently used `default_deploy_target`. If you hit that, you weren't missing a control — you weren't given one. See `docs/TOMBSTONES.md` #132.)

The '''Select Model''' dropdown follows whatever is running only until you pick a model yourself; after that it stays put until you deploy. The target dropdown behaves the same way — it follows whichever host is currently serving '''only until you pick one yourself''', after which it stays where you put it, and it never auto-selects a reserved host. (Before 2026-09-06 it re-pinned to the serving host on every 4-second poll, which made it effectively unusable once anything resident occupied the first-listed node: your selection reverted within seconds and the deploy went somewhere you didn't choose. If you have older notes telling you to "use the CLI instead" because of this, they're stale — see `docs/TOMBSTONES.md` #131.)

== Air-Gapped & Offline Operations ==

To deploy models without internet connectivity, you must pre-cache the assets and toggle the cluster into Offline Mode.

1. '''Pre-Cache Assets:''' Run the pre-fetcher script to download all Docker images and HuggingFace safetensors to the local NVMe cache on both nodes.

cd ~/docker/orchestrator
python3 cache_cluster_assets.py

Note: as of 2026-09-07, this prefetcher only knows the shared HF cache mount. A recipe using `extra_mounts` to stage weights at a separate local path (see Deploying, above) is '''not''' covered — stage those by hand per that recipe's own instructions, or the offline deploy fails at load.

Note: as of 2026-09-07, this prefetcher only knows about the shared HF cache mount pattern. A recipe using `model_path_override`/`extra_mounts` to stage weights at a separate local directory (see Deploying, above) is NOT covered by this script — stage those manually per that recipe's own instructions.

2. '''Toggle Offline Mode:''' In the Web Dashboard, click the green '''"🌐 ONLINE MODE"''' badge to toggle to '''"🔒 OFFLINE MODE"'''. This injects `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` into all ''new'' deployments, forcing them to load strictly from the local NVMe cache. (Note: This does not affect models that are already running).

== Teardown ==

Pick a scope next to the dashboard's "Teardown Runtimes" button, or run teardown with an explicit scope from the CLI:

dgx-config teardown --host spark-3
dgx-config teardown --host spark-3,spark-4
dgx-config teardown --all

'''There is no argument-free teardown any more.''' A bare `dgx-config teardown` exits with a usage error rather than clearing the cluster. That's deliberate: destroying every runtime should be something you typed on purpose, not something you got by default. An unrecognised host name is also an error listing the valid names, rather than a silent no-op that reports a clean teardown of nothing.

'''Note:''' This is graceful, not instant. It sends SIGTERM to the engine and Ray inside each container, then `docker stop` (with a grace period), and only falls back to `docker rm -f` for anything still standing — this protects in-progress JIT compiles from corruption, which a hard kill can leave half-written. Expect it to take up to roughly a minute on a 2-node cluster; the dashboard's Teardown button shows live phase progress for the duration rather than a static "in progress" label. It also sweeps orphaned shared-memory segments left behind by Ray/vLLM on the targeted hosts as its final step. While teardown (or a deploy) is running, the other dashboard controls lock to prevent a conflicting operation starting mid-flight.

Tearing down one host leaves the other host's session tracking alone — the dashboard's session stats and the model ledger keep accruing for whatever is still serving, rather than resetting as they used to.

== Performance Benchmarking ==

Validate real-world token throughput and latency using the integrated benchmark tool.

cd ~/docker/orchestrator
python3 benchmark.py

All runs are automatically appended to `benchmark_ledger.csv` with precise Time-To-First-Token (TTFT) and Decode speed metrics. The tool is hardened against vLLM Multi-Token Prediction (MTP) stream buffering to ensure mathematical accuracy.

'''If a deploy's auto-benchmark didn't fire,''' check `benchmark_ledger.csv` for a row near that deploy's timestamp before assuming the feature is broken. Two distinct known causes: the checkbox was ticked after Deploy was already clicked (expected — see Dashboard Usage Notes), or `wait_for_cluster_ready()`'s 900s budget expired silently on a slow boot, which skips the trigger with no error anywhere. Either way the manual '''Run Benchmark Suite Now''' button gets you the numbers once the dashboard shows ready.

'''If a deploy's "auto-run benchmark when ready" doesn't seem to have fired,''' check `benchmark_ledger.csv` for an entry near the deploy's timestamp before assuming the feature is broken. Two known, distinct causes: (1) the checkbox was checked after the deploy was already sent — see the Dashboard Usage Notes above, this is expected behavior, not a bug; (2) `wait_for_cluster_ready()`'s timeout (`tuning.deploy_wait_timeout_sec`, 900s by default) can expire silently if a model takes longer than that to boot, skipping the benchmark trigger with no visible error. If it's (2), the manual "Run Benchmark Suite Now" button (appears once the dashboard shows the cluster as ready) gets you the numbers regardless.

== A/B Testing Two Recipes ==

Skip the manual deploy/benchmark/teardown cycle — `tests/ab_test.py` does the whole thing for one or two variants automatically, logs everything regardless of pass/fail, and prints a side-by-side comparison if you gave it two:

python3 tests/ab_test.py --variant-a recipe-one --variant-b recipe-two --prompts all

Either side can be an existing recipe name, an existing recipe with fields overridden, or a fully ad-hoc config — see `docs/AB_TEST_USAGE.md` for the full reference. Two things worth knowing up front: an image with a non-default entrypoint (needs `--a-entrypoint`) can't go through mods, and can't use `--a-nodes 2` — that's a structural limit of the deploy path, not a flag you're missing. And `--host` now defaults to the scratch node and refuses a reserved host outright, with no `--force` of its own.

== If something looks wrong ==

* '''Model dropdown totally empty''' (not just missing one model): a single malformed recipe file can currently break the whole catalog, not just itself. Worth flagging rather than assuming it's just slow to load — see `USERMANUAL.md`'s Troubleshooting section.
* '''Dashboard frozen — same numbers for a long time:''' a stale backend computation is possible, not just a slow poll. Check `stale`/`stale_for_seconds` at the API level if you can, and flag it rather than assuming it'll clear itself — see `docs/TOMBSTONES.md` #76 for the history.
* '''Teardown or deploy refused with "marked reserved":''' working as intended — see the Reserved Hosts section above. Confirm in the dialog (or the menu's y/N prompt, or `--force`) if you actually mean it, or scope to the other host.
* '''"This exact configuration has not been confirmed to launch successfully yet" on a recipe you've definitely launched:''' if the launches were all from the CLI, or all onto the non-first-listed host, that marker was genuinely never recorded — a real bug, fixed 2026-09-06. Launch history written before that date is incomplete rather than merely sparse. Deploy it once more and the marker will populate. See `docs/TOMBSTONES.md` #129. Separately: the recipe config-hash schema was bumped twice more on 2026-09-07 (entrypoint, then model_path_override/extra_mounts) — every recipe's launch history reset again at that point, for the same reason and just as harmlessly. See `docs/TOMBSTONES.md` #133.
* '''A 2-node deploy loads weights for ~10 minutes and then dies with `collective_rpc should not be called on follower node`:''' the worker is on the wrong entry point. `vllm serve` honours `--headless`; `python3 -m vllm.entrypoints.openai.api_server` parses it and ignores it, so the worker starts an API server it should not have. The recipe needs `launch_argv_prefix: ["vllm", "serve", "{model}"]`. Note this is the same error string as `docs/TOMBSTONES.md` #43, which was a different cause with a different fix (the Ray flag) — that fix does not apply here. See `docs/TOMBSTONES.md` #140.
* '''A community-image recipe fails at container startup with an error that doesn't match its own flags''' (e.g. an unrelated-looking argparse error, or a `FileNotFoundError` inside the container for a path that should exist): check whether the recipe's `image` is built on the official `vllm/vllm-openai` base or has custom model-loading code — these have been confirmed to need `entrypoint: ""` and/or `model_path_override`/`extra_mounts` respectively. See `docs/TOMBSTONES.md` #133 for the full diagnostic story.
* '''Dashboard session speed looks wrong while two models are serving:''' only one host is tracked at a time. See `docs/BACKLOG-session-tracker-multi-model.md` — the numbers `benchmark.py` reports are unaffected.
* '''Everything else:''' `USERMANUAL.md` has the fuller troubleshooting list; this page is deliberately just the fast path.
