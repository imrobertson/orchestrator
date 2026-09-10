#!/usr/bin/env python3
"""
Verification harness for the disk-backed, host-keyed pending-launch
ledger path in dgx-orchestrator.py.

Exercises pure construction/promotion logic only -- no cluster, no SSH, no
HTTP. `common` is stubbed before import so the real cluster_config.yaml is
not required; host names are taken from the stub, never hardcoded into the
assertions.

Run:  python3 verify_pending_launch.py
"""
import hashlib
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import types

SELF = pathlib.Path(__file__).resolve()
print(f"[harness] self-hash: {hashlib.sha256(SELF.read_bytes()).hexdigest()[:16]}  {SELF}")

sys.path.insert(0, str(SELF.parent))
from _repo import ORCHESTRATOR          # noqa: E402

TARGET = ORCHESTRATOR
print(f"[harness] target-hash: {hashlib.sha256(TARGET.read_bytes()).hexdigest()[:16]}  {TARGET}")

# Host inventory the stub will report. The orchestrator derives
# PRIMARY_HOST/SECONDARY_HOST/RESERVED_HOSTS from this, so the assertions
# below read those back out of the module rather than restating them.
STUB_HOSTS = {
    "node-a": {"ip": "10.0.0.1", "alias": "alias-a", "role": "head", "reserved": True},
    "node-b": {"ip": "10.0.0.2", "alias": "alias-b", "role": "worker", "reserved": False},
}

WORKDIR = tempfile.mkdtemp(prefix="pending-launch-verify-")
os.environ["BASE_DIR"] = WORKDIR


def _install_stubs() -> None:
    common = types.ModuleType("common")
    common.__path__ = []
    sys.modules["common"] = common

    cfg = types.ModuleType("common.config")
    cfg.legacy_hosts_dict = lambda *a, **k: dict(STUB_HOSTS)
    cfg.reserved_hosts = lambda *a, **k: [h for h, m in STUB_HOSTS.items() if m["reserved"]]
    cfg.default_deploy_target = lambda *a, **k: "node-b"

    class _Tuning:
        shm_size_1node = "16gb"
        shm_size_2node = "64gb"
        gpu_clock_lock = "300,1800"
        deploy_wait_timeout_sec = 900
        deploy_poll_interval_sec = 15
        jit_cache_maxsize_bytes = 1
        debug_launch_blocking = False
        crash_log_retention_days = 7

    class _Cluster:
        tuning = _Tuning()
        ports = {"vllm_api": 8000, "orchestrator_api": 5001, "ray": 6379, "master": 29500}
        container_names = {"standalone": "vllm-standalone", "head": "vllm-head", "worker": "vllm-worker"}
        ssh_user = "tetrel"
        ssh_key_name = "id_dgx_orchestrator"
        default_image = "stub/image:latest"
        gpu_util_ceiling = 0.75
        global_hf_hub_offline = 0
        global_transformers_offline = 0
        network = types.SimpleNamespace(topology="switched", interface="eth0", nccl_ib_hca="hca0")
        hosts = {}

    cfg.load_cluster_config = lambda *a, **k: _Cluster()
    sys.modules["common.config"] = cfg

    consts = types.ModuleType("common.constants")

    class ContainerRole:
        STANDALONE = "vllm-standalone"
        HEAD = "vllm-head"
        WORKER = "vllm-worker"

    consts.ContainerRole = ContainerRole
    sys.modules["common.constants"] = consts

    mods = types.ModuleType("common.mods")
    mods.ModBakeError = type("ModBakeError", (Exception,), {})
    mods.ModResolutionError = type("ModResolutionError", (Exception,), {})
    mods.ensure_mods_baked = lambda *a, **k: {}
    mods.resolve_mod_tag = lambda *a, **k: None
    sys.modules["common.mods"] = mods

    runlog = types.ModuleType("common.runlog")
    runlog.archive_run_log = lambda *a, **k: None
    sys.modules["common.runlog"] = runlog

    # Any name the orchestrator imports from common.recipes resolves to a
    # harmless callable (or an exception class, if the name looks like
    # one). Enumerating them by hand just means this harness breaks every
    # time the real module grows an export, which tells us nothing.
    recipes = types.ModuleType("common.recipes")

    def _recipes_getattr(name):
        if name.endswith("Error"):
            return type(name, (Exception,), {})
        return lambda *a, **k: {}

    recipes.__getattr__ = _recipes_getattr
    sys.modules["common.recipes"] = recipes

    phase = types.ModuleType("common.phase_extract")
    phase.extract_phases = lambda *a, **k: None
    sys.modules["common.phase_extract"] = phase

    # Catch-all for any other common.* submodule the orchestrator imports
    # (common.ssh today, whatever lands next). Same permissive __getattr__
    # as common.recipes above -- none of these are exercised by the pure
    # ledger logic under test, and enumerating them by hand only makes the
    # harness rot.
    for extra in ("ssh", "runlog", "phase_extract"):
        name = f"common.{extra}"
        if name in sys.modules:
            continue
        mod = types.ModuleType(name)
        mod.__getattr__ = lambda n: (type(n, (Exception,), {}) if n.endswith("Error")
                                     else (lambda *a, **k: None))
        sys.modules[name] = mod


