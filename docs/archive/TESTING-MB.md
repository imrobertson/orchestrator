# Testing Task MB — `common/mods.py` on live hardware

This validates the bake/cache/tag-resolution mechanism standalone, by
calling `ensure_mods_baked()` directly from a Python shell on `maestro`.
Task MC (deploy-path integration) hasn't happened yet, so there is no
`--dry-run` / dashboard path to trigger this through yet — you're calling
the module's public functions directly against `spark-3`/`spark-4`.

None of this should be committed. The test mods below are throwaway
fixtures to exercise MB's mechanics; Task MD creates the real `mods/_noop/`
as its own deliverable, in its own conversation, per
`PHASE-MODS-PROMPTS.md`. Delete `mods/_test_*` when you're done here.

**Base image:** `eugr/spark-vllm-b12x:latest` — already present on both
hosts and the one M0 validated `docker commit` fidelity against. No need
to pull anything new.

---

## 0. Place the file

Copy the delivered `mods.py` to `common/mods.py` in your repo checkout on
`maestro`. Don't wire it into `dgx-orchestrator.py` yet — that's Task MC.

```bash
cp /path/to/downloaded/mods.py common/mods.py
python3 -c "import ast; ast.parse(open('common/mods.py').read())"
```

## 1. Create the throwaway test mods

Three of them, each exercising a different path:

```bash
mkdir -p mods/_test_marker_a mods/_test_marker_b mods/_test_failing

cat > mods/_test_marker_a/run.sh << 'EOF'
#!/bin/bash
set -e
echo "marker_a applied at $(date -u +%FT%TZ), WORKSPACE_DIR=$WORKSPACE_DIR" \
  >> "$WORKSPACE_DIR/mod_order.log"
EOF

cat > mods/_test_marker_b/run.sh << 'EOF'
#!/bin/bash
set -e
echo "marker_b applied at $(date -u +%FT%TZ), WORKSPACE_DIR=$WORKSPACE_DIR" \
  >> "$WORKSPACE_DIR/mod_order.log"
EOF

cat > mods/_test_failing/run.sh << 'EOF'
#!/bin/bash
echo "this mod deliberately fails" >&2
exit 1
EOF

chmod +x mods/_test_marker_a/run.sh mods/_test_marker_b/run.sh mods/_test_failing/run.sh
```

`_test_marker_a` / `_test_marker_b` both append to the same file under
`$WORKSPACE_DIR` — appending (not overwriting) is what lets you confirm
declared *order* from the log's line order, and confirms `WORKSPACE_DIR`
actually resolved to something writable inside the container rather than
a hardcoded guess. `_test_failing` exists purely to drive the abort path.

## 2. Open a Python shell on `maestro`, from the repo root

```bash
cd /path/to/orchestrator
python3
```

```python
from common.mods import ensure_mods_baked, resolve_mod_tag, ModBakeError, ModResolutionError
import subprocess, shlex

BASE_IMAGE = "eugr/spark-vllm-b12x:latest"
HOSTS = {"spark-4": "10.0.14.43", "spark-3": "10.0.14.41"}  # from cluster_config.yaml

def ssh(ip, *cmd):
    """Quick ad-hoc check, independent of run_ssh, so you're not trusting
    the same code path you're testing to also verify it."""
    full = ["ssh", "-o", "StrictHostKeyChecking=no", f"tetrel@{ip}"] + list(cmd)
    return subprocess.run(full, capture_output=True, text=True, timeout=30)
```

## 3. Test A — resolution without hardware (should need zero SSH)

```python
# Missing mod -> ModResolutionError, no network activity
try:
    resolve_mod_tag(BASE_IMAGE, ["does_not_exist"])
    print("FAIL: should have raised")
except ModResolutionError as e:
    print("PASS:", e)

# Empty mods -> exact same tag as base, immediately
assert resolve_mod_tag(BASE_IMAGE, []) == BASE_IMAGE
print("PASS: empty mod set is a no-op tag")
```

