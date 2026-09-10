#!/usr/bin/env python3
"""
Verify _discover_host_container()'s model-name parsing across BOTH calling
conventions.

    python3 -m vllm.entrypoints.openai.api_server --model <path>
    vllm serve <path>                                    (positional)

The second arrived with launch_argv_prefix (schema 5, TOMBSTONES #140).
Every parse branch previously gated on the literal string "--model", so a
positional recipe matched none of them and every host reported the
"Active Container" placeholder -- which is what _resolve_catalog_key()
consumes, so the dashboard name and any fuzzy-resolved ledger key degraded
together.

Section 4 is the regression guard: the exact argv from the GLM-5.3 deploy
that produced the placeholder, asserted to parse now.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _repo import orchestrator_source
SRC = orchestrator_source()

# Extract the nested helper by source, with the same stubs it needs.
start = SRC.index('        def _model_from_argv(parts: list) -> str:')
end = SRC.index('        inspect_res = run_ssh(', start)
import textwrap
body = textwrap.dedent(SRC[start:end])
ns = {}
exec(compile(body, "<helper>", "exec"), ns)
parse = ns["_model_from_argv"]

FAIL = []
def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   {detail}" if not cond else ""))
    if not cond:
        FAIL.append(label)


print("\n[1] --model form (every recipe before schema 5)")
argv = ["python3", "-m", "vllm.entrypoints.openai.api_server",
        "--model", "RedHatAI/GLM-5.3-Flash-NVFP4",
        "--tensor-parallel-size", "2"]
check("basename extracted", parse(argv) == "GLM-5.3-Flash-NVFP4", repr(parse(argv)))

print("\n[2] --model form pointing at a container path")
argv = ["python3", "-m", "vllm.entrypoints.openai.api_server",
        "--model", "/models/GLM-5.3-Flash-NVFP4", "--nnodes", "2"]
check("last path segment", parse(argv) == "GLM-5.3-Flash-NVFP4", repr(parse(argv)))

print("\n[3] positional form -- the case that returned the placeholder")
argv = ["vllm", "serve", "/models/GLM-5.3-Flash-NVFP4",
        "--tensor-parallel-size", "2", "--headless"]
check("basename extracted", parse(argv) == "GLM-5.3-Flash-NVFP4", repr(parse(argv)))

print("\n[4] REGRESSION GUARD: the real GLM-5.3 worker argv, verbatim")
real = ["vllm", "serve", "/models/GLM-5.3-Flash-NVFP4",
        "--tensor-parallel-size", "2", "--pipeline-parallel-size", "1",
        "--nnodes", "2", "--node-rank", "1",
        "--master-addr", "10.0.14.41", "--master-port", "29500",
        "--gpu-memory-utilization", "0.85", "--max-model-len", "98304",
        "--headless", "--trust-remote-code", "--kv-cache-dtype", "fp8",
        "--tool-call-parser", "glm47", "--enable-auto-tool-choice",
        "--reasoning-parser", "glm45", "--block-size", "2304",
        "--moe-backend", "marlin", "--max-num-seqs", "2",
        "--speculative-config", '{"method":"mtp","num_speculative_tokens":5}']
got = parse(real)
check("parses to GLM-5.3-Flash-NVFP4", got == "GLM-5.3-Flash-NVFP4", repr(got))
check("NOT the placeholder", got not in ("", "Active Container"), repr(got))

print("\n[5] absolute path to the vllm binary still matches")
check("/usr/local/bin/vllm serve",
      parse(["/usr/local/bin/vllm", "serve", "/models/Foo-Bar"]) == "Foo-Bar")

print("\n[6] a flag immediately after `serve` is NOT taken as the model")
# `vllm serve --model X` is the flag form even though `serve` is present;
# the --model branch must win, and a bare `serve --help` must yield nothing.
check("serve + --model prefers the flag",
      parse(["vllm", "serve", "--model", "/models/Foo-Bar"]) == "Foo-Bar")
check("serve followed by a flag yields nothing",
      parse(["vllm", "serve", "--help"]) == "",
      repr(parse(["vllm", "serve", "--help"])))

print("\n[7] unrelated argv containing the word 'serve' is not mined")
check("no bogus name from a stray 'serve'",
      parse(["ray", "start", "--head", "serve", "--block"]) == "",
      repr(parse(["ray", "start", "--head", "serve", "--block"])))
check("empty argv is safe", parse([]) == "")
check("no model anywhere is safe",
      parse(["ray", "start", "--head", "--port=6379", "--block"]) == "")

print("\n[8] the ps-aux path uses the same helper, so a split() line works")
ps_line = ("root 133 12.0 8.1 ... /usr/local/bin/vllm serve "
           "/models/GLM-5.3-Flash-NVFP4 --tensor-parallel-size 2 --headless")
check("parsed from ps output", parse(ps_line.split()) == "GLM-5.3-Flash-NVFP4",
      repr(parse(ps_line.split())))

print("\n[9] bash -c regex path (2-node ray head) handles both forms")
for label, cmd, want in [
    ("--model", "python3 -m vllm.entrypoints.openai.api_server --model /models/Foo-Bar --tp 2", "Foo-Bar"),
    ("positional", "vllm serve /models/Foo-Bar --tp 2", "Foo-Bar"),
]:
    m = re.search(r'--model\s+([^\s]+)', cmd)
    if m:
        got = m.group(1).split("/")[-1]
    else:
        m2 = re.search(r'\bvllm\s+serve\s+([^\s-][^\s]*)', cmd)
        got = m2.group(1).split("/")[-1] if m2 else ""
    check(f"bash -c {label}", got == want, repr(got))

print("\n" + "=" * 60)
print(f"FAILED: {FAIL}" if FAIL else "ALL CHECKS PASSED")
sys.exit(1 if FAIL else 0)
