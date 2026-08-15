import os, sys
# Dynamic launcher lookup fix for transformers pickling issue
main_mod = sys.modules.get("__main__")
if main_mod and not hasattr(main_mod, "launcher"):
    setattr(main_mod, "launcher", lambda *args, **kwargs: None)

target = "/usr/local/lib/python3.12/dist-packages/vllm/model_executor/layers/fused_moe/oracle/mxfp4.py"
if os.path.exists(target):
    try:
        with open(target, "r") as f:
            content = f.read()
        old_str = "return Mxfp4MoeBackend.FLASHINFER_CUTLASS_MXFP4_BF16"
        new_str = "return Mxfp4MoeBackend.FLASHINFER_CUTLASS_MXFP4_MXFP8"
        if old_str in content:
            content = content.replace(old_str, new_str)
            with open(target, "w") as f:
                f.write(content)
    except Exception:
        pass