**Expected:** both print PASS instantly, no SSH traffic (you can watch
`ssh -v` output or just note there's no delay).

## 4. Test B — first real bake, on `spark-4`

```python
tag = ensure_mods_baked("spark-4", HOSTS["spark-4"], BASE_IMAGE,
                         ["_test_marker_a", "_test_marker_b"])
print("baked tag:", tag)
```

**Verify independently (not through the module):**

```python
# tag exists on spark-4
r = ssh(HOSTS["spark-4"], "docker", "image", "inspect", tag)
assert r.returncode == 0, r.stderr
print("PASS: tag present on spark-4")

# mod order log shows marker_a before marker_b
r = ssh(HOSTS["spark-4"], "docker", "run", "--rm", tag, "cat", "/workspace/vllm/mod_order.log")
print(r.stdout)
assert "marker_a" in r.stdout.splitlines()[0]
assert "marker_b" in r.stdout.splitlines()[1]
print("PASS: mods applied in declared order")

# ENTRYPOINT/CMD restored to match base -- the critical check
base_ep = ssh(HOSTS["spark-4"], "docker", "inspect", "--format", "{{json .Config.Entrypoint}}", BASE_IMAGE).stdout.strip()
derived_ep = ssh(HOSTS["spark-4"], "docker", "inspect", "--format", "{{json .Config.Entrypoint}}", tag).stdout.strip()
assert base_ep == derived_ep, (base_ep, derived_ep)
base_cmd = ssh(HOSTS["spark-4"], "docker", "inspect", "--format", "{{json .Config.Cmd}}", BASE_IMAGE).stdout.strip()
derived_cmd = ssh(HOSTS["spark-4"], "docker", "inspect", "--format", "{{json .Config.Cmd}}", tag).stdout.strip()
assert base_cmd == derived_cmd, (base_cmd, derived_cmd)
print("PASS: Entrypoint/Cmd match base exactly (not the throwaway 'sleep' override)")

# WorkingDir untouched
base_wd = ssh(HOSTS["spark-4"], "docker", "inspect", "--format", "{{.Config.WorkingDir}}", BASE_IMAGE).stdout.strip()
derived_wd = ssh(HOSTS["spark-4"], "docker", "inspect", "--format", "{{.Config.WorkingDir}}", tag).stdout.strip()
assert base_wd == derived_wd == "/workspace/vllm"
print("PASS: WorkingDir preserved,", derived_wd)

# no dangling throwaway container, no leftover staging dir
r = ssh(HOSTS["spark-4"], "docker", "ps", "-a", "--filter", "name=dgx-mods-bake", "--format", "{{.Names}}")
assert r.stdout.strip() == "", r.stdout
print("PASS: no leftover bake containers")

r = ssh(HOSTS["spark-4"], "bash", "-c", "ls /tmp | grep dgx-mods-bake || true")
assert r.stdout.strip() == "", r.stdout
print("PASS: no leftover staging dirs")
```

**Also deploy it for real**, mirroring M0's step 4 — confirm the derived
image actually serves, not just that its `.Config` looks right:

```bash
# on spark-4, outside the python shell
ssh tetrel@10.0.14.43 docker run -d --name mods-test-serve --net=host --gpus all \
  -v /home/tetrel/.cache/huggingface:/root/.cache/huggingface \
  <the tag printed above> \
  python3 -m vllm.entrypoints.openai.api_server --model <a small known-good hf_path> \
  --gpu-memory-utilization 0.5 --max-model-len 4096
# then the usual: docker logs -f mods-test-serve, curl :8000/health once up, docker rm -f when done
```

## 5. Test C — idempotent second call

```python
import time
t0 = time.time()
tag_again = ensure_mods_baked("spark-4", HOSTS["spark-4"], BASE_IMAGE,
                               ["_test_marker_a", "_test_marker_b"])
elapsed = time.time() - t0
assert tag_again == tag
assert elapsed < 5, f"took {elapsed}s -- looks like it rebaked instead of skipping"
print(f"PASS: second call returned same tag in {elapsed:.2f}s (no rebake)")
```

## 6. Test D — payload edit triggers a rebake

```python
with open("mods/_test_marker_a/run.sh", "a") as f:
    f.write('echo "edited" >> "$WORKSPACE_DIR/mod_order.log"\n')

new_tag = ensure_mods_baked("spark-4", HOSTS["spark-4"], BASE_IMAGE,
                             ["_test_marker_a", "_test_marker_b"])
assert new_tag != tag, "tag should have changed after editing a mod payload"
print("PASS: edited payload produced new tag:", tag, "->", new_tag)

r = ssh(HOSTS["spark-4"], "docker", "image", "inspect", new_tag)
assert r.returncode == 0
print("PASS: new tag exists on spark-4")
```

## 7. Test E — failing `run.sh` aborts cleanly

```python
try:
    ensure_mods_baked("spark-4", HOSTS["spark-4"], BASE_IMAGE,
                       ["_test_marker_a", "_test_failing"])
    print("FAIL: should have raised ModBakeError")
except ModBakeError as e:
    print("PASS:", e)

# confirm no half-baked tag was left behind
would_be_tag = resolve_mod_tag(BASE_IMAGE, ["_test_marker_a", "_test_failing"])
r = ssh(HOSTS["spark-4"], "docker", "image", "inspect", would_be_tag)
assert r.returncode != 0, "a half-applied image must not exist"
print("PASS: no partial tag left behind")

# confirm cleanup: no dangling container, no staging dir
r = ssh(HOSTS["spark-4"], "docker", "ps", "-a", "--filter", "name=dgx-mods-bake", "--format", "{{.Names}}")
assert r.stdout.strip() == ""
r = ssh(HOSTS["spark-4"], "bash", "-c", "ls /tmp | grep dgx-mods-bake || true")
assert r.stdout.strip() == ""
print("PASS: cleanup left nothing dangling after the failure")
```

## 8. Test F — second host is baked independently (2-node symmetry)

Confirms per-host baking actually happens per host — a tag existing on
`spark-4` must not make `ensure_mods_baked()` skip the bake on `spark-3`.

```python
r_before = ssh(HOSTS["spark-3"], "docker", "image", "inspect", tag)
assert r_before.returncode != 0, "tag should not exist on spark-3 yet"

tag_spark3 = ensure_mods_baked("spark-3", HOSTS["spark-3"], BASE_IMAGE,
                                ["_test_marker_a", "_test_marker_b"])
assert tag_spark3 == tag  # same content -> same deterministic tag
r_after = ssh(HOSTS["spark-3"], "docker", "image", "inspect", tag)
assert r_after.returncode == 0
print("PASS: spark-3 baked independently, produced the identical tag")
```

## 9. Cleanup

```python
for h, ip in HOSTS.items():
    for t in {tag, new_tag}:
        ssh(ip, "docker", "rmi", "-f", t)
```

```bash
ssh tetrel@10.0.14.43 docker rm -f mods-test-serve   # if you ran the serve check
rm -rf mods/_test_marker_a mods/_test_marker_b mods/_test_failing
```

---

## Pass/fail summary to report back

Matches the "what to send back for review" convention: plain pass/fail
per numbered test above, plus anything that contradicted an assumption in
`PHASE-MODS-PROMPTS.md` or the module's docstrings — in particular, flag
it clearly if the Entrypoint/Cmd restoration check (Test B) fails, since
that's the one place a passing "it deployed" result could still be
masking a wrong derived image in a less careful test.