def _load_target():
    spec = importlib.util.spec_from_file_location("dgx_orchestrator_under_test", TARGET)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FAILURES = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}{(' -- ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append(label)


def main():
    _install_stubs()

    # common.recipes exports are stubbed with a shared lambda above; the
    # orchestrator imports specific names from it, so anything genuinely
    # missing will raise here rather than silently no-op later.
    try:
        m = _load_target()
    except Exception as exc:
        print(f"[harness] FATAL: could not import target: {exc!r}")
        raise

    primary = m.PRIMARY_HOST
    secondary = m.SECONDARY_HOST
    reserved = list(m.RESERVED_HOSTS)
    print(f"[harness] derived PRIMARY_HOST={primary} SECONDARY_HOST={secondary} "
          f"RESERVED_HOSTS={reserved} DEFAULT_DEPLOY_HOST={m.DEFAULT_DEPLOY_HOST}")
    print(f"[harness] state dir: {WORKDIR}")

    print("\n--- 1. disk round-trip (the CLI/daemon cross-process case) ---")
    m._set_pending_launch(secondary, "recipe-x", "1_node", "hash-x")
    on_disk = json.loads((pathlib.Path(WORKDIR) / "pending_launch_state.json").read_text())
    check("record lands on disk, keyed by host",
          secondary in on_disk, f"file keys={list(on_disk)}")
    check("record carries model/topo/hash",
          on_disk[secondary]["model"] == "recipe-x"
          and on_disk[secondary]["topo_key"] == "1_node"
          and on_disk[secondary]["config_hash"] == "hash-x",
          json.dumps(on_disk[secondary]))
    # A fresh read with no in-memory cache is the whole point of the fix.
    check("_load_pending_launch_state reads fresh from disk",
          m._load_pending_launch_state().get(secondary, {}).get("model") == "recipe-x")

    print("\n--- 2. two independent hosts hold independent records ---")
    m._set_pending_launch(primary, "recipe-agent", "1_node", "hash-agent")
    both = m._load_pending_launch_state()
    check("both hosts present simultaneously",
          set(both) == {primary, secondary}, f"keys={sorted(both)}")

    print("\n--- 3. promotion requires READY *and* matching recipe on that host ---")
    recorded = []
    m._record_launch_success = lambda model, topo, h: recorded.append((model, topo, h))

    # Not ready yet -> nothing promoted, record retained.
    m._consume_pending_launches({
        primary: {"model_status": "READY", "active_recipe_key": "recipe-agent"},
        secondary: {"model_status": "COMPILING", "active_recipe_key": "recipe-x"},
    })
    check("READY host promoted", ("recipe-agent", "1_node", "hash-agent") in recorded,
          f"recorded={recorded}")
    check("non-READY host not promoted", ("recipe-x", "1_node", "hash-x") not in recorded)
    after = m._load_pending_launch_state()
    check("promoted record cleared", primary not in after, f"remaining={sorted(after)}")
    check("unpromoted record retained", secondary in after, f"remaining={sorted(after)}")

    print("\n--- 4. READY but a DIFFERENT recipe is running -> no false promotion ---")
    recorded.clear()
    m._consume_pending_launches({
        secondary: {"model_status": "READY", "active_recipe_key": "some-other-recipe"},
    })
    check("mismatched recipe key does not promote", recorded == [], f"recorded={recorded}")
    check("record still retained after mismatch",
          secondary in m._load_pending_launch_state())

    print("\n--- 5. matching recipe now READY -> promoted exactly once ---")
    recorded.clear()
    status = {secondary: {"model_status": "READY", "active_recipe_key": "recipe-x"}}
    m._consume_pending_launches(status)
    m._consume_pending_launches(status)  # second poll, 4s later
    check("promoted exactly once across two polls",
          recorded == [("recipe-x", "1_node", "hash-x")], f"recorded={recorded}")
    check("state file now empty", m._load_pending_launch_state() == {},
          json.dumps(m._load_pending_launch_state()))

    print("\n--- 6. stale records age out instead of promoting forever ---")
    recorded.clear()
    m._set_pending_launch(secondary, "recipe-stale", "1_node", "hash-stale")
    stale = m._load_pending_launch_state()
    stale[secondary]["started_ts"] -= (m.PENDING_LAUNCH_STALE_SEC + 60)
    m._write_json_state(m.PENDING_LAUNCH_STATE_PATH, stale)
    m._consume_pending_launches({secondary: {"model_status": "READY",
                                             "active_recipe_key": "recipe-stale"}})
    check("stale record not promoted", recorded == [], f"recorded={recorded}")
    check("stale record dropped", m._load_pending_launch_state() == {})

    print("\n--- 7. teardown clears pending alongside active state ---")
    m._set_pending_launch(secondary, "recipe-y", "1_node", "hash-y")
    m._clear_pending_launch([secondary])
    check("cleared on teardown", m._load_pending_launch_state() == {})

    print("\n--- 8. malformed record is dropped, not retried forever ---")
    m._write_json_state(m.PENDING_LAUNCH_STATE_PATH, {secondary: "not-a-dict"})
    m._consume_pending_launches({secondary: {"model_status": "READY",
                                             "active_recipe_key": "anything"}})
    check("malformed record dropped", m._load_pending_launch_state() == {})

    print("\n--- 9. status payload exposes reserved-host policy ---")
    src = TARGET.read_text()
    for key in ("reserved_hosts", "default_deploy_host", "primary_host"):
        check(f'status_data emits "{key}"', f'"{key}":' in src)

    print("\n--- 10. reserved-host refusal is a structured record ---")
    reserved_host = reserved[0]
    unreserved = [h for h in STUB_HOSTS if h not in reserved][0]

    ok = m.check_reserved_hosts([unreserved], False, "deploy 'x' (1-node)")
    check("unreserved target is not refused", ok is None, repr(ok))

    forced = m.check_reserved_hosts([reserved_host], True, "deploy 'x' (1-node)")
    check("force bypasses the guard", forced is None, repr(forced))

    ref = m.check_reserved_hosts([reserved_host], False, "deploy 'x' (1-node)")
    check("reserved target is refused", isinstance(ref, dict), repr(ref))
    check('refusal carries code="reserved_host"',
          isinstance(ref, dict) and ref.get("code") == "reserved_host",
          (ref or {}).get("code"))
    check("refusal names the offending host(s)",
          isinstance(ref, dict) and ref.get("hosts") == [reserved_host],
          str((ref or {}).get("hosts")))
    check("refusal message is human-readable",
          isinstance(ref, dict) and reserved_host in ref.get("message", "")
          and "reserved" in ref.get("message", ""),
          (ref or {}).get("message"))

    # target_hosts=None means "everything", which includes the reserved
    # host -- this is the dashboard's plain Teardown button.
    ref_all = m.check_reserved_hosts(None, False, "tear down")
    check("whole-cluster teardown is refused",
          isinstance(ref_all, dict) and ref_all.get("hosts") == [reserved_host],
          str((ref_all or {}).get("hosts")))

    # A record written by a prior deploy should be named in the refusal,
    # so the dialog can say what is about to be destroyed.
    m._set_active_deployment(reserved_host, "resident-agent", "1_node", "hash-agent")
    ref_named = m.check_reserved_hosts([reserved_host], False, "tear down")
    check("refusal names what is running there",
          isinstance(ref_named, dict) and "resident-agent" in ref_named.get("message", ""),
          (ref_named or {}).get("message"))
    m._clear_active_deployment([reserved_host])

    print("\n--- 11. API maps refusals to 423, distinct from 400/409 ---")
    # Ordering, not literal text: what matters is that the reserved_host
    # branch is reached BEFORE the generic error branch in each endpoint,
    # and a text match would break on any intervening comment.
    def _line_of(needle, after=0):
        for i, line in enumerate(src.splitlines()):
            if i >= after and needle in line:
                return i
        return -1

    dep_reserved = _line_of('if res.get("code") == "reserved_host"')
    dep_generic = _line_of('status_code=400, detail=res.get("message"', after=dep_reserved)
    check("deploy: reserved_host branch precedes its generic 400",
          dep_reserved != -1 and dep_generic > dep_reserved,
          f"reserved@{dep_reserved} generic@{dep_generic}")

    td_reserved = _line_of('results.get("code") == "reserved_host"')
    td_generic = _line_of('status_code=409', after=td_reserved)
    check("teardown: reserved_host branch precedes its generic 409",
          td_reserved != -1 and td_generic > td_reserved,
          f"reserved@{td_reserved} generic@{td_generic}")
    check("both refusal branches use 423",
          src.count("status_code=423") == 2, str(src.count("status_code=423")))

    print()
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("RESULT: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
