#!/usr/bin/env python3
"""
Head-to-head AGENTIC eval (realistic workflow, not a trivial Q).
2026-09-06: compares single-Spark brain candidates (35B-A3B vs Gemma4-26B-A4B)
on the kind of work an agent brain actually does:
  A. MULTI-HOP READ:  read 2 files, correlate, answer (tool-calling chain)
  B. BUG-FIX:         find a buggy .py via tools, emit corrected code; the
                      emitted code is EXECUTED against hidden tests.
  C. AGENTIC SPEED:   sustained decode on a longer agentic-style generation
                      (512 tokens) for a fair tps comparison.
Usage: python3 h2h_agentic.py --host 10.0.14.41 --port 8000 [--model <id>]
Writes /tmp/h2h_last.json and prints a compact scorecard.
"""
import argparse, json, urllib.request, urllib.error, time, re, os, sys

# ---- mock FS (a realistic little project) ----
FS = {
    "/proj": ["config.py", "utils.py", "main.py", "README.md"],
    "/proj/config.py": "DB_PORT = 5432\nCACHE_TTL = 300\nMAX_WORKERS = 16\n",
    "/proj/utils.py":
        "def clamp(v, lo, hi):\n"
        "    if v < lo:\n"
        "        return lo\n"
        "    if v > hi:\n"
        "        return hi\n"
        "    return v\n",
    "/proj/main.py":
        "from utils import clamp\n"
        "from config import MAX_WORKERS\n"
        "\n"
        "def scale_workers(base, load):\n"
        "    # BUG: integer division floors, and clamps to the wrong bound\n"
        "    return int(base * load) // MAX_WORKERS\n",
    "/proj/README.md": "scale_workers should be at most MAX_WORKERS.\n",
}

