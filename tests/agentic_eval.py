#!/usr/bin/env python3
"""
Agentic-quality eval for a vLLM endpoint (single-Spark sweep, 2026-09-05/06).
Tests THREE things the "brain" needs, on the same model:
  1. TOOL-LOOP : multi-turn tool calling against a mock FS (right tool, right
                 args, right answer). Verifies the endpoint is brain-eligible:
                 a model whose recipe lacks --enable-auto-tool-choice /
                 --tool-call-parser 400s here (surfaced, not crashed).
  2. CODING    : a concrete coding task; extract fenced code and EXECUTE the
                 hidden test cases against it.
  3. BUDGET    : trivial Q with tools available; measure overthinking
                 (reasoning tokens the model burns before a one-word answer).

Why "hardened" (2026-09-05 sweep lessons baked in):
  - `chat()` first-token / reasoning detection reads BOTH `reasoning_content`
    (DeepSeek) and `reasoning` (Qwen3.5/3.6). vLLM streams thinking-model CoT
    into a per-family delta key; missing the key reports 0 reasoning and can
    mask overthinking.
  - coding() uses max_tokens=2500, not 800. Overthinking models spend
    ~2000 reasoning tokens before emitting the function; a small budget
    truncates them (finish: length, 0 code) and looks like a quality bug when
    it's a budget artifact.
  - main() clears any stale /tmp/last_agentic_eval.json before running and
    writes a per-model output file, so a failed run never leaves the previous
    model's JSON for a caller (the sweep) to read as if it were fresh.
  - HTTP errors (400/500) are surfaced with the body instead of a bare
    URLError, so "recipe missing tool flags -> 400" is readable.

Usage:
  python3 agentic_eval.py --host 10.0.14.41 --port 8000 [--model <id>]
                          [--out /tmp/last_agentic_eval.json]
"""
import argparse, json, urllib.request, urllib.error, time, sys, re, os, textwrap

OUT = "/tmp/last_agentic_eval.json"  # set in main(); also honors --out

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
        body = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"HTTP {e.code} from /v1/chat/completions: {body}") from None
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
            # thinking models: Qwen3.5/3.6 -> 'reasoning'; DeepSeek -> 'reasoning_content'
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

FS = {
    "/app": ["config.yaml", "README.md", "server.py"],
    "/app/config.yaml": "port: 8080\nmodel: qwen3.6\nmax_tokens: 4096\nreasoning_effort: high\n",
    "/app/README.md": "A tiny demo app.\n",
    "/app/server.py": "print('hello')\n",
}
def tool(name, args_str):
    try:
        a = json.loads(args_str) if args_str else {}
    except Exception:
        return json.dumps({"error": "bad json args"})
    if name == "list_dir":
        return json.dumps(FS.get(a.get("path", "/"), {"error": "no such dir"}))
    if name == "read_file":
        return json.dumps(FS.get(a.get("path", ""), {"error": "not found"}))
    return json.dumps({"error": "unknown tool " + name})

