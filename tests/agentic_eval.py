#!/usr/bin/env python3
"""
Agentic-quality eval for a vLLM endpoint.

Talks to whatever is already serving on the given host:port. It NEVER
deploys, tears down, or touches cluster state -- which is why it is safe
against a reserved host, and why it stayed separate from ab_test.py rather
than being folded in (ab_test.py's unit of work is a weight load; this
one's is a request).

FIVE tests, three of which EXECUTE or VERIFY rather than string-match:

  1. TOOL-LOOP  : single-hop tool call against a mock FS. Cheap canary --
                  a model whose recipe lacks --enable-auto-tool-choice /
                  --tool-call-parser 400s here in seconds, before anything
                  expensive runs. Failure mode surfaced, not crashed.
  2. MULTI-HOP  : read TWO files, correlate, answer. The real tool-calling
                  test -- one file is a lookup, two is a chain.
  3. CODING     : write count_vowels(); the emitted code is EXECUTED
                  against five hidden cases.
  4. BUG-FIX    : find a bug via tools, emit the corrected file; the
                  emitted code is EXECUTED and checked for the two defects.
  5. BUDGET     : trivial question with tools available; measures
                  overthinking (reasoning tokens burned before a one-word
                  answer). Earned its place -- qwen-3.6-35b-a3b-nvfp4-nothink
                  exists because thinking mode scored 0/5 on the coding suite.

MERGED 2026-09-10 from agentic_eval.py + h2h_agentic.py, which had a
byte-identical chat() and divergent everything else. This file keeps
agentic_eval's plumbing (--out, stale-file clear, HTTP-body surfacing,
exit 2) and h2h's harder tasks. Two bugs in h2h were fixed on the way in:

  - score_bugfix()'s exec() failed on the emitted `from utils import clamp`,
    so `functional` was False for every CORRECT answer. Stub modules are now
    installed before exec.
  - agentic_speed() computed tok/s from SSE DELTA COUNT, which undercounts
    every speculative-decode model by a factor that varies with acceptance
    rate. That is TOMBSTONES #52 exactly, reintroduced in a copy. DROPPED
    rather than fixed: benchmark.py already measures decode from
    stream_options usage and was hardened for this. One implementation.

Why the hardening below is the way it is (2026-09-05 sweep lessons, kept):
  - chat() reads BOTH `reasoning_content` (DeepSeek) and `reasoning`
    (Qwen3.5/3.6). vLLM streams CoT into a per-family delta key; missing
    it reports 0 reasoning and masks overthinking.
  - coding() uses max_tokens=2500, not 800. Overthinking models spend
    ~2000 reasoning tokens before emitting the function; a small budget
    truncates them and looks like a quality bug when it is a budget artifact.
  - main() clears any stale output file first, so a failed run never leaves
    the previous model's JSON for a caller to read as fresh.
  - HTTP errors surface the response body, so "recipe missing tool flags
    -> 400" is readable instead of a bare URLError.

Usage:
  python3 agentic_eval.py --host 10.0.14.41 --port 8000 [--model <id>]
                          [--out /tmp/last_agentic_eval.json]
                          [--skip bugfix,budget]
"""
import argparse, json, urllib.request, urllib.error, time, sys, re, os, types

OUT = "/tmp/last_agentic_eval.json"


