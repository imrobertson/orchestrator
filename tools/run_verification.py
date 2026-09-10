#!/usr/bin/env python3
"""
run_verification.py -- read-only verification sweep. Run on maestro.

    cd ~/docker/orchestrator
    python3 run_verification.py

Writes ./verification-report.txt and prints it. Paste the file back.

SAFETY: this script never deploys, never tears down, never writes to the
cluster. Every deploy invocation carries --dry-run, which per
execute_deployment()'s own docstring "never calls run_ssh or mutates any
cluster state". The only SSH is a read-only `docker run --rm ... pip show`
in section 6, and that is skipped unless you pass --allow-ssh.

Credentials: dry-run output is masked at the source (TOMBSTONES #138), and
this script additionally scrubs anything that looks like a token before
writing. Both, because #94 is what happens when you trust one layer.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

def _find_repo_root() -> Path:
    """
    Walk up for dgx-orchestrator.py. The first version used
    Path(__file__).parent, which is the repo root only if this file sits
    there -- it does not, it lives in tools/. Running it resolved every
    path one level too deep, every command failed, and the checks below
    then ran against the ERROR TEXT and reported OK. Exactly the failure
    verify_pending_launch.py had, reproduced here within two days of my
    pointing it out.
    """
    for d in [Path(__file__).resolve().parent, *Path(__file__).resolve().parents]:
        if (d / "dgx-orchestrator.py").is_file():
            return d
    raise SystemExit(
        "[-] Cannot find the repo root (no dgx-orchestrator.py above "
        f"{Path(__file__).resolve().parent}).\n"
        "    Refusing to run -- every check below would silently test nothing.")


REPO = _find_repo_root()
OUT = REPO / "verification-report.txt"
lines: list[str] = []

TOKENISH = re.compile(r"\b(hf_[A-Za-z0-9]{8,}|sk-[A-Za-z0-9]{8,}|gh[pous]_[A-Za-z0-9]{8,})\b")


def emit(s: str = "") -> None:
    lines.append(s)
    print(s)


def header(n: str) -> None:
    emit("\n" + "=" * 74)
    emit(n)
    emit("=" * 74)


def run(cmd: list[str] | str, *, shell: bool = False, timeout: int = 180) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, shell=shell, cwd=REPO, capture_output=True,
                           text=True, timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        # run_ssh(tty=True) emits bare CR. Left in, the report stair-steps
        # across the page and is close to unreadable -- see the 18:52 run.
        out = out.replace("\r\n", "\n").replace("\r", "\n")
        return r.returncode, TOKENISH.sub("***SCRUBBED***", out)
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {timeout}s"
    except FileNotFoundError as exc:
        return 127, f"not found: {exc}"


def resolve_cli() -> list[str] | None:
    """dgx-config on PATH, else ./dgx-config, else the module entry point."""
    if shutil.which("dgx-config"):
        return ["dgx-config"]
    if (REPO / "dgx-config").is_file():
        return [str(REPO / "dgx-config")]
    if (REPO / "dgx-orchestrator.py").is_file():
        return [sys.executable, str(REPO / "dgx-orchestrator.py"), "cli"]
    return None


CLI = resolve_cli()


def show(label: str, cmd, *, shell=False, timeout=180, head=None, grep=None):
    emit(f"\n--- {label}")
    emit(f"$ {cmd if isinstance(cmd, str) else ' '.join(cmd)}")
    rc, out = run(cmd, shell=shell, timeout=timeout)
    body = out.splitlines()
    if grep:
        body = [l for l in body if re.search(grep, l)] or ["(no matching lines)"]
    if head:
        body = body[:head]
    emit("\n".join(body) if body else "(no output)")
    emit(f"[exit {rc}]")
    return rc, out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow-ssh", action="store_true",
                    help="permit the one read-only SSH check in section 6")
    a = ap.parse_args()

    if CLI is None:
        raise SystemExit("[-] Cannot find dgx-config or dgx-orchestrator.py. "
                         "Refusing to run -- every check would test nothing.")

    # The first version of this script mis-resolved the repo root and wrote
    # its report into tools/. That stale file outlives the fix and is the
    # one a person reaches for, because it has the same name. Say so.
    stale = REPO / "tools" / "verification-report.txt"
    if stale.exists() and stale.resolve() != OUT.resolve():
        print(f"[!] A stale report from the pre-fix script is at:\n      {stale}")
        print(f"    This run writes to:\n      {OUT}")
        print("    Same filename, different directory. Delete the stale one --")
        print("    it reports OK for checks that never ran.\n")
    emit(f"verification-report  {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    emit(f"repo: {REPO}")
    emit(f"cli:  {' '.join(CLI)}")
    emit(f"git:  {run(['git','rev-parse','--short','HEAD'])[1].strip()}  "
         f"{run(['git','rev-parse','--abbrev-ref','HEAD'])[1].strip()}")
    dirty = run(["git", "status", "--porcelain"])[1].strip()
    emit(f"working tree: {'DIRTY' if dirty else 'clean'}")
    if dirty:
        emit(dirty)

    # ---------------------------------------------------------------- 0
    header("0. DAEMON AND CATALOG")
    show("orchestrator version + staleness", "curl -s http://localhost:5001/api/status "
         "| python3 -c \"import sys,json;d=json.load(sys.stdin);"
         "print('version:',d.get('orchestrator_version'));"
         "print('stale:',d.get('stale'),d.get('stale_for_seconds'));"
         "print('primary:',d.get('primary_host'),'reserved:',d.get('reserved_hosts'),"
         "'default:',d.get('default_deploy_host'))\"", shell=True)
    _rc, cat = show("catalog size (MUST be ~35, not 0 -- the loader fails closed, #41)",
               " ".join(CLI + ["status"]) + " 2>&1 | head -60", shell=True)

    # ---------------------------------------------------------------- 1
    header("1. DRY RUNS -- the four recipes fixed 2026-09-09")
    emit("No SSH, no host contact. Reading for: no --speculative-model anywhere;")
    emit("drafter inside --speculative-config JSON; qwen 2-node = TP2 + ray + b12x;")
    emit("sqk2 --nodes 2 must ERROR; HF_TOKEN masked.")

    cases = [
        ("qwen-3.8-27b 1-node", CLI + ["deploy", "--model", "qwen-3.8-27b",
                                 "--nodes", "1", "--dry-run"]),
        ("qwen-3.8-27b 2-node (--force: spark-3 reserved)",
         CLI + ["deploy", "--model", "qwen-3.8-27b", "--nodes", "2",
          "--dry-run", "--force"]),
        ("qwen-3.8-27b-nvfp4-sqk2 1-node",
         CLI + ["deploy", "--model", "qwen-3.8-27b-nvfp4-sqk2",
          "--nodes", "1", "--dry-run"]),
        ("sqk2 2-node -- EXPECTED TO FAIL (its dead block was deleted)",
         CLI + ["deploy", "--model", "qwen-3.8-27b-nvfp4-sqk2",
          "--nodes", "2", "--dry-run", "--force"]),
        ("nemotron-3.5-lightning-nvfp4 1-node",
         CLI + ["deploy", "--model", "nemotron-3.5-lightning-nvfp4",
          "--nodes", "1", "--dry-run"]),
        ("_nemotron-3.5-lightning-nvfp4-tools 1-node",
         CLI + ["deploy", "--model", "_nemotron-3.5-lightning-nvfp4-tools",
          "--nodes", "1", "--dry-run"]),
    ]
    verdicts = []
    for label, cmd in cases:
        rc, out = show(label, cmd, timeout=120)
        expect_fail = "EXPECTED TO FAIL" in label

        # A verdict requires POSITIVE EVIDENCE that the thing under test
        # actually ran. Absence checks on an error message are vacuous --
        # "not found: dgx-config" contains no --speculative-model either.
        # The first version of this script reported OK on exactly that.
        two_node = "--nodes 2" in " ".join(cmd) or "2-node" in label
        produced_argv = "docker" in out and (
            ("ray" in out and "--num-gpus" in out) if two_node else "--model" in out)
        if expect_fail:
            verdicts.append((label, "AS EXPECTED" if rc != 0 else "UNEXPECTED PASS",
                             {} if rc != 0 else {"should have been refused": False}))
            emit(f"  -> {'refused, as expected' if rc != 0 else 'DID NOT FAIL -- investigate'}")
            continue
        if rc != 0 or not produced_argv:
            why = (f"command exited {rc}" if rc != 0
                   else "command succeeded but emitted no docker argv")
            verdicts.append((label, "NO VERDICT", {"ran": False}))
            emit(f"  -> NO VERDICT: {why}. Nothing was tested; the checks below")
            emit("     are deliberately NOT reported, because absence checks on")
            emit("     an error message pass for the wrong reason.")
            continue

        v = {
            "--speculative-model absent": "--speculative-model" not in out,
            "token masked": not TOKENISH.search(out),
        }
        if "2-node" in label:
            # A 2-node dry run shows ONLY the Ray bootstrap containers. The
            # engine is exec'd in afterwards, so --model, tp/pp size and
            # every vllm_args flag are ABSENT from docker_run_commands.
            # Confirmed on hardware 2026-09-10. E003/E005 and anything else
            # about vllm_args are therefore NOT previewable for 2-node --
            # do not add checks for them here, they will silently never
            # fire. Check what the bootstrap actually contains.
            v["ray head bootstrap present"] = '"--head"' in out or "ray start --head" in out
            v["worker joins the head"] = "--address=" in out
            v["b12x image not NGC default"] = ("spark-vllm-b12x" in out
                                               and "nvcr.io/nvidia/vllm" not in out)
        emit("  checks: " + ", ".join(f"{k}={'OK' if b else 'LOOK'}" for k, b in v.items()))
        verdicts.append((label, "OK" if all(v.values()) else "LOOK", v))

    # ---------------------------------------------------------------- 2
    header("2. LEGACY CATALOG REMOVAL (TOMBSTONES #144)")
    emit("Expect ONLY the two past-tense comments in dgx-orchestrator.py.")
    show("repo-wide grep -- K10's own verification step",
         r"grep -rn 'USE_LEGACY_CATALOG\|models\.yaml' . "
         r"--include=*.py --include=*.yaml --include=*.sh "
         r"| grep -v '^\./docs/' | grep -v '\.bak'", shell=True)
    show("recipes/eugr retired?", "ls -la recipes/ 2>&1", shell=True)

    # ---------------------------------------------------------------- 3
    header("3. PREFETCHER -- drafters must now be discovered")
    emit("Reading the 'Discovered HF Repositories' line ONLY. The script is")
    emit("serial and a full run takes hours, so this is cut short deliberately.")
    # python3 -u: without it the discovery lines sit in a pipe buffer while
    # ssh's stderr goes straight through, so `head` captures docker-pull
    # noise and misses the only line this section exists to read.
    show("discovered repositories (cut short deliberately; a full run is hours)",
         "timeout 20 python3 -u cache_cluster_assets.py 2>/dev/null "
         "| grep -m2 -E 'Discovered (Docker Images|HF Repositories)'",
         shell=True, timeout=60)
    emit("\nLooking for these four drafters in the line above:")
    for d in ["z-lab/gemma-4-26B-A4B-it-DFlash",
              "google/gemma-4-26B-A4B-it-assistant",
              "meta-models/Muse-Glimmer-30B-assistant",
              "NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4-DSpark"]:
        emit(f"  - {d}")

    # ---------------------------------------------------------------- 4
    header("4. VERIFY HARNESSES")
    emit("Expect 5 PASS. verify_gemma4_recipe / verify_glm_recipe need their")
    emit("recipe YAMLs. verify_recipe_equivalence SHOULD fail -- it is obsolete")
    emit("by design; read its banner before 'fixing' it.")
    vd = REPO / "tools" / "verify"
    if vd.is_dir():
        for f in sorted(vd.glob("verify_*.py")):
            rc, out = run([sys.executable, str(f)], timeout=300)
            tail = [l for l in out.splitlines() if l.strip()][-1:] or [""]
            emit(f"  {f.name:<40} exit={rc:<4} {tail[0][:60]}")
    else:
        emit("  tools/verify/ not present")

    show("does the GLM recipe set launch_argv_prefix? (if yes, verify_glm_recipe "
         "asserts a stale argv shape and would pass while proving nothing)",
         "grep -n 'launch_argv_prefix\\|entrypoint' recipes/local/glm-5_3-flash-nvfp4-mtp.yaml 2>&1",
         shell=True)

    # ---------------------------------------------------------------- 5
    header("5. DOC REFERENCE CHECK (advisory)")
    if (REPO / "tools" / "check_doc_references.py").exists():
        show("checker", [sys.executable, "tools/check_doc_references.py"], timeout=120)
    else:
        emit("  tools/check_doc_references.py not present yet")

    header("6. ARCHIVE MOVER -- DRY RUN ONLY")
    if (REPO / "tools" / "finalize_docs.py").exists():
        show("mover plan (nothing is moved)", [sys.executable, "tools/finalize_docs.py"],
             timeout=120)
    else:
        emit("  tools/finalize_docs.py not present yet")

    if a.allow_ssh:
        header("7. IMAGE DRIFT ACROSS HOSTS (Incident #14 / #106)")
        for host in ("10.0.14.41", "10.0.14.43"):
            show(f"ray version on {host}",
                 f"ssh -o BatchMode=yes -o ConnectTimeout=8 tetrel@{host} "
                 f"\"docker run --rm eugr/spark-vllm-b12x:latest python3 -c "
                 f"'import ray;print(ray.__version__)'\" 2>&1 | tail -2",
                 shell=True, timeout=180)
    else:
        header("7. SKIPPED -- pass --allow-ssh to include the host image check")

    header("SUMMARY")
    for label, state, v in verdicts:
        bad = [k for k, b in v.items() if not b]
        emit(f"  {state:<13} {label}" + (f"   -> {', '.join(bad)}" if bad else ""))
    nv = sum(1 for _, st, _ in verdicts if st == "NO VERDICT")
    if nv:
        emit(f"\n  {nv} case(s) produced NO VERDICT -- the command did not run or")
        emit("  emitted no argv. THIS IS NOT A PASS. Fix the environment and")
        emit("  re-run; a report full of NO VERDICT tested nothing at all.")

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "=" * 74)
    print(f"Written to {OUT}")
    print("Confirm before reading: line 2 must be the repo ROOT (not .../tools),")
    print("and line 3 must be a `cli:` line. If not, you are reading a stale report.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
