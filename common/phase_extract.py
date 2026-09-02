"""
Phase-boundary reconstruction from run-log archives (common/runlog.py).

Replaces the READY-time bucket classifier's "one keyword anywhere in the
log -> one label for the whole run" approach (TOMBSTONES #95, #97) with
actual per-phase durations, extracted from a real timeline.

DESIGN PRINCIPLE: PREFER SELF-REPORTED DURATIONS OVER COMPUTED ONES

vLLM already reports several phase durations directly in its own log
output -- these are AUTHORITATIVE, not inferred:

    [default_loader.py:430] Loading weights took 143.67 seconds
    [monitor.py:53] torch.compile took 0.38 s in total
    [monitor.py:81] Initial profiling/warmup run took 0.24 s
    [core.py:361] init engine (profile, create kv cache, warmup model)
        took 42.33 s (compilation: 0.74 s)

Where a self-reported duration exists, use it. It is immune to SSH/poll
jitter and to exactly which log lines happened to survive a `--tail`
window -- the two failure modes that produced the bad `cached`/
`compiled`/`downloaded` buckets in the first place.

Only ONE phase has no self-report anywhere in any of the sample logs:
everything before "Starting to load model" -- container boot, CUDA init,
Python imports, and on a 2-node topology the Ray/NCCL handshake. This is
DERIVED from a timestamp delta (archive start -> first "Starting to load
model" line) because nothing better is available. It is flagged as
derived in the output, not silently presented as equally solid.

This split matters more than it looks like it should: across all 4 real
archives collected so far, this derived pre-load phase is 58-65% of
total wall time. It is the single largest phase in every sample and the
existing bucket taxonomy has no name for it at all.

VOCABULARY: AT LEAST TWO KNOWN STACKS, TREAT A THIRD AS UNKNOWN

Confirmed from real archives:
  - gemma4-style (eugr/spark-vllm:latest, MTP path): emits
    "torch.compile took" and "Initial profiling/warmup run took", and
    "init engine" carries a "(compilation: Ns)" suffix.
  - dspark-style (hazyumps DSpark image): emits NEITHER. No compile
    duration is self-reported anywhere in these logs. "init engine"
    never carries the compilation suffix.

A parser that assumes one vocabulary and silently returns partial phases
on the other is worse than one that admits it doesn't recognize the
shape. extract_phases() sets `compile_stage_confidence` explicitly per
stack rather than guessing, and a totally unrecognized log (no loader
markers of any kind matched) raises UnrecognizedLogShape rather than
returning an empty/zero-filled result that looks like real data.

NOT YET VALIDATED: COLD COMPILE

All 4 real archives collected so far are warm starts (280-375s total).
None contain a genuine cold JIT compile -- the multi-hundred/thousand-
second values in the existing ledger (2408s, 1689s, 3001s) have no
archived counterpart yet. The self-reported "torch.compile took Ns"
line SHOULD scale correctly regardless of duration (it is vLLM's own
timer, not something this module computes), but that is an assumption,
not something demonstrated against real data. Treat any phase output
with compile_sec in the hundreds+ range as a first real data point for
that assumption, not as confirmation of it.

MULTI-RANK AGGREGATION (2-NODE / TP>1)

On a 2-node deploy, "Loading weights took Ns" and "Starting to load
model" are each emitted ONCE PER RANK -- confirmed in the real dspark
archive: TP0 and TP1 each report their own weight-load duration, at
different wall-clock times, and TP0 additionally reports it TWICE (two
loading passes; unconfirmed why -- possibly a two-stage load or a
retry, not investigated here). Wall-clock progress is gated by whichever
rank is slowest, so per-phase durations aggregate by MAX across ranks,
not sum and not mean. The per-rank breakdown is kept in `by_rank` for
anyone who wants to look at load imbalance directly.
"""

from __future__ import annotations

import gzip
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class UnrecognizedLogShape(Exception):
    """
    Raised when NONE of the known phase markers were found anywhere in
    the log -- not "this stack doesn't report compile duration" (that's
    normal and handled via compile_stage_confidence), but "nothing in
    this file matches any pattern this module knows about at all".

    This is deliberately a hard failure rather than a result with every
    field None. A silently-empty result is indistinguishable from a
    correctly-parsed log of a run that did nothing, and would masquerade
    as data. Callers should catch this, log the run_id, and skip the
    run rather than record a bucket for it -- exactly the trap #95/#97
    exist because of, one layer further down the same problem.
    """


# Rank tag prefixes vLLM/Ray emits ahead of a worker's own log line, e.g.
# "(EngineCore pid=2490) (RayWorkerProc pid=471, ip=10.0.14.41) (Worker_TP1 pid=471) ".
# IMPORTANT: key identity on the RayWorkerProc pid, NOT on Worker_TPn.
# Confirmed on the real dspark archive: a single worker's lines carry
# "RayWorkerProc pid=3575" on every line from that process, but only carry
# "Worker_TP0" on SOME of them (it's added once the worker has determined
# its TP rank, not present on its earliest lines). Keying on whichever tag
# happened to be present on a given line split one physical worker into
# two dict entries (pid3575 and TP0), inflating a 2-rank deploy to a
# reported rank_count of 5. Worker_TPn is kept as a display label only.
_RANK_TAG = re.compile(r'\(Worker_(TP\d+)[^)]*\)')
_RAY_PID_TAG = re.compile(r'\(RayWorkerProc pid=(\d+)')

