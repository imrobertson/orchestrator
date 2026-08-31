#!/bin/bash
# mods/_noop/run.sh -- Task MD's proof mod. Touches nothing vLLM-related.
#
# Runs once, inside the throwaway bake container, per constraint 3
# (PHASE-MODS-PROMPTS.md): WORKSPACE_DIR is set by common/mods.py's
# _bake_on_host() to the base image's real Config.WorkingDir
# (/workspace/vllm on eugr/spark-vllm-b12x:latest, per M0/MB), not
# hardcoded here.
#
# Idempotency is not a concern for this file specifically -- unlike
# Task ME's real patch mod, this runs exactly once per bake (MB's
# ensure_mods_baked() skips the bake entirely, run.sh included, if the
# derived tag already exists on the host) -- but the write is still a
# clean overwrite (>) rather than an append, so a re-bake under a
# different tag (e.g. after a deliberate payload edit, per MB's
# check_payload_edit) never leaves a stale line from a prior bake.

set -euo pipefail

if [ -z "${WORKSPACE_DIR:-}" ]; then
    echo "mods/_noop/run.sh: WORKSPACE_DIR is unset -- refusing to guess a path." >&2
    exit 1
fi

marker="$WORKSPACE_DIR/_noop_marker.txt"
echo "Task MD no-op mod applied. WORKSPACE_DIR=$WORKSPACE_DIR" > "$marker"

echo "mods/_noop/run.sh: wrote $marker"
exit 0
