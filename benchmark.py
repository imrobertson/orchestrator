#!/usr/bin/env python3
import argparse
import csv
from datetime import datetime
import json
import os
import sys
import time
import requests

from common.config import load_cluster_config

# Ledger & Metadata Configuration
BENCHMARK_VERSION = "1.3.0"
CSV_FILE = "benchmark_ledger.csv"

# Default target - overridable via --host, since a 1-node deployment can be
# pinned to either spark-4 or spark-3 (see dgx-orchestrator.py's --head flag).
# Hardcoding spark-4 here meant benchmarking a spark-3 deployment would either
# fail outright or silently score whatever stale model was still running on
# spark-4, then log it under the wrong name.
DEFAULT_HOST_IP = load_cluster_config().hosts["spark-4"].management_ip


def build_urls(host_ip: str) -> tuple[str, str]:
    return (
        f"http://{host_ip}:8000/v1/chat/completions",
        f"http://{host_ip}:8000/v1/models",
    )


def get_active_model_info(models_url: str):
    """Discover served model ID and engine details from vLLM."""
    try:
        resp = requests.get(models_url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        model_data = data["data"][0]
        model_id = model_data.get("id", "unknown_model")
        engine = model_data.get("owned_by", "vLLM")
        print(f"[+] Discovered active served model ID: '{model_id}' (Engine: {engine})\n")
        return model_id, engine
    except Exception as e:
        print(f"[-] Failed to fetch served model from {models_url}: {e}")
        sys.exit(1)


def run_benchmark_pass(api_url, model_id, prompt, max_tokens=256, temperature=0.0):
    """Executes a streaming request, gracefully handling MTP buffering and batch fallbacks."""
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True}
    }

    start_time = time.perf_counter()
    first_token_time = None
    exact_tokens = 0

    try:
        response = requests.post(api_url, json=payload, stream=True, timeout=120)
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "")

        # Handle Scenario 1: vLLM ignored 'stream=True' and returned a monolithic JSON (Common with MTP)
        if "application/json" in content_type:
            data = response.json()
            end_time = time.perf_counter()
            total_duration = end_time - start_time
            exact_tokens = data.get("usage", {}).get("completion_tokens", max_tokens)
            
            print("    [!] vLLM disabled streaming (MTP fallback). Returning total throughput.")
            return total_duration, (exact_tokens / total_duration), exact_tokens, False

        # Handle Scenario 2: Standard SSE Streaming
        for line in response.iter_lines():
            if not line:
                continue

            line_str = line.decode("utf-8").strip()
            if line_str.startswith("data: "):
                data_str = line_str[6:]
                if data_str == "[DONE]":
                    break

                try:
                    chunk = json.loads(data_str)
                    
                    if "usage" in chunk and chunk["usage"] is not None:
                        exact_tokens = chunk["usage"].get("completion_tokens", exact_tokens)

                    choices = chunk.get("choices", [])
                    if choices and "delta" in choices[0]:
                        delta = choices[0]["delta"]
                        has_content = delta.get("content") or delta.get("reasoning_content")
                        
                        if has_content and first_token_time is None:
                            first_token_time = time.perf_counter()

                except json.JSONDecodeError:
                    continue

        end_time = time.perf_counter()
        total_duration = end_time - start_time

        if first_token_time is None:
            first_token_time = end_time

        ttft = first_token_time - start_time
        decode_duration = max(end_time - first_token_time, 0.0)

        if exact_tokens == 0:
            exact_tokens = max_tokens

        # Handle Scenario 3: Buffered SSE Flush (All chunks arrived in the same millisecond)
        if decode_duration < 0.01:
            print("    [!] Stream was buffered by the engine. Returning total throughput.")
            decode_speed = exact_tokens / total_duration
            is_true_stream = False
        else:
            decode_speed = exact_tokens / decode_duration
            is_true_stream = True

        return ttft, decode_speed, exact_tokens, is_true_stream

    except Exception as e:
        print(f"[-] Request failed: {e}")
        return 0.0, 0.0, 0, False


