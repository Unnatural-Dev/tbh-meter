"""Anomaly dump — an opt-in black box: the last ~6 min of ticks, written to disk only for a suspicious run.

Why: a run whose kill count exceeds the stage's mob total (or whose measured duration runs well past the
game's own clear time) can only be understood from the tick-by-tick monster snapshots around it, and by
the time anyone notices, they are gone. With `--anomaly-dump` the reader keeps a bounded in-memory ring of
what the loop ALREADY reads each tick; on a run close that trips a rule it writes the ring next to a small
summary. Default OFF; the app never passes the flag.

Pure Python: no memory reads and no meter_windows import. The orchestrator feeds it (`observe_tick`,
`note_closed`, `on_run_close`); this module owns the ring, the trigger rules and the files. Best-effort
by contract — nothing here may raise into the run loop (see docs/invariants/orchestration-purity).
"""

import gzip
import json
import math
import os
import subprocess
import sys
from collections import deque

from shared.utils import diag

RING_SECONDS = 360       # a whole run (~140 s; the +510 one was 292 s) PLUS the boundary before it:
                         # post-fail leftovers vanish 4-10 s after the fail, so a short ring loses the cause
MAX_DUMPS = 20           # per process: a runaway reader must not fill the disk

CLEAR_TIME_SLACK = 5     # seconds the measured duration may run past the game's own clear time


def describe_reader_version() -> str | None:
    """`git describe` of the checkout the reader runs from, or None (a frozen build has no git). Best effort."""
    try:
        kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}   # CREATE_NO_WINDOW
        r = subprocess.run(["git", "describe", "--always", "--dirty"], capture_output=True, text=True,
                           timeout=3, cwd=os.path.dirname(os.path.abspath(__file__)), **kw)
        out = (r.stdout or "").strip()
        return out if r.returncode == 0 and out else None
    except Exception:
        return None


def trigger_reason(outcome, mobs, total_mobs, duration, clear_time):
    """Why this closed run deserves a dump, or None. Only a SUCCESS is judged (only it carries a clear time),
    and an unknown input (None, or a clear time of 0 = unreadable) can never trigger — absent data is not an anomaly."""
    if outcome != "success":
        return None
    if mobs is not None and total_mobs is not None and mobs > total_mobs:
        return "mobs>total_mobs"
    if duration is not None and clear_time and clear_time > 0 and duration - clear_time > CLEAR_TIME_SLACK:
        return "duration-clear_time>5"
    return None


class AnomalyDump:
    def __init__(self, hz: float, out_dir: str, reader_version: str | None = None, max_dumps: int = MAX_DUMPS):
        self._hz = hz
        self._ring: deque = deque(maxlen=max(1, math.ceil(RING_SECONDS * hz)))
        self._out_dir = out_dir
        self._reader_version = reader_version
        self._max_dumps = max_dumps
        self._dumps = 0

    def observe_tick(self, t, snapshot, stage_key, dead_count) -> None:
        """One loop tick, as already read: `snapshot` is [(addr, hp, hp_max), ...], [] or None (invalid read)."""
        try:
            mons = None if snapshot is None else [[a, hp, mx] for a, hp, mx in snapshot]
        except Exception:
            mons = None
        self._ring.append({"t": t, "monsters": mons, "stage_key": stage_key, "dead": dead_count, "closed": []})

    def note_closed(self, class_name: str) -> None:
        """The log-entry class (StageClearLog / StageFailedLog) that closed a run on the tick fed last."""
        if self._ring:
            self._ring[-1]["closed"].append(class_name)

    def ticks(self) -> list:
        return list(self._ring)

    def on_run_close(self, *, run_id, outcome, mobs, total_mobs, duration, clear_time, stage_key):
        """Judge the run that just closed; if it trips a rule write the ring + a summary. Returns the ring
        file path, or None if no complete dump was written. Published archives consume quota even
        when the summary fails. Never raises: an I/O error is logged and ignored."""
        try:
            reason = trigger_reason(outcome, mobs, total_mobs, duration, clear_time)
            if reason is None or self._dumps >= self._max_dumps:
                return None
            anomalies = os.path.join(self._out_dir, "anomalies")
            os.makedirs(anomalies, exist_ok=True)
            ring_path = os.path.join(anomalies, f"{run_id}.ticks.jsonl.gz")
            tmp = ring_path + ".tmp"
            with gzip.open(tmp, "wt", encoding="utf-8") as f:
                for tick in self._ring:
                    f.write(json.dumps(tick) + "\n")
            os.replace(tmp, ring_path)
            # Publication consumes disk quota even if the metadata write fails.
            self._dumps += 1
            meta = {"run_id": run_id, "outcome": outcome, "mobs": mobs, "total_mobs": total_mobs,
                    "duration": duration, "clear_time": clear_time, "stage_key": stage_key, "reason": reason,
                    "reader_version": self._reader_version, "hz": self._hz, "ticks": len(self._ring)}
            with open(os.path.join(anomalies, f"{run_id}.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f)       # written last: its presence marks a complete dump
            return ring_path
        except Exception as e:
            diag(f"[anomaly] dump for run {run_id} failed: {type(e).__name__}: {e}")
            return None
