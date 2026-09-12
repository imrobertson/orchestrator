#!/usr/bin/env python3
"""
Verify the GLM-5.3 recipe against argv captured from the orchestrator's real
two-node dry run. This confirms E005, E019, E020, E021 and E023 at the
argv-construction boundary, and includes a sabotage check proving the harness
rejects a missing entrypoint override.

This does not confirm that weights are staged on either host, that the image
accepts the captured flags, or that a real deployment succeeds.
"""

from __future__ import annotations

import copy
import json
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from _repo import ORCHESTRATOR, REPO_ROOT, recipe_path

import yaml


MODEL = "glm-5_3-flash-nvfp4-mtp"
RECIPE = "glm-5_3-flash-nvfp4-mtp.yaml"
EXPECTED_MODEL_PATH = "/models/GLM-5.3-Flash-NVFP4"


class EnvironmentFailure(Exception):
    """The dry run did not produce usable captured argv."""


@dataclass
class Check:
    section: int
    label: str
    passed: bool
    detail: str = ""


def resolve_cli() -> list[str] | None:
    """Resolve the CLI in the same order as tools/run_verification.py."""
    if shutil.which("dgx-config"):
        return ["dgx-config"]
    repo_cli = REPO_ROOT / "dgx-config"
    if repo_cli.is_file():
        return [str(repo_cli)]
    if ORCHESTRATOR.is_file():
        return [sys.executable, str(ORCHESTRATOR), "cli"]
    return None


