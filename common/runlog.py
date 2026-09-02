"""
Run-log archive: durable, per-load capture of container logs plus the image
provenance needed to explain them later.

WHY THIS EXISTS

Before this module the orchestrator observed every load and remembered
none of it. detect_model_stage() and the READY-time classifier in
_finalize_host_status() both shell out for `docker logs`, read the text,
classify it into one of three buckets, and drop it. The evidence for any
given load survived exactly as long as one run_ssh() call.

That is why model_ledger.json contains values like nemotron-3.5-lightning
-bf16::1_node compiled = 3001s sitting among runs of 571/674/1017, with no
way to establish what happened. It might be a cold JIT compile; it might be
SSH contention, a slow HF mirror, or another job on the node. The number
alone cannot distinguish them, and the logs that could are long gone.

WHAT IT STORES, AND WHAT IT DELIBERATELY DOES NOT

Full logs, gzipped, not a filtered extract. Measured on a real container:
572,480 bytes raw -> 41,088 gzipped, 13.9x. At ~41 KB per load even a
heavy year is tens of megabytes, so there is no reason to be clever.

The temptation is to store only lines matching today's phase keywords.
That would bake today's theory into the archive -- which is precisely the
mistake that made the existing cached/compiled/downloaded buckets useless,
since a future question can only be asked of data captured before anyone
thought to ask it. Store everything, extract at read time.

IMAGE PROVENANCE

A config_hash does not identify what actually ran. Mutable tags move
underneath it: on this cluster `eugr/spark-vllm:latest` resolved to an
image built 2026-08-26 while `eugr/spark-vllm-b12x:latest` resolved to one
built 2026-08-15 -- two different vLLM builds, live on one node, selected
by recipe, with nothing in the ledger recording which was which. A
`docker pull` swaps either silently, with no recipe edit and no hash
change, and every sample before and after lands in the same bucket looking
comparable.

So each record pins the image four ways, verified against real containers
on this cluster:

  image_id           .Image             sha256:078a8109... (pulled)
                                        sha256:9be8538f... (mod-baked)
                     ALWAYS present, for both pulled and locally-baked
                     images. This is the anchor -- key modelling on it.
  image_ref          .Config.Image      "eugr/spark-vllm:latest". The tag
                     as written; human context, not an identity.
  image_repo_digest  RepoDigests[0]     "eugr/spark-vllm@sha256:1342a788..."
                     NULLABLE BY DESIGN. Populated for pulled images,
                     [] for anything ensure_mods_baked() built locally
                     (confirmed on eugr/spark-vllm-b12x:latest-mods-
                     d470c7a26847e57b). A null here is normal, not a
                     capture failure -- do not treat it as one.
  image_created      .Created           Image build time. Cheap proxy for
                     "which upstream build was this".

image_id and image_repo_digest are different digests over different things
(config vs registry manifest) and must never be compared to each other --
only each to itself across runs.

CONTRACT

Best-effort and silent, like every other writer on the status-poll path
(record_load_time(), _record_launch_success(), SessionTracker._commit_
session()). A failed capture must never break a poll, a deploy, or a
teardown. Every entry point returns None on failure rather than raising.

Idempotent via archive-file existence rather than in-memory state:
_finalize_host_status() runs every ~4 seconds and READY persists for the
whole life of a container, so a capture keyed on memory would re-run after
any daemon restart and re-pull the whole log. Disk existence survives
restarts, which is the property that actually matters here.
"""

from __future__ import annotations

import gzip
import json
import re
import time
from pathlib import Path
from typing import Optional

from common.config import BASE_DIR
from common.phase_extract import UnrecognizedLogShape, extract_phases
from common.ssh import run_ssh

RUN_LOGS_DIR = BASE_DIR / "run_logs"
RUN_INDEX_PATH = RUN_LOGS_DIR / "index.json"

# Ceiling on lines pulled per capture. `docker logs` with no bound could in
# principle haul an unbounded stream over SSH into memory; 200k lines is
# ~30x the largest real capture measured here and still bounded. Whether
# the ceiling was hit is recorded in the manifest, so a truncated archive
# is identifiable rather than silently short.
MAX_LOG_LINES = 200_000

ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

# Redaction before anything touches disk. These archives are meant to be
# kept indefinitely and shared while investigating a load, and a token has
# already leaked once through this codebase (--dry-run rendered HF_TOKEN in
# plaintext in an API response). Container logs are a plausible second
# route: anything that dumps its environment on startup would carry one.
# Cheap insurance; the archive loses nothing of analytic value.
_REDACTIONS = (
    (re.compile(r'\bhf_[A-Za-z0-9]{20,}'), 'hf_***REDACTED***'),
    (re.compile(r'\b(gh[pousr]_[A-Za-z0-9]{20,})'), 'gh*_***REDACTED***'),
    # The (?:bearer\s+)? group matters: without it, "authorization: Bearer
    # <secret>" matches \S+ against "Bearer" and redacts the scheme name
    # while leaving the actual credential in the archive. Caught in
    # testing, and it is the exact shape an HTTP auth header takes.
    (re.compile(r'(?i)\b(authorization|api[_-]?key|token|secret|password)'
                r'(\s*[=:]\s*)(?:bearer\s+)?(\S+)'), r'\1\2***REDACTED***'),
)


