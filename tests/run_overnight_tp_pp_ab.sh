#!/usr/bin/env bash
#
# run_overnight_tp_pp_ab.sh -- unattended overnight A/B runner.
#
# Originally hardcoded to two TP-vs-PP pairs; generalized 2026-09-03 to
# take any list of ab_test.py variant pairs, so it also covers things
# like the qwen-3.5-122b MTP token-depth sweep without a second script.
# Filename kept as-is despite the broadened scope -- TP vs PP remains the
# primary use case, and it's already referenced under this name in
# ROADMAP.md/TOMBSTONES.md. Rename later if that stops being true.
#
# Runs ab_test.py for each pair, one at a time (there's only one 2-node
# pair of physical hosts, so 2-node pairs cannot run concurrently
# regardless -- see the per-pair `nodes` field below). Each pair's full
# output goes to its own timestamped log; a failure in one pair does NOT
# abort the run -- the next pair still gets its turn, and the final
# summary says which pairs passed/failed/never ran, so nothing silently
# vanishes overnight.
#
# PAIRS SOURCE (pick one):
#   1. Default: reads pairs from a plain-text file, one pair per line,
#      via --pairs-file (default: pairs.txt in the current directory).
#      Blank lines and lines starting with # are ignored.
#   2. Positional args: pass pairs directly on the command line instead
#      of using a file -- useful for a quick one-off without editing a
#      file first. If any positional args are given, the pairs file is
#      ignored entirely (args win, not merged).
#
# PAIR FORMAT (one per line/arg):
#   label|variant_a|variant_b[|nodes]
#
#   `nodes` is optional, defaults to 2 (this script's original and still
#   primary use case). Pass 1 explicitly for a 1-node comparison --
#   passed through as --a-nodes/--b-nodes on both sides equally; there's
#   no support here for an asymmetric a-nodes != b-nodes pair, since
#   nothing in this repo's catalog needs that shape yet.
#
# Example pairs.txt:
#   # TP vs PP
#   qwen-2.5-coder-32b|qwen-2_5-coder-32b|qwen-2_5-coder-32b-tp|2
#   # MTP token-depth sweep (all TP, same nodes value throughout)
#   qwen-3.5-122b-mtp2-vs-mtp3|qwen-3.5-122b-mtp2|qwen-3.5-122b-tp|2
#   qwen-3.5-122b-mtp3-vs-mtp4|qwen-3.5-122b-tp|qwen-3.5-122b-mtp4|2
#
# Usage (on maestro, inside a tmux/screen session so a dropped SSH
# connection doesn't kill it -- matches this repo's own convention for
# unattended runs, see the sibling project's master_orchestrator.sh):
#
#   tmux new -s overnight_ab
#   ./run_overnight_tp_pp_ab.sh                       # reads pairs.txt
#   ./run_overnight_tp_pp_ab.sh --pairs-file my.txt    # explicit file
#   ./run_overnight_tp_pp_ab.sh "label|a|b" "label2|a2|b2|1"  # ad-hoc
#   # Ctrl+B, D to detach; tmux attach -t overnight_ab to check back in
#
# Does NOT check whether the cluster is currently busy with something
# else (e.g. an interactive llama test run) before starting -- that's a
# deliberate choice, not an oversight: this repo has no lock/queue
# mechanism to check against, and guessing wrong would be worse than
# just not guessing. Confirm the cluster is actually free before
# launching this.

set -uo pipefail  # deliberately NOT -e -- one pair's failure must not
                   # abort the whole run; see the per-pair handling below.

PAIRS_FILE="pairs.txt"
REPEATS=3
PROMPTS="all"
SLEEP_BETWEEN_PAIRS=60  # seconds -- headroom for full teardown/GPU release
                         # before the next pair's deploy. 60s is a guess,
                         # not a measurement; the nemotron-nano-right-after
                         # -minimax-crash case one session needed only
                         # ~15s, so this is deliberately generous, not
                         # tuned. Revisit if a pair ever fails at deploy
                         # time in a way that looks like leftover state
                         # from the previous pair.

