> ### Context
>
> Repo `imrobertson/orchestrator`, two-node DGX Spark (GB10, SM121) cluster:
> `spark-3` (10.0.14.41, head, **reserved**) and `spark-4` (10.0.14.43).
>
> `glm-5_3-flash-nvfp4-mtp.yaml` is in production: 2-node TP2, 262,144
> context, **21.3–23.2 tok/s** warm decode across three 3-pass runs, MTP-5
> at ~37% acceptance. That is the baseline to beat.
>
> Upstream measured **46.9 tok/s vs 21.8 for MTP-4 on the same hardware at
> the same context — 2.15x — at 74.1% acceptance**, using DFlash2 block-
> diffusion speculative decoding. Critically it **costs zero KV pool**: the
> drafter's layers slot-share the MLA tensors the way GLM's own mamba
> layers do, so context is unaffected. This is the single largest available
> performance win on this cluster and the reason for this session.
>
> ### What blocks it
>
> The image currently in the recipe
> (`ghcr.io/tonyd2wild/vllm-glm53-flash:sm121-v8`) ships vLLM
> `0.1.dev20051+g487ecf187`, which supports DFlash1 and **predates DFlash2**
> (upstream PR #52816, merged 2026-08-21). So this is not a `vllm_args`
> change. It needs:
>
> - a different image — `radixark/vllm-glm53-flash:sm121-v8-dflash2-tony`
>   is referenced by the TP4 community deploy; tonyd2wild's TP2 repo carries
>   its own overlay. **Establish which is actually right for TP2 before
>   pulling 180 GB of anything.**
> - the drafter `incoai/GLM-5.3-Flash-DFlash2` (~2.2 GB) staged on **both**
>   hosts, with an `extra_mounts` entry for it
> - `--speculative-config` changed from `{"method":"mtp",...}` to the
>   DFlash2 form
>
> The recipe schema should already be sufficient — `entrypoint`,
> `launch_argv_prefix`, `extra_mounts` and `model_path_override` all exist
> and were each added because a community image needed one. If you find
> yourself wanting a sixth field, that is a real finding; say so rather than
> working around it.
>
> ### Do these two checks BEFORE writing any recipe
>
> Both are one command and both would have saved most of a day when this
> image was first brought up (`TOMBSTONES.md` #133):
>
> ```
> docker run --rm --entrypoint "" <image> python3 -c "import ray; print(ray.__version__)"
> docker inspect <image> --format '{{.Config.Entrypoint}}'
> ```
>
> The current image has **no ray** and sets `ENTRYPOINT ["vllm","serve"]`.
> If the DFlash2 image differs on either, the recipe's `entrypoint` and
> `launch_argv_prefix` need to change with it — and note that a multi-node
> **worker** specifically requires the `vllm serve` CLI, because the
> `api_server` module path parses `--headless` and then ignores it, starting
> an APIServer that dies on `collective_rpc` nine minutes into weight
> loading (`TOMBSTONES.md` #140, confirmed by source read at
> `vllm/entrypoints/cli/serve.py:146/177/262`).
>
> ### Constraints that are not negotiable
>
> **Do not raise `--kv-cache-memory` to "fully utilize gpu memory".** vLLM
> suggests ~8 GiB; upstream documents that concurrency is bounded by
> free-memory *headroom* rather than by the pool, and at `4445787956` —
> less than vLLM's suggestion — a 3-way 20K-token prefill drove
> MemAvailable to 3.06 GB and an anti-OOM watchdog killed the engine. Their
> shipping pin is deliberately lower than what fits. The heuristic here
> lands at 2.88 GiB unpinned. DFlash2 should not need more, since it costs
> no KV pool.
>
> **`spark-3` is reserved.** Every 2-node deploy spans both hosts and needs
> `--force`. That is intended, not an obstacle to route around.
>
> **Weight staging is manual and unverified by anything.** Docker silently
> creates a missing bind-mount source as an empty directory rather than
> failing, which produces a `FileNotFoundError` deep in model loading with
> no hint that the mount was the problem. Verify with `ls` on the specific
> file, on both hosts, before deploying.
>
> ### Measurement discipline
>
> **Two runs minimum before any number goes in a recipe header, and record
> the range rather than the mean** (`TOMBSTONES.md` #143, `WORKSTREAMS.md`
> F-o). Two numbers nearly became findings on 2026-09-09 and neither
> survived a repeat — the worse one *confirmed an expectation*, which is the
> kind least likely to get a second run.
>
> `benchmark.py --host 10.0.14.41 --nodes 2` sends **one general-prose
> prompt**. Upstream's 46.9 figure is single-stream; their coding peak is
> higher. If DFlash2's advantage is workload-dependent the way DFlash's was
> for Gemma4 (2.8x on extraction, tied on prose), a prose benchmark will
> understate it. `tests/ab_test.py --prompts all` is the honest comparison
> and MTP-vs-DFlash2 on identical hardware is exactly what it exists for.
>
> Also worth checking, since it is free once the thing is up: **acceptance
> rate**. Upstream reports 74.1% with per-position figures. If yours is far
> off, that is a signal the drafter is loaded but not working properly —
> which is a real failure mode here, not hypothetical.
>
> ### What to report back
>
> Whether it works, at what tok/s **with the range**, at what acceptance
> rate, and at what context. If it does not work, the failure and the
> mechanism — but mark mechanism claims as inferred unless the code path was
> actually read. Four mechanism claims were stated as fact on 2026-09-08 and
> all four were wrong; `WORKSTREAMS.md` F-m has the pattern.
>
> ### Do not
>
> Do not change `glm-5_3-flash-nvfp4-mtp.yaml`. It is in production and
> validated. DFlash2 is a **new recipe file** — the two should be A/B
> comparable, and `-mtp` in the current name exists precisely so a `-dflash2`
> sibling can sit beside it.
>
> Do not delete `deploy_gemma4_dflash.py` as part of this work even though
> it is now retirable; that is a separate, unrelated cleanup.
>
> ### Known upstream caveat worth reading before starting
>
> Their NVFP4-KV port of the same overlay serves and drafts correctly —
> 334,161-token pool, 35.9 tok/s, acceptance 0.563 — **but any prompt
> requiring chunked prefill (>3K tokens) kills the rank-0 worker with no
> traceback, no CUDA error and no OOM entry.** That is on the NVFP4-KV lane,
> not the fp8/marlin lane this cluster runs, so it should not apply. Know it
> exists so a silent rank-0 death is not mistaken for something new.
