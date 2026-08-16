import time
import requests
import json

url = "http://10.0.14.43:8000/v1/chat/completions"
headers = {"Content-Type": "application/json"}

payload = {
    "model": "Rarri/DeepSeek-V4-Flash-0731-NVFP4",
    "messages": [
        {"role": "user", "content": "Write a highly detailed, comprehensive architectural breakdown of the Linux kernel, explaining the scheduler and memory management subsystems."}
    ],
    "max_tokens": 1024,
    "stream": True
}

runs = []

print("Starting 3-pass benchmark against spark-4...\n")

for i in range(1, 4):
    print(f"--- Run {i} ({'Cold Start' if i == 1 else 'Warm Pass'}) ---")
    start_time = time.time()
    
    try:
        response = requests.post(url, headers=headers, json=payload, stream=True, timeout=120)
        
        first_token_time = None
        token_count = 0

        for line in response.iter_lines():
            if line:
                decoded_line = line.decode('utf-8')
                if decoded_line == "data: [DONE]":
                    break
                if first_token_time is None:
                    first_token_time = time.time()
                token_count += 1

        end_time = time.time()

        if first_token_time is None:
            print("❌ No tokens received.")
            continue

        ttft = first_token_time - start_time
        decode_time = end_time - first_token_time
        tps = token_count / decode_time if decode_time > 0 else 0

        runs.append({"run": i, "ttft": ttft, "tps": tps, "tokens": token_count})
        print(f"  TTFT:  {ttft:.2f}s | Decode Speed: {tps:.2f} tokens/sec ({token_count} tokens)\n")

    except Exception as e:
        print(f"❌ Run {i} failed: {e}\n")

if runs:
    cold = runs[0]
    warm_runs = runs[1:]
    
    print("=" * 50)
    print("SUMMARY RESULTS")
    print("=" * 50)
    print(f"Cold Start (Run 1): TTFT {cold['ttft']:.2f}s | Speed {cold['tps']:.2f} tok/s")
    
    if warm_runs:
        avg_warm_ttft = sum(r['ttft'] for r in warm_runs) / len(warm_runs)
        avg_warm_tps = sum(r['tps'] for r in warm_runs) / len(warm_runs)
        print(f"Warm Avg (Runs 2-3): TTFT {avg_warm_ttft:.2f}s | Speed {avg_warm_tps:.2f} tok/s")
    print("=" * 50)
