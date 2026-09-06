#!/usr/bin/env python3
"""Sweep-2: A/B the 35B-A3B tuning knobs (front-runner). Fast (35B cached on spark-3).
Each variant: teardown spark-3, deploy, wait health, benchmark (3 pass), agentic eval.
Records to sweep2_results.tsv. Strictly spark-3 (reserved guard protects spark-4)."""
import json, subprocess, sys, time, os

ORCH = "/home/ian/docker/orchestrator"
HOST, PORT = "10.0.14.41", 8000
AGENTS = os.path.join(ORCH, "tests", "agentic_eval.py")
RESULTS = os.path.join(ORCH, "sweep2_results.tsv")
TRANS = os.path.join(ORCH, "sweep2_transcript.log")

# (recipe key, served-model substring, label)
CANDS = [
    ("_qwen-3.6-35b-a3b-nvfp4",        "Qwen3.6-35B-A3B-NVFP4", "base (n=3, moe auto)"),
    ("_qwen-3.6-35b-a3b-nvfp4-latency","Qwen3.6-35B-A3B-NVFP4", "moe-backend=latency + sm_121a"),
    ("_qwen-3.6-35b-a3b-nvfp4-mtp6",   "Qwen3.6-35B-A3B-NVFP4", "MTP n=6 (deeper draft)"),
]

def log(m):
    l = time.strftime("%H:%M:%S") + " " + m
    print(l, flush=True)
    open(TRANS, "a").write(l + "\n")

def sh(cmd, timeout=1800):
    try:
        r = subprocess.run(cmd, shell=True, cwd=ORCH, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        return -9, "TIMEOUT " + str(e)

def wait_up(timeout=1500):
    t0 = time.time()
    while time.time() - t0 < timeout:
        rc, out = sh("curl -s -m 5 http://%s:%d/health" % (HOST, PORT), timeout=30)
        if rc == 0 and ("OK" in out or out.strip() == ""):
            return True
        time.sleep(15)
    return False

def deploy(model):
    log("  teardown spark-3")
    sh("python3 dgx-orchestrator.py teardown --host spark-3 2>&1", timeout=300)
    log("  deploy %s" % model)
    sh("python3 dgx-orchestrator.py deploy --model %s --nodes 1 --head spark-3 --wait -y 2>&1" % model, timeout=3600)
    return wait_up(timeout=1500)

def bench():
    rc, out = sh("python3 benchmark.py --host %s --port %d --nodes 1 --model-key 35b-sweep2 --max-tokens 512 2>&1" % (HOST, PORT), timeout=900)
    tps = ttft = ""
    for ln in out.splitlines():
        if "Warm Avg" in ln and "Decode Speed" in ln:
            try: tps = ln.split("Decode Speed:")[1].split(" tok/s")[0].strip()
            except Exception: pass
        if "Warm Avg" in ln and "TTFT" in ln:
            try: ttft = ln.split("TTFT")[1].split("|")[0].strip().rstrip("s").strip()
            except Exception: pass
    log("  bench tps=%s ttft=%ss" % (tps, ttft))
    return tps, ttft

def agentic():
    sh("python3 %s --host %s --port %d 2>&1" % (AGENTS, HOST, PORT), timeout=900)
    try: d = json.load(open("/tmp/last_agentic_eval.json"))
    except Exception: d = {}
    tl = d.get("tool_loop", {}); co = d.get("coding", {}); bu = d.get("budget", {})
    tool_ok = "8080" in tl.get("answer", "")
    log("  agentic tool_ok=%s coding=%s/%s budget_reasoning=%s" %
        (tool_ok, co.get("passed"), co.get("total"), bu.get("reasoning_len")))
    return tool_ok, "%s/%s" % (co.get("passed"), co.get("total")), bu.get("reasoning_len", "?")

def main():
    open(RESULTS, "w").write("variant\tmodel\tdeploy_tps\tttft_s\ttool_loop_ok\tcoding\tbudget_reasoning\tts\n")
    log("=== SWEEP2 START ===")
    for model, sub, label in CANDS:
        log("=== %s (%s) ===" % (model, label))
        if not deploy(model) or not wait_up(60):
            log("  NOT UP -> skip")
            open(RESULTS, "a").write("%s\t%s\tFAILED\t\t\t\t\t%s\n" % (label, model, time.strftime("%H:%M")))
            continue
        tps, ttft = bench()
        tool_ok, code_score, budget_r = agentic()
        open(RESULTS, "a").write("%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" %
            (label, model, tps, ttft, tool_ok, code_score, budget_r, time.strftime("%H:%M")))
        log("  recorded %s tps=%s tool_ok=%s coding=%s" % (label, tps, tool_ok, code_score))
    log("=== SWEEP2 DONE ===")

if __name__ == "__main__":
    main()