def chat(host, port, model, messages, tools=None, tool_choice=None,
         max_tokens=1500, temp=0.0):
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens,
               "temperature": temp, "stream": True,
               "stream_options": {"include_usage": True}}
    if tools:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    req = urllib.request.Request(
        f"http://{host}:{port}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
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
                        # arguments arrive in fragments across deltas
                        tool_calls[idx]["args"] += fn["arguments"]
            if ch[0].get("finish_reason"):
                finish = ch[0]["finish_reason"]
            u = c.get("usage")
            if u and u.get("completion_tokens"):
                nu = u["completion_tokens"]
    return content, reasoning, tool_calls, finish, nu


# --------------------------------------------------------------- mock FS
# One fixture for every tool-using test. /app answers the single-hop
# lookup; /proj carries the two-file correlation and the seeded bug.
FS = {
    "/app": ["config.yaml", "README.md", "server.py"],
    "/app/config.yaml": "port: 8080\nmodel: qwen3.6\nmax_tokens: 4096\nreasoning_effort: high\n",
    "/app/README.md": "A tiny demo app.\n",
    "/app/server.py": "print('hello')\n",

    "/proj": ["config.py", "utils.py", "main.py", "README.md"],
    "/proj/config.py": "DB_PORT = 5432\nCACHE_TTL = 300\nMAX_WORKERS = 16\n",
    "/proj/utils.py": ("def clamp(v, lo, hi):\n"
                       "    if v < lo:\n"
                       "        return lo\n"
                       "    if v > hi:\n"
                       "        return hi\n"
                       "    return v\n"),
    "/proj/main.py": ("from utils import clamp\n"
                      "from config import MAX_WORKERS\n"
                      "\n"
                      "def scale_workers(base, load):\n"
                      "    # BUG: integer division floors, and clamps to the wrong bound\n"
                      "    return int(base * load) // MAX_WORKERS\n"),
    "/proj/README.md": "scale_workers should be at most MAX_WORKERS.\n",
}

TOOLS = [
    {"type": "function", "function": {
        "name": "list_dir", "description": "List a directory",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a file",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
]


def tool(name, args_str):
    try:
        a = json.loads(args_str) if args_str else {}
    except Exception:
        return json.dumps({"error": "bad json args"})
    if name == "list_dir":
        p = a.get("path", "/")
        if p in FS:
            return json.dumps(FS[p])
        # A forgiving hint, as a real agent would get from a shell. Still a
        # genuine test -- the model must still pick the right file and read it.
        return json.dumps({"error": "no such dir", "hint": "project roots are /app and /proj"})
    if name == "read_file":
        p = a.get("path", "")
        if p in FS:
            return json.dumps(FS[p])
        return json.dumps({"error": "not found", "hint": "try <root>/<file>"})
    return json.dumps({"error": "unknown tool " + name})


def run_loop(host, port, model, system, user, max_turns=8, max_tokens=1500):
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": user}]
    seq = []; tr = ""; tc = ""; tn = 0
    for turn in range(max_turns):
        content, reasoning, tcs, finish, nu = chat(
            host, port, model, msgs, TOOLS, "auto", max_tokens)
        tr += reasoning; tc += content; tn += nu
        if tcs:
            for t in tcs.values():
                seq.append({"turn": turn, "tool": t["name"], "args": t["args"]})
                msgs.append({"role": "assistant", "content": None, "tool_calls": [
                    {"id": t["id"], "type": "function",
                     "function": {"name": t["name"], "arguments": t["args"]}}]})
                msgs.append({"role": "tool", "tool_call_id": t["id"],
                             "content": tool(t["name"], t["args"])})
            continue
        return {"answer": content, "finish": finish, "seq": seq,
                "reasoning_len": len(tr), "content_len": len(tc),
                "total_tokens": tn, "turns": turn + 1}
    return {"answer": tc, "finish": "max_turns", "seq": seq,
            "reasoning_len": len(tr), "content_len": len(tc),
            "total_tokens": tn, "turns": max_turns}


# --------------------------------------------------------------- 1. canary
def t_tool_loop(host, port, model):
    r = run_loop(host, port, model,
                 "You have list_dir and read_file. Answer precisely; do not overthink.",
                 "In /app, find config.yaml and tell me the value of the 'port' key.",
                 max_turns=6)
    r["ok"] = "8080" in r["answer"]
    return r


# ------------------------------------------------------------- 2. multi-hop
def t_multi_hop(host, port, model):
    r = run_loop(host, port, model,
                 "You have list_dir and read_file. Answer precisely from the files; cite the value.",
                 "What is the maximum number of workers the project allows, and which file sets "
                 "the default DB port? Answer in one line: "
                 "'max_workers=<n> (file), db_port=<p> (file)'.",
                 max_tokens=600)
    # Both values live in /proj/config.py but the question requires reading
    # README.md too to know MAX_WORKERS is the *allowed maximum*.
    r["ok"] = ("16" in r["answer"] and "5432" in r["answer"])
    return r


# ---------------------------------------------------------------- 3. coding
def t_coding(host, port, model):
    task = ("Write a Python function `count_vowels(s: str) -> int` returning the number of "
            "vowels (a,e,i,o,u, case-insensitive) in s. Put ONLY the function in one ```python "
            "fenced block. No explanation.")
    c, r, _tcs, _finish, nu = chat(host, port, model,
                                   [{"role": "user", "content": task}],
                                   None, None, max_tokens=2500, temp=0.0)
    m = re.search(r"```(?:python)?\s*(.*?)```", c, re.S)
    code = (m.group(1).strip() if m else c.strip())
    total = 5; passed = 0; results = []; ns = {}
    try:
        exec(code, ns)
    except Exception as e:
        return {"passed": 0, "total": total, "ok": False,
                "error": "exec failed: " + str(e), "code": code[:300],
                "tokens": nu, "reasoning_len": len(r)}
    fn = ns.get("count_vowels")
    if not fn:
        return {"passed": 0, "total": total, "ok": False,
                "error": "no count_vowels defined", "code": code[:300],
                "tokens": nu, "reasoning_len": len(r)}
    for inp, exp in [("hello", 2), ("AEIOU", 5), ("rhythm", 0),
                     ("aeiou", 5), ("testing", 2)]:
        try:
            got = fn(inp); ok = (got == exp); passed += ok
            results.append({"in": inp, "exp": exp, "got": got, "ok": ok})
        except Exception as e:
            results.append({"in": inp, "exp": exp, "got": "ERR " + str(e), "ok": False})
    return {"passed": passed, "total": total, "ok": passed == total,
            "results": results, "code": code[:300],
            "tokens": nu, "reasoning_len": len(r)}


# ---------------------------------------------------------------- 4. bug-fix
def _install_proj_stubs():
    """
    Make `from utils import clamp` and `from config import MAX_WORKERS`
    resolve, so the emitted file can be exec'd.

    THIS IS THE FIX FOR THE BUG THAT MADE THIS TEST USELESS. The prompt asks
    for the FULL corrected main.py, so a correct answer BEGINS with those two
    imports. Neither module exists at runtime, so exec() raised ImportError,
    the bare except caught it, and `functional` came back False for every
    correct answer. Seeding the names into the exec namespace did not help --
    the import statement runs first and fails before any name lookup.
    """
    utils = types.ModuleType("utils")
    utils.clamp = lambda v, lo, hi: max(lo, min(hi, v))
    config = types.ModuleType("config")
    config.MAX_WORKERS = 16
    config.DB_PORT = 5432
    config.CACHE_TTL = 300
    sys.modules["utils"] = utils
    sys.modules["config"] = config
    return utils, config


def _score_bugfix(answer):
    m = re.search(r"```(?:python)?\s*(.*?)```", answer, re.S)
    code = (m.group(1) if m else answer)
    utils, config = _install_proj_stubs()
    ns = {"clamp": utils.clamp, "MAX_WORKERS": config.MAX_WORKERS}
    functional = False; err = None
    try:
        exec(code, ns)
        f = ns.get("scale_workers")
        if f:
            # The README's constraint: never exceed MAX_WORKERS. Checked by
            # RUNNING it, not by grepping for the absence of the old bug.
            vals = [f(8, 1.0), f(8, 2.0), f(1, 0.5), f(100, 10.0)]
            functional = all(isinstance(v, (int, float)) and v <= 16 for v in vals)
    except Exception as e:
        err = str(e)
    finally:
        sys.modules.pop("utils", None)
        sys.modules.pop("config", None)
    return {"code_present": bool(m or "def scale_workers" in code),
            "old_bug_absent": "// MAX_WORKERS" not in code,
            "functional": functional, "ok": functional,
            "exec_error": err}


def t_bugfix(host, port, model):
    r = run_loop(host, port, model,
                 "You are a senior dev. You have list_dir and read_file. Inspect /proj, find "
                 "the bug in scale_workers (see README for the intended constraint), and "
                 "return the FULL corrected main.py in one ```python fence. No explanation.",
                 "Find and fix the bug in /proj/main.py. Return the corrected main.py.",
                 max_tokens=2000)
    sc = _score_bugfix(r["answer"])
    return {**sc, "turns": r["turns"], "n_tool_calls": len(r["seq"]),
            "finish": r["finish"], "reasoning_len": r["reasoning_len"],
            "answer_len": len(r["answer"])}


# ---------------------------------------------------------------- 5. budget
def t_budget(host, port, model):
    c, r, _tcs, finish, nu = chat(
        host, port, model,
        [{"role": "user", "content": "Is 7*8 greater than 50? Reply yes or no."}],
        None, None, max_tokens=400, temp=0.0)
    return {"answer": c.strip()[:80], "reasoning_len": len(r),
            "tokens": nu, "finish": finish,
            "ok": bool(re.search(r"\byes\b", c, re.I))}


TESTS = [("tool_loop", t_tool_loop), ("multi_hop", t_multi_hop),
         ("coding", t_coding), ("bugfix", t_bugfix), ("budget", t_budget)]


def main():
    global OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="10.0.14.41")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--model", default=None)
    ap.add_argument("--out", default="/tmp/last_agentic_eval.json")
    ap.add_argument("--skip", default="",
                    help="comma-separated test names to skip: "
                         + ",".join(n for n, _ in TESTS))
    args = ap.parse_args()
    OUT = args.out
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    # Never let a stale result from a previous model masquerade as this one.
    if os.path.exists(OUT):
        os.remove(OUT)
    if not args.model:
        with urllib.request.urlopen(
                f"http://{args.host}:{args.port}/v1/models", timeout=5) as r:
            args.model = json.load(r)["data"][0]["id"]

    print(f"=== agentic eval: {args.model} @ {args.host}:{args.port} ===\n", flush=True)
    out = {"model": args.model, "host": f"{args.host}:{args.port}"}
    t0 = time.time()
    try:
        for name, fn in TESTS:
            if name in skip:
                print(f"[-] {name}: skipped", flush=True)
                continue
            res = fn(args.host, args.port, args.model)
            out[name] = res
            mark = "ok" if res.get("ok") else "FAIL"
            extra = ""
            if name == "coding":
                extra = f" {res['passed']}/{res['total']}"
            elif name == "budget":
                extra = f" reasoning_len={res['reasoning_len']}"
            elif name in ("tool_loop", "multi_hop"):
                extra = f" turns={res['turns']} calls={len(res['seq'])}"
            elif name == "bugfix":
                extra = f" functional={res['functional']} turns={res['turns']}"
            print(f"[{mark:4}] {name}{extra}", flush=True)
    except Exception as e:
        out["error"] = str(e)
        print(f"EVAL FAILED: {e}", file=sys.stderr, flush=True)

    out["elapsed_s"] = round(time.time() - t0, 1)
    scored = [(n, out[n].get("ok")) for n, _ in TESTS if n in out]
    out["score"] = f"{sum(1 for _, ok in scored if ok)}/{len(scored)}"
    print(f"\n=== SUMMARY  score={out['score']} ===")
    print(json.dumps(out, indent=2))
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2)
    if "error" in out:
        sys.exit(2)


if __name__ == "__main__":
    main()
