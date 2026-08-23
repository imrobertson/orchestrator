#!/usr/bin/env python3
"""
TETREL SECURITY - DGX CLUSTER BENCHMARK UTILITY
--------------------------------------------------------------------------------
Accurate MTP & Streaming Throughput Benchmark.
Uses stream_options to extract actual completion_tokens across multi-token SSE chunks.
"""

import argparse
import csv
import datetime
import json
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
BENCHMARK_LEDGER_PATH = BASE_DIR / "benchmark_ledger.csv"
BENCHMARK_RESULTS_PATH = BASE_DIR / "benchmark_results.txt"


def discover_model_id(host: str, port: int) -> str:
    url = f"http://{host}:{port}/v1/models"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=5) as response:
        data = json.loads(response.read().decode("utf-8"))
        models = data.get("data", [])
        if models:
            return models[0]["id"]
    raise RuntimeError(f"No active models found at http://{host}:{port}/v1/models")


def run_benchmark_pass(host: str, port: int, model_id: str, prompt: str, max_tokens: int) -> dict:
    url = f"http://{host}:{port}/v1/chat/completions"
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True}
    }
    
    headers = {"Content-Type": "application/json"}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    start_time = time.perf_counter()
    first_token_time = None
    completion_tokens = 0
    chunk_count = 0

    with urllib.request.urlopen(req, timeout=300) as response:
        for line_bytes in response:
            line = line_bytes.decode("utf-8").strip()
            if not line or not line.startswith("data: "):
                continue
            
            chunk_str = line[6:].strip()
            if chunk_str == "[DONE]":
                break

            try:
                chunk_json = json.loads(chunk_str)
                
                # First Token Time (TTFT)
                choices = chunk_json.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    if delta.get("content"):
                        if first_token_time is None:
                            first_token_time = time.perf_counter()
                        chunk_count += 1

                # Pull exact token count from vLLM usage payload
                usage = chunk_json.get("usage")
                if usage and "completion_tokens" in usage and usage["completion_tokens"] > 0:
                    completion_tokens = usage["completion_tokens"]

            except json.JSONDecodeError:
                continue

    end_time = time.perf_counter()
    total_duration = end_time - start_time

    # Fallback to chunk count if vLLM engine omitted usage header
    if completion_tokens == 0:
        completion_tokens = chunk_count

    if first_token_time is None:
        ttft = total_duration
        decode_duration = 0.0
        decode_tps = 0.0
    else:
        ttft = first_token_time - start_time
        decode_duration = end_time - first_token_time
        decode_tps = completion_tokens / decode_duration if decode_duration > 0 else 0.0

    return {
        "total_duration": total_duration,
        "ttft": ttft,
        "tokens": completion_tokens,
        "decode_duration": decode_duration,
        "decode_tps": decode_tps,
        "overall_tps": completion_tokens / total_duration if total_duration > 0 else 0.0
    }


def main():
    parser = argparse.ArgumentParser(description="vLLM MTP Benchmark Tool")
    parser.add_argument("--host", default="10.0.14.43")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--nodes", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--prompt", default="Write a 200 word technical overview of distributed tensor parallelism.")
    args = parser.parse_args()

    print(f"[+] Connecting to http://{args.host}:{args.port}...")
    model_id = discover_model_id(args.host, args.port)
    print(f"[+] Discovered active served model ID: '{model_id}'\n")

    runs = []
    print(f"Starting 3-pass benchmark against http://{args.host}:{args.port}/v1/chat/completions...\n")

    for idx in range(1, 4):
        run_type = "Cold Start" if idx == 1 else "Warm Pass"
        print(f"--- Run {idx} ({run_type}) ---")
        
        res = run_benchmark_pass(args.host, args.port, model_id, args.prompt, args.max_tokens)
        runs.append(res)
        
        print(f"  TTFT (Prefill): {res['ttft']:.2f}s | Decode Speed: {res['decode_tps']:.1f} tok/s ({res['tokens']} tokens in {res['decode_duration']:.2f}s)\n")
        time.sleep(2)

    cold_run = runs[0]
    warm_runs = runs[1:]
    
    avg_warm_ttft = sum(r['ttft'] for r in warm_runs) / len(warm_runs)
    avg_warm_decode_tps = sum(r['decode_tps'] for r in warm_runs) / len(warm_runs)

    summary_text = (
        f"==================================================\n"
        f"BENCHMARK SUMMARY RESULTS: {model_id}\n"
        f"==================================================\n"
        f"Cold Start (Run 1) : TTFT {cold_run['ttft']:.2f}s | Decode Speed: {cold_run['decode_tps']:.1f} tok/s\n"
        f"Warm Avg (Runs 2+) : TTFT {avg_warm_ttft:.2f}s | Decode Speed: {avg_warm_decode_tps:.1f} tok/s\n"
        f"=================================================="
    )
    
    print(summary_text)

    # Write output files
    BENCHMARK_RESULTS_PATH.write_text(summary_text)
    
    file_exists = BENCHMARK_LEDGER_PATH.exists()
    with open(BENCHMARK_LEDGER_PATH, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["Timestamp", "Model", "Warm_Decode_TPS", "Warm_TTFT_Sec", "Nodes"])
        writer.writerow([
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            model_id.split("/")[-1],
            f"{avg_warm_decode_tps:.2f}",
            f"{avg_warm_ttft:.2f}",
            args.nodes
        ])

    print(f"\n[+] Results logged to '{BENCHMARK_LEDGER_PATH.name}' and '{BENCHMARK_RESULTS_PATH.name}'")


if __name__ == "__main__":
    main()