_LOADER_START = re.compile(
    r'Starting to load model (\S+)'
)
_LOADER_DONE = re.compile(
    r'\[default_loader\.py:\d+\]\s*Loading weights took ([\d.]+) seconds?'
)
_TORCH_COMPILE = re.compile(
    r'\[monitor\.py:\d+\]\s*torch\.compile took ([\d.]+) s'
)
_WARMUP_RUN = re.compile(
    r'\[monitor\.py:\d+\]\s*Initial profiling/warmup run took ([\d.]+) s'
)
_ENGINE_INIT = re.compile(
    r'\[core\.py:\d+\]\s*init engine \([^)]*\) took ([\d.]+) s'
    r'(?:\s*\(compilation:\s*([\d.]+) s\))?'
)
_READY = re.compile(r'Application startup complete')

_LINE_TS = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+)Z ?(.*)$')


def _rank_of(line: str) -> str:
    """Stable rank key. RayWorkerProc pid is present on every line from a
    given worker for the whole run; Worker_TPn is only added once that
    worker has resolved its tensor-parallel rank, so it is NOT present on
    a worker's earliest lines and cannot be the primary key -- see the
    comment above _RANK_TAG for the fragmentation this caused pre-fix."""
    m = _RAY_PID_TAG.search(line)
    if m:
        return f"pid{m.group(1)}"
    if _RANK_TAG.search(line):
        # A Worker_TPn tag with no RayWorkerProc pid alongside it hasn't
        # been observed in real archives, but if it happens, don't merge
        # it into "single" silently -- keep it distinguishable.
        return f"tp_only_{_RANK_TAG.search(line).group(1)}"
    return "single"


def _rank_label(rank_key: str, lines_for_rank: list) -> str:
    """Best display label for a rank_key -- the TPn tag if any line for
    this rank ever carried one, else the raw key."""
    for line in lines_for_rank:
        m = _RANK_TAG.search(line)
        if m:
            return m.group(1)
    return rank_key


@dataclass
class RankTiming:
    label: str = ""                               # best display name (e.g. "TP0"), see _rank_label
    load_started_at: Optional[float] = None      # seconds from archive start
    weight_load_durations: list = field(default_factory=list)  # self-reported, may be >1
    compile_sec: Optional[float] = None
    warmup_sec: Optional[float] = None
    engine_init_sec: Optional[float] = None
    engine_init_compile_sec: Optional[float] = None


@dataclass
class PhaseResult:
    total_sec: float
    pre_load_sec: float                  # DERIVED (timestamp delta), see module docstring
    pre_load_is_derived: bool            # always True today; field exists so a future
                                          # self-reported version of this phase doesn't
                                          # need a schema change, just a flip to False
    weight_load_sec: Optional[float]     # AUTHORITATIVE (self-reported), max across ranks
    compile_sec: Optional[float]         # AUTHORITATIVE where present; None means the
                                          # stack doesn't self-report it, NOT zero compile time
    compile_stage_confidence: str        # "reported" | "absent_known_stack" | "absent_unknown"
    engine_init_sec: Optional[float]     # AUTHORITATIVE
    rank_count: int
    by_rank: dict                        # rank_id -> RankTiming, for load-imbalance inspection
    unaccounted_sec: float               # total - (sum of everything else known) -- should be
                                          # small; large values mean a phase this module
                                          # doesn't yet track is eating real time

    def to_ledger_dict(self) -> dict:
        """
        The subset of this result that belongs in model_ledger.json's
        `runs[]`. Deliberately excludes by_rank: the ledger is read on
        every ~4s status poll (get_estimated_load_time() calls
        _read_json_state() 2-3x per poll on top of that), so it stays a
        summary. Per-rank detail is always recoverable from the archive
        itself via the run's run_id/log_path if anyone needs it.
        """
        return {
            "total_sec": self.total_sec,
            "pre_load_sec": self.pre_load_sec,
            "pre_load_is_derived": self.pre_load_is_derived,
            "weight_load_sec": self.weight_load_sec,
            "compile_sec": self.compile_sec,
            "compile_stage_confidence": self.compile_stage_confidence,
            "engine_init_sec": self.engine_init_sec,
            "rank_count": self.rank_count,
            "unaccounted_sec": self.unaccounted_sec,
        }


