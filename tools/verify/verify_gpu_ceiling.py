#!/usr/bin/env python3
"""
Verify the gpu_util_ceiling decision table.

Extracts the branch from dgx-orchestrator.py by source and exercises all
five outcomes. The property that matters most is the LAST check: no mode,
under any combination, ever changes gpu_util itself. A guard that clamps
is how an unexplained performance regression appears six weeks later.
"""
import re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _repo import orchestrator_source
SRC = orchestrator_source()
start = SRC.index("    gpu_util_ceiling_note = None\n    try:\n        _cfg = load_cluster_config()")
end = SRC.index('    max_model_len = topo_config.get', start)
import textwrap
BODY = textwrap.dedent(SRC[start:end])

FAIL = []
def check(l, c, d=""):
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   {d}" if not c else ""))
    if not c: FAIL.append(l)

class Cfg:
    def __init__(s, ceiling, mode): s.gpu_util_ceiling, s.gpu_util_ceiling_enforce = ceiling, mode

def run(gpu_util, ceiling, mode, exempt):
    """Execute the extracted branch and report (outcome, gpu_util_after)."""
    printed = []
    ns = {
        "gpu_util": gpu_util, "model": "test-recipe",
        "model_config": {"gpu_util_ceiling_exempt": True} if exempt else {},
        "load_cluster_config": lambda: Cfg(ceiling, mode),
        "print": lambda s: printed.append(s),
    }
    src = "def _f():\n" + textwrap.indent(BODY, "    ") + "\n    return None\n_r = _f()"
    exec(compile(src, "<branch>", "exec"), ns)
    r = ns.get("_r")
    if isinstance(r, dict) and r.get("status") == "error":
        return "REFUSED", ns["gpu_util"], printed
    if printed:
        return ("NOTE" if printed[0].startswith("[i]") else "WARN"), ns["gpu_util"], printed
    return "SILENT", ns["gpu_util"], printed

print("\n[1] decision table")
cases = [
    (0.65, 0.75, "off",   False, "SILENT"),
    (0.65, 0.75, "error", False, "SILENT"),
    (0.85, 0.75, "off",   False, "NOTE"),
    (0.85, 0.75, "warn",  False, "WARN"),
    (0.85, 0.75, "error", False, "REFUSED"),
    (0.85, 0.75, "error", True,  "NOTE"),
    (0.85, 0.75, "off",   True,  "NOTE"),
    (0.85, 0.75, "warn",  True,  "NOTE"),
]
for gu, ceil, mode, ex, want in cases:
    got, after, _ = run(gu, ceil, mode, ex)
    check(f"gpu_util={gu} ceiling={ceil} mode={mode:5} exempt={str(ex):5} -> {want}",
          got == want, f"got {got}")

print("\n[2] NEVER CLAMPS -- gpu_util is unchanged in every case")
for gu, ceil, mode, ex, _ in cases:
    _, after, _ = run(gu, ceil, mode, ex)
    check(f"mode={mode:5} exempt={str(ex):5} keeps {gu}", after == gu, f"became {after}")

print("\n[3] an exempt recipe never emits a WARNING (rule 3: always-fires)")
for mode in ("off", "warn", "error"):
    got, _, printed = run(0.85, 0.75, mode, True)
    check(f"exempt under {mode:5} is informational", got == "NOTE" and printed[0].startswith("[i]"))

print("\n[4] the refusal message is actionable")
ns_out = run(0.85, 0.75, "error", False)
_, _, _ = ns_out
printed = ns_out[2]
src = "def _f():\n" + textwrap.indent(BODY, "    ") + "\n    return None\n_r = _f()"
ns = {"gpu_util": 0.85, "model": "test-recipe", "model_config": {},
      "load_cluster_config": lambda: Cfg(0.75, "error"), "print": lambda s: None}
exec(compile(src, "<branch>", "exec"), ns)
msg = ns["_r"]["message"]
for frag in ["gpu_util_ceiling_exempt", "lower the", "raise the ceiling", "warn"]:
    check(f"names remedy {frag!r}", frag in msg)
check("code is machine-readable", ns["_r"].get("code") == "gpu_util_ceiling")

print("\n[5] unreadable config degrades to off, never to refusing")
ns2 = {"gpu_util": 0.99, "model": "m", "model_config": {},
       "load_cluster_config": lambda: (_ for _ in ()).throw(RuntimeError("boom")),
       "print": lambda s: None}
exec(compile(src, "<branch>", "exec"), ns2)
check("config failure does not block a deploy", ns2["_r"] is None)

print("\n" + "="*58)
print(f"FAILED: {FAIL}" if FAIL else "ALL CHECKS PASSED")
sys.exit(1 if FAIL else 0)
