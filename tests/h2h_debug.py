import json, sys
sys.path.insert(0, "/home/ian/docker/orchestrator/tests")
import h2h_agentic as H
host, port, model = "10.0.14.41", "8000", "nvidia/Gemma-4-26B-A4B-NVFP4"

mh = H.run_loop(host, port, model,
    "You have list_dir and read_file. Answer precisely from the files; cite the value.",
    "What is the maximum number of workers the project allows, and which file sets the default DB port? One line: max_workers=<n> (file), db_port=<p> (file).",
    max_turns=8, max_tokens=600)
print("=== MULTI-HOP (Gemma4) ===")
print("turns:", mh["turns"], "finish:", mh["finish"])
for i, s in enumerate(mh["seq"]):
    print("  turn%d: %s %s" % (i, s["tool"], s["args"]))
print("answer:", repr(mh["answer"][:200]))

bf = H.run_loop(host, port, model,
    "You are a senior dev. You have list_dir and read_file. Inspect /proj, find the bug in scale_workers (see README for the intended constraint), and return the FULL corrected main.py in one python fence. No explanation.",
    "Find and fix the bug in /proj/main.py. Return the corrected main.py.", max_tokens=2000)
print("\n=== BUGFIX emitted code (Gemma4) ===")
print(bf["answer"][:700])
