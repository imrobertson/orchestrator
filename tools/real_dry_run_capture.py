#!/usr/bin/env python3
"""
Captures every model x topology's real --dry-run docker_run_commands output
by calling execute_deployment(..., dry_run=True) directly (in-process, no
subprocess/CLI parsing needed) against the given dgx-orchestrator.py.

Usage: python3 real_dry_run_capture.py <path_to_dgx_orchestrator.py> <output_dir>
"""
import importlib.util
import json
import sys
from pathlib import Path


def main():
    target_path, out_dir = sys.argv[1], sys.argv[2]
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    spec = importlib.util.spec_from_file_location("orch_under_test", target_path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(Path(target_path).resolve().parent))
    spec.loader.exec_module(mod)

    catalog = mod.load_model_catalog().get("catalog", {}).get("models", {})
    written = 0
    for model_name, model_data in catalog.items():
        for topo_key in (model_data.get("topologies") or {}):
            nodes = 2 if topo_key == "2_node" else 1
            res = mod.execute_deployment(model_name, nodes, "spark-4", "dry-run-user", dry_run=True)
            cmds = res.get("docker_run_commands", {})
            # normalize (StrEnum -> str) exactly like the earlier harness did,
            # for apples-to-apples diffing against /tmp/real-baseline
            normalized = {host: [str(x) for x in cmd] for host, cmd in cmds.items()}
            (Path(out_dir) / f"{model_name}__{topo_key}.json").write_text(
                json.dumps(list(normalized.values()), indent=2)
            )
            written += 1
    print(f"[+] Wrote {written} files to {out_dir}")


if __name__ == "__main__":
    main()