TOOLS = [
    {"type": "function", "function": {"name": "list_dir", "description": "List a directory",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
]

def fs_tool(name, args_str):
    try:
        a = json.loads(args_str) if args_str else {}
    except Exception:
        return json.dumps({"error": "bad json"})
    if name == "list_dir":
        p = a.get("path", "/")
        if p in FS:
            return json.dumps(FS[p])
        # forgiving hint (real agents get this from a shell); still a genuine test
        return json.dumps({"error": "no such dir", "hint": "the project root is /proj"})
    if name == "read_file":
        p = a.get("path", "")
        if p in FS:
            return json.dumps(FS[p])
        return json.dumps({"error": "not found", "hint": "try /proj/<file>"})
    return json.dumps({"error": "unknown tool " + name})

def chat(host, port, model, messages, tools=None, tool_choice=None, max_tokens=1500, temp=0.0):
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens,
               "temperature": temp, "stream": True, "stream_options": {"include_usage": True}}
    if tools:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    req = urllib.request.Request(f"http://{host}:{port}/v1/chat/completions",
        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    content = ""; reasoning = ""; tool_calls = {}; finish = None; nu = 0
    try:
        r = urllib.request.urlopen(req, timeout=600)
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s: %s" % (e.code, e.read().decode(errors="replace")[:400]))
    with r:
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data: "):
                continue
            s = line[6:].strip()
            if s == "[DONE]":
                break
            try:
                c = json.loads(s)
            except Exception:
                continue
            ch = c.get("choices", [])
            if not ch:
                continue
            d = ch[0].get("delta", {})
            if d.get("content"):
                content += d["content"]
            if d.get("reasoning_content") or d.get("reasoning"):
                reasoning += (d.get("reasoning_content") or d.get("reasoning"))
            tc = d.get("tool_calls")
            if tc:
                for t in tc:
                    idx = t.get("index", 0)
                    tool_calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if t.get("id"):
                        tool_calls[idx]["id"] = t["id"]
                    fn = t.get("function", {})
                    if fn.get("name"):
                        tool_calls[idx]["name"] = fn["name"]
                    if fn.get("arguments"):
                        tool_calls[idx]["args"] += fn["arguments"]
            if ch[0].get("finish_reason"):
                finish = ch[0]["finish_reason"]
            u = c.get("usage")
            if u and u.get("completion_tokens"):
                nu = u["completion_tokens"]
    return content, reasoning, tool_calls, finish, nu

def run_loop(host, port, model, system, user, max_turns=8, max_tokens=1500):
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    seq = []; tr = ""; tc = ""; tn = 0
    for turn in range(max_turns):
        content, reasoning, tcs, finish, nu = chat(host, port, model, msgs, TOOLS, "auto", max_tokens)
        tr += reasoning; tc += content; tn += nu
        if tcs:
            for t in tcs.values():
                seq.append({"turn": turn, "tool": t["name"], "args": t["args"]})
                msgs.append({"role": "assistant", "content": None, "tool_calls": [
                    {"id": t["id"], "type": "function",
                     "function": {"name": t["name"], "arguments": t["args"]}}]})
                msgs.append({"role": "tool", "tool_call_id": t["id"],
                             "content": fs_tool(t["name"], t["args"])})
            continue
        return {"answer": content, "finish": finish, "seq": seq,
                "reasoning_len": len(tr), "content_len": len(tc), "tokens": tn, "turns": turn + 1}
    return {"answer": tc, "finish": "max_turns", "seq": seq,
            "reasoning_len": len(tr), "content_len": len(tc), "tokens": tn, "turns": max_turns}

def score_bugfix(answer):
    """Extract the fixed main.py and check the two defects are gone."""
    m = re.search(r"```(?:python)?\s*(.*?)```", answer, re.S)
    code = (m.group(1) if m else answer)
    bugs_gone = ("// MAX_WORKERS" not in code and "int(base * load)" not in code)
    uses_clamp = ("clamp" in code)
    # functional: define a scale_workers if it parses
    functional = False
    ns = {}
    try:
        # provide deps so the emitted function can run
        ns["clamp"] = lambda v, lo, hi: max(lo, min(hi, v))
        ns["MAX_WORKERS"] = 16
        exec(code, ns)
        f = ns.get("scale_workers")
        if f:
            # expect at most MAX_WORKERS and reasonable scaling
            vals = [f(8, 1.0), f(8, 2.0), f(1, 0.5)]
            functional = all(v <= 16 for v in vals)
    except Exception:
        functional = False
    return {"code_present": bool(m or "def scale_workers" in code),
            "bugs_gone": bugs_gone, "uses_clamp": uses_clamp, "functional": functional}

def agentic_speed(host, port, model):
    t0 = time.time(); first = None; n = 0
    payload = {"model": model,
        "messages": [{"role": "user", "content":
            "You are a coding agent. Describe a step-by-step plan (no code) for refactoring "
            "a large monolithic Python service into a clean layered architecture with "
            "dependency injection, testing strategy, and a migration path. Be thorough."}],
        "max_tokens": 512, "temperature": 0.7, "stream": True}
    req = urllib.request.Request(f"http://{host}:{port}/v1/chat/completions",
        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    end = t0
    with urllib.request.urlopen(req, timeout=300) as r:
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data: "):
                continue
            s = line[6:].strip()
            if s == "[DONE]":
                break
            try:
                ch = json.loads(s).get("choices", [{}])[0]
            except Exception:
                continue
            d = ch.get("delta", {})
            if first is None and (d.get("content") or d.get("reasoning") or d.get("reasoning_content")):
                first = time.time()
            n += 1
            end = time.time()
    gen = (end - first) if first else (end - t0)
    tps = (n - 1) / gen if gen > 0 else 0
    return {"tps": round(tps, 1), "ttft": round((first - t0), 2) if first else None,
            "deltas": n, "gen_s": round(gen, 2)}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="10.0.14.41")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--model", default=None)
    args = ap.parse_args()
    if not args.model:
        with urllib.request.urlopen(f"http://{args.host}:{args.port}/v1/models", timeout=5) as r:
            args.model = json.load(r)["data"][0]["id"]
    if os.path.exists("/tmp/h2h_last.json"):
        os.remove("/tmp/h2h_last.json")
    print("=== H2H agentic eval: %s ===" % args.model, flush=True)
    out = {"model": args.model}
    t0 = time.time()

    # A. multi-hop read (needs to read config.py + README.md, correlate MAX_WORKERS=16)
    mh = run_loop(args.host, args.port, args.model,
        "You have list_dir and read_file. Answer precisely from the files; cite the value.",
        "What is the maximum number of workers the project allows, and which file sets the "
        "default DB port? Answer in one line: 'max_workers=<n> (file), db_port=<p> (file)'.",
        max_tokens=600)
    mh_ok = ("16" in mh["answer"] and "5432" in mh["answer"])
    out["multi_hop"] = {"ok": mh_ok, "answer": mh["answer"][:160], "finish": mh["finish"],
                        "turns": mh["turns"], "n_tool_calls": len(mh["seq"]),
                        "reasoning_len": mh["reasoning_len"]}
    print("[A] multi_hop ok=%s turns=%d calls=%d :: %s" %
          (mh_ok, mh["turns"], len(mh["seq"]), mh["answer"][:100]), flush=True)

    # B. bug-fix via tools
    bf = run_loop(args.host, args.port, args.model,
        "You are a senior dev. You have list_dir and read_file. Inspect /proj, find the bug in "
        "scale_workers (see README for the intended constraint), and return the FULL corrected "
        "main.py in one ```python fence. No explanation.",
        "Find and fix the bug in /proj/main.py. Return the corrected main.py.",
        max_tokens=2000)
    sc = score_bugfix(bf["answer"])
    out["bugfix"] = {**sc, "turns": bf["turns"], "n_tool_calls": len(bf["seq"]),
                     "finish": bf["finish"], "reasoning_len": bf["reasoning_len"],
                     "answer_len": len(bf["answer"])}
    print("[B] bugfix bugs_gone=%s functional=%s turns=%d calls=%d" %
          (sc["bugs_gone"], sc["functional"], bf["turns"], len(bf["seq"])), flush=True)

    # C. agentic speed
    sp = agentic_speed(args.host, args.port, args.model)
    out["speed"] = sp
    print("[C] speed tps=%.1f ttft=%s" % (sp["tps"], sp["ttft"]), flush=True)

    out["elapsed_s"] = round(time.time() - t0, 1)
    print("\n=== SCORECARD ===")
    print(json.dumps(out, indent=2))
    with open("/tmp/h2h_last.json", "w") as f:
        json.dump(out, f, indent=2)

if __name__ == "__main__":
    main()