def _redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


_LINE_TS = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z) ?(.*)$')


def _merge_streams_by_timestamp(stdout_text: str, stderr_text: str) -> str:
    """
    `docker logs --timestamps` prefixes every line with an RFC3339
    timestamp, but stdout and stderr are two SEPARATE streams -- naive
    concatenation (stdout in full, then stderr in full) produces a file
    that looks chronological and isn't. Confirmed on a real capture
    (gemma4-26b-a4b-nvfp4, run 1788360581): line 143 read 14:54:23.940
    (APIServer, end of stdout) and line 144 read 14:49:57.450 (a Python
    warnings.warn from stderr, near the start of the run) -- a 266-second
    jump backwards sitting in the middle of the file with nothing to flag
    it. A reader that assumes file order is chronological order will get
    phase boundaries wrong by however far the two streams diverge, silently.

    Merges by parsed timestamp instead. Lines without a parseable
    timestamp (tqdm progress-bar fragments updated via \\r -- docker only
    timestamps the first fragment of each) are attached to whichever
    timestamped line precedes them, in their original stream and order,
    so no line is dropped and relative order within a stream is
    preserved for the untimestamped runs between timestamped anchors.

    Stable sort, so truly simultaneous lines (same timestamp) keep stdout
    before stderr and original within-stream order -- there is no better
    tiebreak available than that.
    """
    def tag(text: str, stream: int) -> list:
        out = []
        last_ts = None
        for line in text.splitlines():
            m = _LINE_TS.match(line)
            if m:
                last_ts = m.group(1)
            out.append((last_ts, stream, line))
        return out

    combined = tag(stdout_text, 0) + tag(stderr_text, 1)
    # None (no timestamp seen yet on that stream) sorts before everything
    # else by using "" -- these are a handful of banner lines before the
    # first timestamped line and their exact position doesn't matter.
    combined.sort(key=lambda row: (row[0] or "", row[1]))
    return "\n".join(row[2] for row in combined)


def _inspect_image_provenance(ip: str, user: Optional[str], container: str) -> dict:
    """
    Resolve container -> image identity. Two SSH round trips, deliberately:
    the container carries only the image ID, and RepoDigests/Created live on
    the image object, so a single `docker inspect` on the container cannot
    return them. A shell pipeline would collapse this to one call, but
    whether run_ssh() accepts `sh -c` is not something this module should
    assume -- every call site in dgx-orchestrator.py passes a plain argv
    list. Two cheap inspects on a path that runs once per container beats a
    clever one that might not work.

    Never raises. Missing fields come back as None; a null image_repo_digest
    in particular is the expected result for a mod-baked image.
    """
    out = {"image_id": None, "image_ref": None,
           "image_repo_digest": None, "image_created": None}
    try:
        res = run_ssh(ip, user, ["docker", "inspect", container,
                                 "--format", "{{.Image}}|{{.Config.Image}}"], timeout=8)
        if res.returncode != 0 or not res.stdout.strip():
            return out
        image_id, _, image_ref = res.stdout.strip().partition("|")
        out["image_id"] = image_id.strip() or None
        out["image_ref"] = image_ref.strip() or None
        if not out["image_id"]:
            return out

        res2 = run_ssh(ip, user, ["docker", "image", "inspect", out["image_id"],
                                  "--format", "{{json .RepoDigests}}|{{.Created}}"], timeout=8)
        if res2.returncode != 0 or not res2.stdout.strip():
            return out
        digests_raw, _, created = res2.stdout.strip().rpartition("|")
        out["image_created"] = created.strip() or None
        try:
            digests = json.loads(digests_raw)
            if isinstance(digests, list) and digests:
                out["image_repo_digest"] = digests[0]
        except (json.JSONDecodeError, TypeError):
            pass
    except Exception:
        pass
    return out


def _run_id(started_ts: float, host: str, container_id: str) -> str:
    """
    Unique per container start. Anchored on the run, NOT on config_hash --
    a hash is schema-versioned (it orphans on a bump), blind to mutable tags
    and mod payload edits, and deliberately non-unique, since two recipes
    with identical configs are supposed to collide. It is carried as an
    attribute of the run instead, which is the role it can actually fill.
    """
    return f"{int(started_ts)}-{host}-{(container_id or 'unknown')[:12]}"


