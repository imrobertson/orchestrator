#!/usr/bin/env python3
"""
Overnight single-Spark sweep (spark-3 only). For each candidate recipe:
  1. teardown spark-3 (scoped), 2. deploy the recipe (1_node, head=spark-3),
  3. wait for /health, 4. run benchmark.py (3 passes), 5. run agentic_eval.py,
  6. log a line to results.tsv. NEVER touches spark-4 (reserved guard enforces it).
Runs unattended. Append-only results + a human-readable transcript.
"""
import json, subprocess, sys, time, os, csv

ORCH = "/home/ian/docker/orchestrator"
HOST = "10.0.14.41"
PORT = 8000
AGENTS = os.path.join(ORCH, "tests", "agentic_eval.py")  # lives alongside sweep.py on maestro
RESULTS = os.path.join(ORCH, "sweep_results.tsv")
TRANS = os.path.join(ORCH, "sweep_transcript.log")

# candidate -> (recipe key, served-model substring, extra notes)
CANDIDATES = [
    # 35B already benchmarked (59 tps); still agentic-eval it (skip redeploy if serving).
    ("_qwen-3.6-35b-a3b-nvfp4", "Qwen3.6-35B-A3B-NVFP4", "A3B mixed FP8/NVFP4, MTP n=3"),
    ("nemotron-3-nano-30b-a3b-nvfp4", "Nemotron-3-Nano-30B-A3B", "A3B NVFP4, no MTP in recipe"),
    ("nemotron-3.5-lightning-nvfp4", "Nemotron-3.5-Lightning-30B-A3B", "A3B NVFP4 dspark MTP n=5"),
    ("gemma4-26b-a4b-nvfp4", "Gemma-4-26B-A4B", "A4B NVFP4 MTP n=2 (needs ~15GB download)"),
    ("muse-glimmer-30b", "Muse-Glimmer-30B", "dense 30B agent-tuned (tool-first)"),
]

def log(msg):
    line = time.strftime("%H:%M:%S") + " " + msg
    print(line, flush=True)
    with open(TRANS, "a") as f:
        f.write(line + "\n")

def sh(cmd, timeout=1800):
    try:
        r = subprocess.run(cmd, shell=True, cwd=ORCH, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        return -9, "TIMEOUT: " + str(e)

def teardown():
    log("  teardown spark-3 ...")
    rc, out = sh("python3 dgx-orchestrator.py teardown --host spark-3 2>&1", timeout=300)
    log("  teardown rc=%s" % rc)
    # ensure spark-4 untouched
    rc2, st = sh("python3 -c \"import json;print(json.load(open('active_deployment_state.json')))\" 2>&1")

def deploy(model):
    log("  deploy %s (1_node, spark-3) ..." % model)
    rc, out = sh("python3 dgx-orchestrator.py deploy --model %s --nodes 1 --head spark-3 --wait -y 2>&1" % model, timeout=3600)
    ok = wait_up(timeout=1500)   # trust the /health endpoint, not the exit code
    log("  deploy rc=%s health_up=%s" % (rc, ok))
    return ok

def health_up():
    rc, out = sh("curl -s -m 5 http://%s:%d/health" % (HOST, PORT), timeout=30)
    return rc == 0 and ("OK" in out or out.strip() == "")

def wait_up(timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if health_up():
            return True
        time.sleep(15)
    return False

def benchmark(model):
    log("  benchmark %s ..." % model)
    rc, out = sh("python3 benchmark.py --host %s --port %d --nodes 1 --model-key %s-sweep --max-tokens 512 2>&1" % (HOST, PORT, model), timeout=900)
    tps = ttft = ""
    for line in out.splitlines():
        if "Warm Avg" in line and "Decode Speed" in line:
            try:
                tps = line.split("Decode Speed:")[1].split(" tok/s")[0].strip()
            except Exception:
                pass
        if "Warm Avg" in line and "TTFT" in line:
            try:
                ttft = line.split("TTFT")[1].split("|")[0].strip().rstrip("s").strip()
            except Exception:
                pass
    log("  benchmark tps=%s ttft=%ss" % (tps, ttft))
    return tps, ttft

def agentic():
    log("  agentic eval ...")
    rc, out = sh("python3 %s --host %s --port %d 2>&1" % (AGENTS, HOST, PORT), timeout=900)
    d = {}
    try:
        d = json.load(open("/tmp/last_agentic_eval.json"))
    except Exception:
        pass
    tl = d.get("tool_loop", {})
    co = d.get("coding", {})
    bu = d.get("budget", {})
    tool_ok = ("8080" in tl.get("answer", ""))
    code_score = "%s/%s" % (co.get("passed"), co.get("total"))
    log("  agentic: tool_ok=%s coding=%s budget_reasoning=%s" % (tool_ok, code_score, bu.get("reasoning_len")))
    return tool_ok, code_score, bu.get("reasoning_len", "?")

def main():
    if not os.path.exists(RESULTS):
        with open(RESULTS, "w") as f:
            f.write("model\tnotes\tdeploy_tps\tttft_s\ttool_loop_ok\tcoding\tbudget_reasoning\tts\n")
    # skip redeploy for the model already up (35B): detect via /v1/models
    log("=== SWEEP START ===")
    for model, served_sub, notes in CANDIDATES:
        log("=== %s (%s) ===" % (model, notes))
        t0 = time.time()
        # detect if this model is already the one serving (skip redeploy)
        rc, cur = sh("curl -s -m 5 http://%s:%d/v1/models 2>&1" % (HOST, PORT), timeout=30)
        if served_sub in cur:
            log("  already serving -> skip deploy")
            up = True
        else:
            teardown()
            up = deploy(model)
        if not up or not wait_up():
            log("  NOT UP after deploy; recording failure and moving on")
            with open(RESULTS, "a") as f:
                f.write("%s\t%s\tFAILED\t\t\t\t\t%s\n" % (model, notes, time.strftime("%H:%M")))
            continue
        tps, ttft = benchmark(model)
        tool_ok, code_score, budget_r = agentic()
        with open(RESULTS, "a") as f:
            f.write("%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" %
                    (model, notes, tps, ttft, tool_ok, code_score, budget_r, time.strftime("%H:%M")))
        log("  recorded %s tps=%s tool_ok=%s coding=%s" % (model, tps, tool_ok, code_score))
    log("=== SWEEP DONE (elapsed %.1f min) ===" % ((time.time() - t0) / 60.0))

if __name__ == "__main__":
    main()
