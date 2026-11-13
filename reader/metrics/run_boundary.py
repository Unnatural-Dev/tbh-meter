"""Run boundary — the per-run kill/damage ledger and the rules that say where a run ends.

The orchestrator (meter_windows.run) READS memory and feeds observations in; this module DECIDES.
It owns, for the run in progress:
  - the ledger: a DpsTracker (kills, damage, dps, peak) fed with the monster snapshot of each tick;
  - the run's start time and the stage it is attributed to (adopted during a short grace window);
  - the reload rule (DeadMonsterUnit count dropping => manual restart of the same stage) and the
    switch rule (live stage key differs from the adopted one => stage switched);
  - close(now): freeze the run into an immutable ClosedRun and start the next run's ledger; after
    a FAILED run, the monsters still listed go to the next ledger as leftovers, not kills.

What stays with the orchestrator (it needs memory): reading the snapshot / stage key / dead count,
and the LOG_LIST scan that closes a run on StageClearLog / StageFailedLog (see
docs/invariants/run-lifecycle). Pure Python — no memory reads, no imports from meter_windows.
"""

from dataclasses import dataclass

from metrics.dps import DpsTracker

ADOPT_GRACE = 3.0        # seconds after a run starts during which the live stage may be (re)adopted
RELOAD_TOLERANCE = 2     # DeadMonsterUnit must drop by MORE than this to count as a reload


@dataclass(frozen=True)
class ClosedRun:
    """What the run that just ended left in the ledger."""
    kills: int
    damage: float
    measured: float           # seconds from the run's start to the close
    stage_key: int | None     # the stage adopted for the run (None if never adopted)


class RunBoundary:
    def __init__(self, now: float):
        self._dead_prev: int | None = None   # survives close(): the game's counter is not per run
        self._reloaded = False
        self._begin(now)

    def _begin(self, now: float, leftovers=()) -> None:
        self._dps = DpsTracker(leftovers=leftovers)
        self._start = now
        self._adopt_until = now + ADOPT_GRACE
        self._stage_key: int | None = None

    # ---- ledger ----------------------------------------------------------------------------
    def observe_monsters(self, snapshot, now: float) -> None:
        """One tick's live-monster snapshot (None = invalid read: state kept, see DpsTracker)."""
        self._dps.update(snapshot, now)

    @property
    def kills(self) -> int:
        return self._dps.total_kills

    @property
    def damage(self) -> float:
        return self._dps.total_damage

    @property
    def peak_dps(self) -> float:
        return self._dps.peak_dps

    @property
    def alive(self) -> int:
        return self._dps.alive

    def dps(self, now: float) -> float:
        return self._dps.dps(now)

    @property
    def start(self) -> float:
        return self._start

    @property
    def stage_key(self) -> int | None:
        return self._stage_key

    def elapsed(self, now: float) -> float:
        return now - self._start

    # ---- stage adoption --------------------------------------------------------------------
    def observe_stage(self, cur_key: int | None, in_catalog: bool, now: float) -> None:
        """Adopt the live stage key while none is adopted yet, or during the initial grace (handles
        auto-replay / advance right after a clear). Keys outside the catalog are never adopted."""
        if cur_key is not None and in_catalog and (
                self._stage_key is None or now < self._adopt_until):
            self._stage_key = cur_key

    # ---- reload / switch -------------------------------------------------------------------
    def observe_dead_count(self, dead_now: int | None) -> bool:
        """DeadMonsterUnit is cumulative and DROPS when the stage reloads on a manual restart
        (clear/auto-replay do NOT zero it). A failed read (None) is neither a reload nor a new
        baseline. Returns, and remembers for should_abandon, whether this tick is a reload."""
        prev = self._dead_prev
        self._reloaded = (prev is not None and dead_now is not None
                          and dead_now < prev - RELOAD_TOLERANCE)
        if dead_now is not None:
            self._dead_prev = dead_now
        return self._reloaded

    def reset_dead_baseline(self) -> None:
        """Forget the previous dead count (re-attach: the old instance's counter is meaningless)."""
        self._dead_prev = None

    def should_abandon(self, cur_key: int | None, in_catalog: bool, now: float) -> bool:
        """END by restart/switch without a clear/fail: the dead count dropped (reload of the same
        stage) OR the live stage changed to another catalogued one. Only once the grace is past and
        a stage is adopted. The caller skips this on a tick where a log already closed the run, and
        closes with the OLD `stage_key` (the stage that was being played)."""
        if now < self._adopt_until or self._stage_key is None:
            return False
        switched = cur_key is not None and in_catalog and cur_key != self._stage_key
        return self._reloaded or switched

    # ---- boundary --------------------------------------------------------------------------
    def close(self, now: float, *, failed: bool = False) -> ClosedRun:
        """Freeze the run in progress and start the next one's ledger at `now`. After a FAILED run
        the monsters still listed are its leftovers: the next ledger does not count them as kills
        if they leave before its first spawn."""
        closed = ClosedRun(kills=self.kills, damage=self.damage,
                           measured=now - self._start, stage_key=self._stage_key)
        self._begin(now, self._dps.listed if failed else ())
        return closed

    def restart(self, now: float) -> None:
        """Discard the run in progress without a summary (re-attach after the game restarted)."""
        self._begin(now)