def _parse_lines(text: str):
    """Yield (offset_sec, raw_line) for every timestamped line, offset from
    the first timestamp seen. Lines with no timestamp are skipped -- see
    common/runlog.py's merge function for why untimestamped lines exist
    (tqdm \\r-updated progress fragments) and why skipping them here is
    safe: they carry no phase-marker content in any sample seen so far."""
    import datetime
    first = None
    for line in text.splitlines():
        m = _LINE_TS.match(line)
        if not m:
            continue
        ts = datetime.datetime.fromisoformat(m.group(1) + "+00:00")
        if first is None:
            first = ts
        yield (ts - first).total_seconds(), m.group(2)


def extract_phases(log_text: str) -> PhaseResult:
    """
    Parse one archive's full log text (already merge-sorted -- see
    common.runlog._merge_streams_by_timestamp) into a PhaseResult.

    Raises UnrecognizedLogShape if no loader marker is found at all.
    """
    ranks: dict = {}
    rank_lines: dict = {}
    ready_at: Optional[float] = None
    saw_any_marker = False

    for offset, line in _parse_lines(log_text):
        rank = _rank_of(line)
        rt = ranks.setdefault(rank, RankTiming())
        rank_lines.setdefault(rank, []).append(line)

        m = _LOADER_START.search(line)
        if m:
            saw_any_marker = True
            if rt.load_started_at is None:
                rt.load_started_at = offset
            continue

        m = _LOADER_DONE.search(line)
        if m:
            saw_any_marker = True
            rt.weight_load_durations.append(float(m.group(1)))
            continue

        m = _TORCH_COMPILE.search(line)
        if m:
            saw_any_marker = True
            rt.compile_sec = (rt.compile_sec or 0.0) + float(m.group(1))
            continue

        m = _WARMUP_RUN.search(line)
        if m:
            saw_any_marker = True
            rt.warmup_sec = (rt.warmup_sec or 0.0) + float(m.group(1))
            continue

        m = _ENGINE_INIT.search(line)
        if m:
            saw_any_marker = True
            rt.engine_init_sec = float(m.group(1))
            if m.group(2) is not None:
                rt.engine_init_compile_sec = float(m.group(2))
            continue

        if _READY.search(line):
            ready_at = offset

    for rank_key, rt in ranks.items():
        rt.label = _rank_label(rank_key, rank_lines.get(rank_key, []))

    if not saw_any_marker:
        raise UnrecognizedLogShape(
            "no known phase marker (loader start/done, torch.compile, "
            "engine init) found anywhere in this log -- either a stack "
            "this module has not seen, or a log that was truncated "
            "before any of these lines, or genuinely malformed input."
        )

    if ready_at is None:
        # Crashed or still-loading archives won't have this. Fall back to
        # the last timestamped offset seen rather than raising -- a crash
        # archive is exactly the case where SOME phase data (how far it
        # got) is more useful than none.
        offsets = [o for o, _ in _parse_lines(log_text)]
        ready_at = max(offsets) if offsets else 0.0

    load_starts = [rt.load_started_at for rt in ranks.values() if rt.load_started_at is not None]
    pre_load_sec = min(load_starts) if load_starts else ready_at

    def rank_weight_total(rt: RankTiming) -> Optional[float]:
        return sum(rt.weight_load_durations) if rt.weight_load_durations else None

    per_rank_weight = [rank_weight_total(rt) for rt in ranks.values()]
    per_rank_weight = [w for w in per_rank_weight if w is not None]
    weight_load_sec = max(per_rank_weight) if per_rank_weight else None

    per_rank_compile = [rt.compile_sec for rt in ranks.values() if rt.compile_sec is not None]
    per_rank_engine_compile = [rt.engine_init_compile_sec for rt in ranks.values()
                               if rt.engine_init_compile_sec is not None]
    if per_rank_compile or per_rank_engine_compile:
        compile_sec = max(per_rank_compile + per_rank_engine_compile)
        confidence = "reported"
    else:
        # Absent is only meaningful once we've established this is a real,
        # recognized log (saw_any_marker is True by this point) -- so this
        # branch means "this stack doesn't self-report compile duration",
        # not "parsing failed". Still not the same as "compile_sec == 0".
        compile_sec = None
        confidence = "absent_known_stack"

    per_rank_engine_init = [rt.engine_init_sec for rt in ranks.values()
                            if rt.engine_init_sec is not None]
    engine_init_sec = max(per_rank_engine_init) if per_rank_engine_init else None

    known = pre_load_sec
    for v in (weight_load_sec, compile_sec, engine_init_sec):
        if v is not None:
            known += v
    unaccounted = max(0.0, ready_at - known)

    return PhaseResult(
        total_sec=ready_at,
        pre_load_sec=pre_load_sec,
        pre_load_is_derived=True,
        weight_load_sec=weight_load_sec,
        compile_sec=compile_sec,
        compile_stage_confidence=confidence,
        engine_init_sec=engine_init_sec,
        rank_count=len(ranks),
        by_rank=ranks,
        unaccounted_sec=unaccounted,
    )


def extract_phases_from_archive(path: Path) -> PhaseResult:
    """Convenience wrapper: decompress a run_logs/*.log.gz and parse it."""
    with gzip.open(path, "rt") as handle:
        return extract_phases(handle.read())