def parse_payload(output: str) -> dict[str, Any]:
    """Find the CLI's JSON object after any informational preamble."""
    decoder = json.JSONDecoder()
    for pos, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(output[pos:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and not output[pos + end:].strip():
            return value
    raise EnvironmentFailure("CLI output did not end with a JSON object")


def capture_dry_run() -> tuple[list[str], dict[str, Any]]:
    cli = resolve_cli()
    if cli is None:
        raise EnvironmentFailure(
            "CLI missing: no dgx-config or dgx-orchestrator.py was found")

    cmd = cli + [
        "deploy", "--model", MODEL, "--nodes", "2", "--dry-run", "--force"
    ]
    try:
        result = subprocess.run(
            cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=180)
    except FileNotFoundError as exc:
        raise EnvironmentFailure(f"CLI missing: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise EnvironmentFailure("dry run timed out after 180 seconds") from exc

    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        detail = output.strip() or "no output"
        raise EnvironmentFailure(
            f"dry run did not execute successfully (exit {result.returncode}): {detail}")

    try:
        payload = parse_payload(result.stdout or "")
    except EnvironmentFailure:
        payload = parse_payload(output)
    if payload.get("status") != "dry_run":
        message = payload.get("message", repr(payload))
        if "not defined in catalog" in str(message):
            raise EnvironmentFailure(f"recipe absent: {message}")
        raise EnvironmentFailure(f"CLI did not return a dry-run payload: {message}")

    return cmd, payload


def option_values(argv: list[Any], option: str) -> list[Any]:
    return [argv[i + 1] for i, token in enumerate(argv[:-1]) if token == option]


def evaluate(
    recipe: dict[str, Any],
    docker_commands: dict[str, list[Any]],
    head: str,
) -> list[Check]:
    checks: list[Check] = []
    image = recipe["image"]
    hf_path = recipe["hf_path"]
    model_path = recipe.get("model_path_override") or hf_path
    mounts = recipe.get("extra_mounts", [])
    workers = [host for host in docker_commands if host != head]
    worker = workers[0] if len(workers) == 1 else None
    engine_argvs: dict[str, list[Any]] = {}

    for host, docker_argv in docker_commands.items():
        if image in docker_argv:
            image_index = docker_argv.index(image)
            engine_argvs[host] = docker_argv[image_index + 1:]
        else:
            engine_argvs[host] = []

        entrypoint_ok = False
        if "--entrypoint" in docker_argv:
            index = docker_argv.index("--entrypoint")
            entrypoint_ok = (
                docker_argv[index:index + 3] == ["--entrypoint", "", image])
        checks.append(Check(
            2, f"E020: --entrypoint '' immediately precedes the image on {host}",
            entrypoint_ok, shlex.join(str(v) for v in docker_argv),
        ))
        for mount in mounts:
            mount_ok = mount in docker_argv and docker_argv.index(mount) > 0
            mount_ok = (
                mount_ok and docker_argv[docker_argv.index(mount) - 1] == "-v")
            checks.append(Check(
                2, f"E021: extra mount {mount!r} is preceded by -v on {host}",
                mount_ok, shlex.join(str(v) for v in docker_argv),
            ))

    hf_basename = hf_path.rsplit("/", 1)[-1]
    model_basename = model_path.rstrip("/").rsplit("/", 1)[-1]
    checks.extend([
        Check(3, "model_path_override differs from the hf_path identity",
              model_path != hf_path, f"{model_path!r} vs {hf_path!r}"),
        Check(3, "model_path_override is the expected staged path",
              model_path == EXPECTED_MODEL_PATH, repr(model_path)),
        Check(3, "E019: model_path_override and hf_path basenames match case-sensitively",
              model_basename == hf_basename,
              f"{model_basename!r} vs {hf_basename!r}"),
        Check(3, "catalog-key suffix relationship still holds",
              hf_path.endswith(model_basename) or model_basename in hf_path,
              f"hf_path={hf_path!r}, basename={model_basename!r}"),
        Check(3, "one worker argv was captured", worker is not None, repr(workers)),
    ])

    all_docker_tokens = [token for argv in docker_commands.values() for token in argv]
    use_ray = "ray" in all_docker_tokens
    checks.extend([
        Check(4, "hf_path identity is absent from every docker run argv",
              hf_path not in all_docker_tokens, repr(hf_path)),
        Check(4, "ray is absent from every captured argv",
              "ray" not in all_docker_tokens, repr(all_docker_tokens)),
        Check(4, "--block is absent from every captured argv",
              "--block" not in all_docker_tokens, repr(all_docker_tokens)),
        Check(4, "captured argv proves use_ray is False",
              use_ray is False, f"use_ray={use_ray}"),
    ])

    for host, engine_argv in engine_argvs.items():
        expected_rank = "0" if host == head else "1"
        checks.extend([
            Check(5, f"{host}: captured engine argv begins with positional model",
                  engine_argv[:3] == ["vllm", "serve", model_path],
                  shlex.join(str(v) for v in engine_argv)),
            Check(5, f"{host}: no separate --model flag is emitted",
                  "--model" not in engine_argv,
                  shlex.join(str(v) for v in engine_argv)),
            Check(5, f"{host}: --tensor-parallel-size 2 is present",
                  option_values(engine_argv, "--tensor-parallel-size") == ["2"],
                  repr(option_values(engine_argv, "--tensor-parallel-size"))),
            Check(5, f"{host}: --nnodes 2 is present",
                  option_values(engine_argv, "--nnodes") == ["2"],
                  repr(option_values(engine_argv, "--nnodes"))),
            Check(5, f"{host}: --node-rank {expected_rank} is present",
                  option_values(engine_argv, "--node-rank") == [expected_rank],
                  repr(option_values(engine_argv, "--node-rank"))),
            Check(5, f"{host}: --headless matches its captured role",
                  ("--headless" in engine_argv) == (host != head),
                  shlex.join(str(v) for v in engine_argv)),
        ])

        pp_values = option_values(engine_argv, "--pipeline-parallel-size")
        speculative_values = option_values(engine_argv, "--speculative-config")
        speculative_config = None
        speculative_error = ""
        if len(speculative_values) == 1:
            try:
                speculative_config = json.loads(str(speculative_values[0]))
            except (TypeError, json.JSONDecodeError) as exc:
                speculative_error = str(exc)
        checks.extend([
            Check(6, f"E005: {host} has no --pipeline-parallel-size 2",
                  "2" not in pp_values, repr(pp_values)),
            Check(6, f"E023: {host} has no standalone --speculative-model",
                  "--speculative-model" not in engine_argv,
                  shlex.join(str(v) for v in engine_argv)),
            Check(6, f"{host}: exactly one --speculative-config value is present",
                  len(speculative_values) == 1, repr(speculative_values)),
            Check(6, f"{host}: --speculative-config value parses as JSON",
                  isinstance(speculative_config, dict),
                  speculative_error or repr(speculative_values)),
            Check(6, f"{host}: --speculative-config method is mtp",
                  isinstance(speculative_config, dict)
                  and speculative_config.get("method") == "mtp",
                  repr(speculative_config)),
        ])
    return checks


def print_checks(checks: list[Check]) -> list[Check]:
    failures: list[Check] = []
    current_section = None
    headings = {
        2: "captured docker run invariants",
        3: "recipe identity and staged model path",
        4: "non-Ray launch and identity separation",
        5: "native multi-node engine argv",
        6: "pipeline parallelism and speculation",
    }
    for check in sorted(checks, key=lambda item: item.section):
        if check.section != current_section:
            current_section = check.section
            print(f"\n[{current_section}] {headings[current_section]}")
        print(("  PASS  " if check.passed else "  FAIL  ") + check.label)
        if not check.passed:
            failures.append(check)
            if check.detail:
                print(f"        {check.detail}")
    return failures


def main() -> int:
    print("=" * 74)
    print("GLM-5.3 CAPTURED-ARGV VERIFICATION")
    print("=" * 74)
    print("\n[1] invoke the real two-node dry run")

    try:
        recipe = yaml.safe_load(recipe_path(RECIPE).read_text())
    except SystemExit as exc:
        print(f"  ENVIRONMENT FAILURE  recipe absent: {exc}")
        return 2
    except (OSError, yaml.YAMLError) as exc:
        print(f"  ENVIRONMENT FAILURE  recipe could not be read: {exc}")
        return 2

    try:
        cmd, payload = capture_dry_run()
    except EnvironmentFailure as exc:
        print(f"  ENVIRONMENT FAILURE  {exc}")
        print("\nSUMMARY: dry run unavailable; no assertions were evaluated")
        return 2

    print(f"  PASS  command completed: {shlex.join(cmd)}")
    print(f"  PASS  status={payload['status']!r}")
    print(f"  head={payload.get('head')!r}, targets={payload.get('targets')!r}")

    head = payload.get("head")
    docker_commands = payload.get("docker_run_commands")
    if not isinstance(docker_commands, dict):
        print("  FAIL  dry-run payload has no docker_run_commands mapping")
        print("\nSUMMARY: FAILED -- captured argv is unavailable")
        return 1
    if not isinstance(head, str) or head not in docker_commands:
        print("  FAIL  docker argv is not keyed by the reported head")
        print("\nSUMMARY: FAILED -- captured argv is invalid")
        return 1
    if len(docker_commands) != 2:
        print(f"  FAIL  expected 2 docker commands, got {len(docker_commands)}")
        print("\nSUMMARY: FAILED -- captured argv is invalid")
        return 1
    if not all(isinstance(argv, list) for argv in docker_commands.values()):
        print("  FAIL  docker_run_commands contains a non-list argv")
        print("\nSUMMARY: FAILED -- captured argv is invalid")
        return 1
    checks = evaluate(recipe, docker_commands, head)
    failures = print_checks(checks)

    print("\n[7] SABOTAGE -- remove the head's --entrypoint override")
    sabotaged = copy.deepcopy(docker_commands)
    sabotaged_head = sabotaged[head]
    if "--entrypoint" not in sabotaged_head:
        print("  FAIL  cannot stage sabotage because captured --entrypoint is absent")
        failures.append(Check(7, "sabotage could not be staged", False))
    else:
        index = sabotaged_head.index("--entrypoint")
        del sabotaged_head[index:index + 2]
        sabotage_checks = evaluate(recipe, sabotaged, head)
        sabotage_failures = [check for check in sabotage_checks if not check.passed]
        caught_entrypoint = any("E020" in check.label for check in sabotage_failures)
        untouched = "--entrypoint" in docker_commands[head]
        sabotage_ok = caught_entrypoint and untouched
        print(("  PASS  " if sabotage_ok else "  FAIL  ")
              + "checking function rejects the corrupted copy; capture remains intact")
        if sabotage_ok:
            print("        demonstrated failure: E020 entrypoint check")
        else:
            failures.append(Check(7, "sabotage was not detected", False))

    print("\n" + "=" * 74)
    if failures:
        print(f"SUMMARY: FAILED -- {len(failures)} check(s) failed")
        return 1
    print("SUMMARY: ALL CHECKS PASSED (including demonstrated sabotage failure)")
    print("""
What this confirms:
  - the real dry run constructs native two-node vLLM argv on both hosts,
    with rank 0 on the head, rank 1 plus --headless on the worker, and no
    `ray` or standalone `--block` token
  - --entrypoint '' immediately precedes the image and the staged local path
    is bind-mounted; the hf_path identity never leaks into launch argv
  - each engine gets the staged path positionally, TP=2 without PP=2, and
    MTP through JSON --speculative-config

What this does NOT confirm:
  - that /var/tmp/glm-5.3-flash-nvfp4 has been populated on both hosts
  - that the image accepts these flags
  - that a real deploy succeeds
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
