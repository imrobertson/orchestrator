#!/usr/bin/env python3
"""
Verify _mask_secret_argv() and _env_flag_collisions().

THE ASSERTION THAT MATTERS, and the reason this file exists rather than a
couple of inline asserts: every masking check below tests that THE SECRET
STRING IS ABSENT FROM THE WHOLE OUTPUT. None of them test that a marker
appeared.

TOMBSTONES #94 is why. Redacting `authorization: <value>` with a `\\S+`
pattern once absorbed the word `Bearer` and wrote the real credential
immediately after the ***REDACTED*** marker -- output that looked MORE
redacted than an untouched line. A marker-presence assertion passed on it.
An absence assertion could not have.

Run standalone; imports the masking helpers by source extraction so it
needs neither the full orchestrator import graph nor pydantic.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _repo import ORCHESTRATOR, orchestrator_source
SRC = ORCHESTRATOR   # still referenced below as the compile() filename
text = orchestrator_source()

# Pull just the two helpers and their module-level regex out of the file.
ns: dict = {"re": re}
start = text.index("_SECRET_ENV_NAME_RE = re.compile(")
end = text.index("def _execute_deployment_impl(")
exec(compile(text[start:end], str(SRC), "exec"), ns)
mask = ns["_mask_secret_argv"]
collide = ns["_env_flag_collisions"]

FAIL = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   {detail}" if not cond else ""))
    if not cond:
        FAIL.append(label)


REAL = "hf_ZxQvAbCdEfGhIjKlMnOpQrStUvWxYz1234"

print("\n[1] the secret does not survive masking, anywhere in the output")
argv = [
    "docker", "run", "-d", "--name", "vllm-head",
    "-e", f"HF_TOKEN={REAL}",
    "-e", "PYTHONUNBUFFERED=1",
    "--entrypoint", "",
    "img:tag", "python3", "-m", "vllm.entrypoints.openai.api_server",
]
out = mask(argv)
joined = " ".join(str(a) for a in out)
check("secret absent from joined output", REAL not in joined, repr(joined))
check("secret absent from every element", all(REAL not in str(a) for a in out))
check("no fragment of the secret leaks", REAL[8:] not in joined and REAL[:12] not in joined)

print("\n[2] variable NAME is preserved (you must still see which creds are expected)")
check("HF_TOKEN name kept", any(str(a).startswith("HF_TOKEN=") for a in out))
check("argv length unchanged", len(out) == len(argv), f"{len(out)} vs {len(argv)}")

print("\n[3] non-secret values untouched")
check("PYTHONUNBUFFERED intact", "PYTHONUNBUFFERED=1" in out)
check("image arg intact", "img:tag" in out)
check("empty --entrypoint preserved", out[argv.index("--entrypoint") + 1] == "")

print("\n[4] an EMPTY secret is left visible (diagnostic signal, nothing to hide)")
e = mask(["-e", "HF_TOKEN="])
check("HF_TOKEN= unchanged", e == ["-e", "HF_TOKEN="], repr(e))

print("\n[5] name variants all masked")
for name in ["HF_TOKEN", "OPENAI_API_KEY", "MY_SECRET", "DB_PASSWORD", "AWS_SECRET_ACCESS_KEY"]:
    r = mask(["-e", f"{name}={REAL}"])
    check(f"{name} masked", REAL not in " ".join(r), repr(r))

print("\n[6] SABOTAGE -- a marker-presence test would pass on broken output")
def broken(argv):
    # The #94 bug shape: marker emitted, secret still present after it.
    return [f"HF_TOKEN=***MASKED*** {REAL}" if REAL in str(a) else a for a in argv]
b = broken(argv)
marker_test_passes = any("***MASKED***" in str(a) for a in b)
absence_test_passes = all(REAL not in str(a) for a in b)
check("marker-presence test WOULD pass on broken output", marker_test_passes)
check("absence test correctly FAILS on broken output", not absence_test_passes)

print("\n[7] env collisions: same value vs conflicting")
same = ["-e", "NCCL_SOCKET_IFNAME=enp1s0f0np0", "-e", "OMP_NUM_THREADS=16",
        "-e", "NCCL_SOCKET_IFNAME=enp1s0f0np0"]
c = collide(same)
check("duplicate detected", "NCCL_SOCKET_IFNAME" in c)
check("marked NOT conflicting", c["NCCL_SOCKET_IFNAME"]["conflicting"] is False)
check("non-duplicate absent", "OMP_NUM_THREADS" not in c)

diff = ["-e", "NCCL_SOCKET_IFNAME=enp1s0f0np0", "-e", "NCCL_SOCKET_IFNAME=eth0"]
c2 = collide(diff)
check("conflicting flagged", c2["NCCL_SOCKET_IFNAME"]["conflicting"] is True)
check("both values reported", c2["NCCL_SOCKET_IFNAME"]["values"] == ["enp1s0f0np0", "eth0"])

print("\n[8] clean argv produces no findings")
check("no collisions on clean input",
      collide(["-e", "A=1", "-e", "B=2"]) == {},
      repr(collide(["-e", "A=1", "-e", "B=2"])))

print("\n[9] the real GLM argv shape from the 2026-09-08 dry-run")
glm = []
for n, v in [("NCCL_CUMEM_ENABLE", "0"), ("NCCL_IB_GID_INDEX", "3"),
             ("NCCL_SOCKET_IFNAME", "enp1s0f0np0"), ("GLOO_SOCKET_IFNAME", "enp1s0f0np0"),
             ("OMP_NUM_THREADS", "16")]:
    glm += ["-e", f"{n}={v}"]
for n, v in [("NCCL_CUMEM_ENABLE", "0"), ("NCCL_IB_GID_INDEX", "3"),
             ("NCCL_SOCKET_IFNAME", "enp1s0f0np0"), ("GLOO_SOCKET_IFNAME", "enp1s0f0np0")]:
    glm += ["-e", f"{n}={v}"]
g = collide(glm)
check("four duplicates found", len(g) == 4, f"got {sorted(g)}")
check("none conflicting (values match)", not any(v["conflicting"] for v in g.values()))
check("OMP_NUM_THREADS not flagged", "OMP_NUM_THREADS" not in g)

print("\n" + "=" * 62)
if FAIL:
    print(f"FAILED: {len(FAIL)} check(s): {FAIL}")
    sys.exit(1)
print("ALL CHECKS PASSED")