def _append_index(entry: dict) -> None:
    """
    Append to the flat manifest index. Read-modify-write: this is only
    reached once per container start, so contention is not a concern, and a
    single readable file beats a directory of fragments for the ad-hoc
    querying this exists to support.
    """
    try:
        index = []
        if RUN_INDEX_PATH.exists():
            loaded = json.loads(RUN_INDEX_PATH.read_text())
            if isinstance(loaded, list):
                index = loaded
        index.append(entry)
        tmp = RUN_INDEX_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(index, indent=2))
        tmp.replace(RUN_INDEX_PATH)
    except Exception:
        pass


def archive_run_log(
    host: str,
    ip: str,
    user: Optional[str],
    container: str,
    model_key: str,
    topo_key: str,
    started_ts: float,
    outcome: str,
    config_hash: Optional[str] = None,
    load_type: Optional[str] = None,
    elapsed_sec: Optional[int] = None,
) -> Optional[dict]:
    """
    Capture one container's full logs plus image provenance.

    `outcome` is free-form but should be one of "ready" / "crashed" -- a
    crashed load is frequently the more informative sample, so this is
    deliberately not restricted to successful loads.

    Returns the manifest entry, or None if the capture did not happen (for
    any reason, including "already archived"). Callers must not depend on
    the return value.
    """
    try:
        container_id = ""
        id_res = run_ssh(ip, user, ["docker", "inspect", container,
                                    "--format", "{{.Id}}"], timeout=8)
        if id_res.returncode == 0:
            container_id = id_res.stdout.strip()

        run_id = _run_id(started_ts, host, container_id)
        safe_model = re.sub(r'[^A-Za-z0-9._-]', '_', f"{model_key}::{topo_key}")
        target_dir = RUN_LOGS_DIR / safe_model
        log_path = target_dir / f"{run_id}.log.gz"

        # Idempotency guard -- see module docstring on why this is disk
        # existence rather than in-memory state.
        if log_path.exists():
            return None

        log_res = run_ssh(ip, user, ["docker", "logs", "--timestamps",
                                     "--tail", str(MAX_LOG_LINES), container], timeout=60)
        # stdout+stderr are BOTH real content on success -- a container's own
        # stderr arrives on the stderr channel, which is why every existing
        # reader in dgx-orchestrator.py concatenates them. On failure that
        # same concatenation would archive the SSH/docker error message as
        # though it were the container's log, producing a plausible-looking
        # 61-byte archive containing nothing but the error. Check the
        # returncode first and archive nothing rather than something false.
        if log_res.returncode != 0:
            return None
        raw = _merge_streams_by_timestamp(log_res.stdout or "", log_res.stderr or "")
        if not raw.strip():
            return None

        cleaned = _redact(ANSI_ESCAPE.sub('', raw))
        encoded = cleaned.encode("utf-8", errors="replace")
        line_count = len(cleaned.splitlines())

        target_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = log_path.with_suffix(".gz.tmp")
        with gzip.open(tmp_path, "wb", compresslevel=9) as handle:
            handle.write(encoded)
        tmp_path.replace(log_path)

        entry = {
            "run_id": run_id,
            "captured_ts": time.time(),
            "host": host,
            "model_key": model_key,
            "topo_key": topo_key,
            "container": container,
            "container_id": container_id or None,
            "started_ts": started_ts,
            "elapsed_sec": elapsed_sec,
            "outcome": outcome,
            "load_type": load_type,
            "config_hash": config_hash,
            "log_path": str(log_path.relative_to(BASE_DIR)),
            "log_bytes_raw": len(encoded),
            "log_bytes_gz": log_path.stat().st_size,
            "log_lines": line_count,
            # True means the MAX_LOG_LINES ceiling may have cut the start of
            # the log off, so absence of an early-phase marker in this
            # archive proves nothing.
            "truncated": line_count >= MAX_LOG_LINES,
        }
        entry.update(_inspect_image_provenance(ip, user, container))

        # Phase extraction runs against the SAME merge-sorted, redacted
        # text that gets archived -- one source of truth, not a second
        # decompress-and-reparse pass. Best-effort like everything else
        # here: a parse failure must not cost the archive itself, which is
        # why this is wrapped separately from the archive-write above and
        # degrades to phases=None rather than propagating. Distinguish the
        # two None cases explicitly in the manifest -- "genuinely didn't
        # recognize this log's shape" vs. "some other extractor bug" --
        # since the first is expected on a new stack and the second isn't.
        try:
            entry["phases"] = extract_phases(cleaned).to_ledger_dict()
            entry["phase_extraction_error"] = None
        except UnrecognizedLogShape as exc:
            entry["phases"] = None
            entry["phase_extraction_error"] = f"unrecognized_shape: {exc}"
        except Exception as exc:
            entry["phases"] = None
            entry["phase_extraction_error"] = f"{type(exc).__name__}: {exc}"

        _append_index(entry)
        return entry
    except Exception:
        return None