POSITIONAL_PAIRS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --pairs-file) PAIRS_FILE="$2"; shift 2 ;;
        --repeats) REPEATS="$2"; shift 2 ;;
        --prompts) PROMPTS="$2"; shift 2 ;;
        --sleep) SLEEP_BETWEEN_PAIRS="$2"; shift 2 ;;
        *) POSITIONAL_PAIRS+=("$1"); shift ;;
    esac
done

# Positional args win outright over the pairs file if any were given --
# not merged, so there's no ambiguity about which source is in effect.
PAIRS=()
if [ "${#POSITIONAL_PAIRS[@]}" -gt 0 ]; then
    PAIRS=("${POSITIONAL_PAIRS[@]}")
    echo "Using ${#PAIRS[@]} pair(s) from command-line args (ignoring $PAIRS_FILE)."
else
    if [ ! -f "$PAIRS_FILE" ]; then
        echo "[!] No pairs given and $PAIRS_FILE doesn't exist. Nothing to run." >&2
        echo "    Either create $PAIRS_FILE (one 'label|variant_a|variant_b[|nodes]' per line)" >&2
        echo "    or pass pairs directly as arguments. See this script's header comment." >&2
        exit 1
    fi
    while IFS= read -r line; do
        line="${line%%$'\r'}"                      # strip stray CR (CRLF-edited file)
        [[ -z "$line" || "$line" == \#* ]] && continue
        PAIRS+=("$line")
    done < "$PAIRS_FILE"
    echo "Using ${#PAIRS[@]} pair(s) from $PAIRS_FILE."
fi

if [ "${#PAIRS[@]}" -eq 0 ]; then
    echo "[!] Pairs source resolved to zero usable lines. Nothing to run." >&2
    exit 1
fi

LOGDIR="ab_logs/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$LOGDIR"

declare -a RESULT_LABELS
declare -a RESULT_STATUS

echo "=== Overnight A/B run starting: $(date) ==="
echo "Logs: $LOGDIR/  repeats=$REPEATS  prompts=$PROMPTS  sleep_between=${SLEEP_BETWEEN_PAIRS}s"
echo ""

for pair in "${PAIRS[@]}"; do
    IFS='|' read -r label variant_a variant_b nodes <<< "$pair"
    nodes="${nodes:-2}"  # default 2 -- this script's original, still primary use case
    logfile="$LOGDIR/${label}.log"

    echo "=== [$label] starting: $(date) ==="
    echo "    a=$variant_a  b=$variant_b  nodes=$nodes  repeats=$REPEATS  prompts=$PROMPTS"
    echo "    log: $logfile"

    docker exec dgx-orchestrator-api python3 tests/ab_test.py \
        --variant-a "$variant_a" \
        --variant-b "$variant_b" \
        --a-nodes "$nodes" --b-nodes "$nodes" \
        --prompts "$PROMPTS" \
        --repeats "$REPEATS" \
        > "$logfile" 2>&1
    exit_code=$?

    RESULT_LABELS+=("$label")
    if [ "$exit_code" -eq 0 ]; then
        RESULT_STATUS+=("PASS")
        echo "=== [$label] PASS (exit 0): $(date) ==="
    else
        RESULT_STATUS+=("FAIL (exit $exit_code)")
        echo "=== [$label] FAIL (exit $exit_code) -- see $logfile: $(date) ==="
        echo "    Continuing to next pair regardless."
    fi
    echo ""

    sleep "$SLEEP_BETWEEN_PAIRS"
done

echo ""
echo "=== Overnight run complete: $(date) ==="
echo "=== Summary ==="
for i in "${!RESULT_LABELS[@]}"; do
    printf "  %-30s %s\n" "${RESULT_LABELS[$i]}" "${RESULT_STATUS[$i]}"
done
echo ""
echo "Full logs in $LOGDIR/ -- each pair's ab_test.py output includes the"
echo "AGGREGATE ACROSS N REPEATS and 'A vs B' sections with real tok/s"
echo "deltas, not just pass/fail. Check those before trusting a PASS as"
echo "'TP won' or 'PP won' -- a PASS only means every health/boot check"
echo "succeeded on both sides, not that the comparison favored either one."