def append_to_ledger(model_id, engine, params_str, cold_ttft, cold_speed, warm_ttft, warm_speed, tokens_gen):
    """Appends benchmark metrics to CSV ledger, auto-creating header if missing."""
    file_exists = os.path.exists(CSV_FILE) and os.path.getsize(CSV_FILE) > 0

    fieldnames = [
        "timestamp",
        "benchmark_version",
        "model",
        "engine",
        "key_parameters",
        "cold_ttft_sec",
        "cold_decode_tok_s",
        "warm_ttft_sec",
        "warm_decode_tok_s",
        "tokens_generated"
    ]

    row = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "benchmark_version": BENCHMARK_VERSION,
        "model": model_id,
        "engine": engine,
        "key_parameters": params_str,
        "cold_ttft_sec": f"{cold_ttft:.2f}",
        "cold_decode_tok_s": f"{cold_speed:.2f}",
        "warm_ttft_sec": f"{warm_ttft:.2f}",
        "warm_decode_tok_s": f"{warm_speed:.2f}",
        "tokens_generated": tokens_gen
    }

    try:
        with open(CSV_FILE, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
        print(f"[+] Result successfully logged to '{CSV_FILE}'")
    except Exception as e:
        print(f"[-] Failed to write to CSV ledger: {e}")


def parse_args():
    parser = argparse.ArgumentParser(description="Tetrel Security DGX vLLM Benchmark")
    parser.add_argument("--host", default=DEFAULT_HOST_IP,
                         help=f"IP of the node actually serving the OpenAI API (default: {DEFAULT_HOST_IP} / spark-4). "
                              "Pass the real deployment head explicitly for 1-node deploys pinned to spark-3.")
    parser.add_argument("--nodes", type=int, choices=[1, 2], default=2,
                         help="Node count of the deployment under test, recorded in the ledger's key_parameters column.")
    return parser.parse_args()

def main():
    args = parse_args()
    api_url, models_url = build_urls(args.host)

    model_id, engine = get_active_model_info(models_url)
    prompt = "Explain the fundamental architecture of Mixture-of-Experts (MoE) LLMs in detail."
    max_tokens = 256
    temperature = 0.0

    key_parameters = f"max_tokens={max_tokens};temp={temperature};nodes={args.nodes};host={args.host}"

    print(f"Starting 3-pass benchmark against {api_url}...\n")

    results = []
    token_counts = []
    
    for i in range(1, 4):
        label = "Cold Start" if i == 1 else "Warm Pass"
        print(f"--- Run {i} ({label}) ---")
        
        ttft, decode_speed, tokens, is_streaming = run_benchmark_pass(api_url, model_id, prompt, max_tokens, temperature)
        
        if tokens > 0:
            results.append((ttft, decode_speed))
            token_counts.append(tokens)
            if is_streaming:
                print(f"  TTFT: {ttft:.2f}s | Decode Speed: {decode_speed:.2f} tok/s ({tokens} tokens generated)\n")
            else:
                print(f"  Total Duration: {ttft:.2f}s | Throughput: {decode_speed:.2f} tok/s ({tokens} tokens generated)\n")
        else:
            print("  [!] Run failed. Excluding from final average.\n")
            
        time.sleep(1)

    if not results:
        print("[-] All benchmark passes failed. Exiting.")
        return

    cold_ttft, cold_speed = results[0]
    
    warm_runs = results[1:]
    if warm_runs:
        warm_ttft = sum(r[0] for r in warm_runs) / len(warm_runs)
        warm_speed = sum(r[1] for r in warm_runs) / len(warm_runs)
    else:
        warm_ttft, warm_speed = cold_ttft, cold_speed

    avg_tokens = sum(token_counts) // len(token_counts)

    print("=" * 50)
    print("SUMMARY RESULTS")
    print("=" * 50)
    print(f"Cold Start (Run 1) : TTFT/Time {cold_ttft:.2f}s | Speed {cold_speed:.2f} tok/s")
    print(f"Warm Avg (Runs 2+): TTFT/Time {warm_ttft:.2f}s | Speed {warm_speed:.2f} tok/s")
    print("=" * 50)

    append_to_ledger(
        model_id=model_id,
        engine=engine,
        params_str=key_parameters,
        cold_ttft=cold_ttft,
        cold_speed=cold_speed,
        warm_ttft=warm_ttft,
        warm_speed=warm_speed,
        tokens_gen=avg_tokens
    )

if __name__ == "__main__":
    main()