TOOLS = [
    {"type": "function", "function": {"name": "list_dir", "description": "List a directory",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
]

def tool_loop(host, port, model, question, max_turns=6):
    msgs = [{"role": "system", "content": "You have list_dir and read_file. Answer precisely; do not overthink."},
            {"role": "user", "content": question}]
    seq = []; total_reason = ""; total_content = ""; total_nu = 0
    for turn in range(max_turns):
        content, reasoning, tcs, finish, nu = chat(host, port, model, msgs, TOOLS, "auto")
        total_reason += reasoning; total_content += content; total_nu += nu
        if tcs:
            for t in tcs.values():
                seq.append({"turn": turn, "tool": t["name"], "args": t["args"]})
                msgs.append({"role": "assistant", "content": None, "tool_calls": [
                    {"id": t["id"], "type": "function", "function": {"name": t["name"], "arguments": t["args"]}}]})
                msgs.append({"role": "tool", "tool_call_id": t["id"], "content": tool(t["name"], t["args"])})
            continue
        return {"answer": content, "finish": finish, "seq": seq,
                "reasoning_len": len(total_reason), "content_len": len(total_content),
                "total_tokens": total_nu, "turns": turn + 1}
    return {"answer": total_content, "finish": "max_turns", "seq": seq,
            "reasoning_len": len(total_reason), "content_len": len(total_content),
            "total_tokens": total_nu, "turns": max_turns}

def coding(host, port, model):
    task = ("Write a Python function `count_vowels(s: str) -> int` returning the number of "
            "vowels (a,e,i,o,u, case-insensitive) in s. Put ONLY the function in one ```python "
            "fenced block. No explanation.")
    # 2500 (not 800): overthinking models spend ~2000 reasoning tokens first;
    # a small budget truncates the code (finish: length) and looks like a
    # quality bug when it's a budget artifact. See 2026-09-05 sweep.
    c, r, tcs, finish, nu = chat(host, port, model, [{"role": "user", "content": task}],
                                 None, None, max_tokens=2500, temp=0.0)
    m = re.search(r"```(?:python)?\s*(.*?)```", c, re.S)
    code = (m.group(1).strip() if m else c.strip())
    total = 5; passed = 0; results = []; ns = {}
    try:
        exec(code, ns)
    except Exception as e:
        return {"passed": 0, "total": total, "error": "exec failed: " + str(e),
                "code": code[:300], "tokens": nu, "reasoning_len": len(r)}
    fn = ns.get("count_vowels")
    cases = [("hello", 2), ("AEIOU", 5), ("rhythm", 0), ("aeiou", 5), ("testing", 2)]
    for inp, exp in cases:
        try:
            got = fn(inp); ok = (got == exp); passed += ok
            results.append({"in": inp, "exp": exp, "got": got, "ok": ok})
        except Exception as e:
            results.append({"in": inp, "exp": exp, "got": "ERR " + str(e), "ok": False})
    return {"passed": passed, "total": total, "results": results, "code": code[:300],
            "tokens": nu, "reasoning_len": len(r)}

def budget(host, port, model):
    c, r, tcs, finish, nu = chat(host, port, model,
        [{"role": "user", "content": "Is 7*8 greater than 50? Reply yes or no."}],
        None, None, max_tokens=400, temp=0.0)
    return {"answer": c.strip()[:80], "reasoning_len": len(r), "tokens": nu, "finish": finish}

def main():
    global OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="10.0.14.41")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--model", default=None)
    ap.add_argument("--out", default="/tmp/last_agentic_eval.json")
    args = ap.parse_args()
    OUT = args.out
    # Never let a stale result from a previous model masquerade as this one.
    if os.path.exists(OUT):
        os.remove(OUT)
    if not args.model:
        with urllib.request.urlopen(f"http://{args.host}:{args.port}/v1/models", timeout=5) as r:
            args.model = json.load(r)["data"][0]["id"]
    print("=== agentic eval: %s @ %s:%s ===\n" % (args.model, args.host, args.port), flush=True)
    out = {"model": args.model}
    t0 = time.time()
    try:
        tl = tool_loop(args.host, args.port, args.model,
            "In /app, find config.yaml and tell me the value of the 'port' key.")
        out["tool_loop"] = tl
        print("[1] tool_loop:", json.dumps({k: tl[k] for k in ("answer", "finish", "seq", "reasoning_len", "total_tokens", "turns")}), flush=True)
        co = coding(args.host, args.port, args.model)
        out["coding"] = co
        print("[2] coding: %d/%d%s" % (co["passed"], co["total"], (" ERR " + co["error"]) if co.get("error") else ""), flush=True)
        bu = budget(args.host, args.port, args.model)
        out["budget"] = bu
        print("[3] budget: ans=%r reasoning_len=%d tokens=%d" % (bu["answer"], bu["reasoning_len"], bu["tokens"]), flush=True)
    except Exception as e:
        out["error"] = str(e)
        print("EVAL FAILED: %s" % e, file=sys.stderr, flush=True)
    out["elapsed_s"] = round(time.time() - t0, 1)
    print("\n=== SUMMARY ===")
    print(json.dumps(out, indent=2))
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2)
    if "error" in out:
        sys.exit(2)

if __name__ == "__main__":
    main()
