#!/usr/bin/env python3
"""
TETREL SECURITY - vLLM STREAMING CHAT BENCHMARK
--------------------------------------------------------------------------------
Auto-detects the active served model ID from /v1/models and measures TTFT 
and streaming decode speed (tok/s) via /v1/chat/completions.
"""

import time
import json
import requests

BASE_URL = "http://10.0.14.43:8000"

def get_active_model_name():
    """Queries /v1/models to fetch the exact served model string from vLLM."""
    try:
        res = requests.get(f"{BASE_URL}/v1/models", timeout=5)
        res.raise_for_status()
        data = res.json()
        models = data.get("data", [])
        if models:
            model_id = models[0].get("id")
            print(f"[+] Discovered active served model ID: '{model_id}'")
            return model_id
    except Exception as e:
        print(f"[!] Failed to fetch /v1/models: {e}")
    return None

def run_benchmark_pass(model_name: str, prompt: str, max_tokens: int = 256):
    url = f"{BASE_URL}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model_name,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True}
    }

    start_time = time.perf_counter()
    first_token_time = None
    token_count = 0

    try:
        response = requests.post(url, headers=headers, json=payload, stream=True, timeout=120)
        response.raise_for_status()

        for line in response.iter_lines():
            if line:
                line_str = line.decode('utf-8')
                if line_str.startswith("data: "):
                    data_str = line_str[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                        choices = data.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            content = delta.get("content", "") or delta.get("reasoning_content", "")
                            if content and first_token_time is None:
                                first_token_time = time.perf_counter()
                        
                        if "usage" in data and data["usage"]:
                            token_count = data["usage"].get("completion_tokens", token_count)
                        elif choices and (choices[0].get("delta", {}).get("content") or choices[0].get("delta", {}).get("reasoning_content")):
                            token_count += 1
                    except json.JSONDecodeError:
                        pass
        
        end_time = time.perf_counter()

        if first_token_time is None:
            first_token_time = end_time

        ttft = first_token_time - start_time
        gen_time = end_time - first_token_time
        
        if gen_time <= 0:
            gen_time = 0.001

        if token_count == 0:
            token_count = max_tokens

        decode_speed = token_count / gen_time

        return {
            "ttft": ttft,
            "token_count": token_count,
            "gen_time": gen_time,
            "decode_speed": decode_speed
        }
    except Exception as e:
        print(f"[-] Benchmark request failed: {e}")
        return None

def main():
    model_name = get_active_model_name()
    if not model_name:
        print("[-] Could not detect an active vLLM model. Ensure the engine is running and ready.")
        return

    prompt = "Explain the architecture of distributed systems, multi-node inference scaling, and consensus algorithms in detail."
    print(f"\nStarting 3-pass streaming benchmark against {BASE_URL}/v1/chat/completions...\n")

    print("--- Run 1 (Cold Start) ---")
    r1 = run_benchmark_pass(model_name, prompt, max_tokens=256)
    if r1:
        print(f"  TTFT: {r1['ttft']:.2f}s | Decode Speed: {r1['decode_speed']:.2f} tok/s ({r1['token_count']} tokens generated)")

    print("\n--- Run 2 (Warm Pass) ---")
    r2 = run_benchmark_pass(model_name, prompt, max_tokens=256)
    if r2:
        print(f"  TTFT: {r2['ttft']:.2f}s | Decode Speed: {r2['decode_speed']:.2f} tok/s ({r2['token_count']} tokens generated)")

    print("\n--- Run 3 (Warm Pass) ---")
    r3 = run_benchmark_pass(model_name, prompt, max_tokens=256)
    if r3:
        print(f"  TTFT: {r3['ttft']:.2f}s | Decode Speed: {r3['decode_speed']:.2f} tok/s ({r3['token_count']} tokens generated)")

    if r2 and r3:
        warm_avg_speed = (r2['decode_speed'] + r3['decode_speed']) / 2
        warm_avg_ttft = (r2['ttft'] + r3['ttft']) / 2
        print("\n" + "="*50)
        print("SUMMARY RESULTS")
        print("="*50)
        if r1:
            print(f"Cold Start (Run 1): TTFT {r1['ttft']:.2f}s | Speed {r1['decode_speed']:.2f} tok/s")
        print(f"Warm Avg (Runs 2-3): TTFT {warm_avg_ttft:.2f}s | Speed {warm_avg_speed:.2f} tok/s")
        print("="*50)

if __name__ == "__main__":
    main()
