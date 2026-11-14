"""Run boundary (metrics/run_boundary.py): the per-run kill/damage ledger and the run-end rules.

Two seams:
  1. RECORDED replay: four live fail boundaries (game 1.3.2, 10 Hz, read-only capture) through
     the module, asserting the exact per-run kills/damage. The failed run's leftovers that leave
     before the next run's first spawn are not the next run's kills (#28); everything else matches
     the pre-refactor meter.
  2. HAND-BUILT tick sequences for the abandoned / reload / stage-adoption rules, which the
     recording can't exercise (its DeadMonsterUnit count is always 0).

Fixture: tests/fixtures/fail_boundary_ticks.jsonl.gz. One header row per window
{"window": n, "fail_at": s}, then {"t", "st", "u": [[id, hp, hp_max], ...]} rows; times are
seconds from the window start, ids are renumbered memory addresses (no real addresses kept).
"""

import gzip
import json
import pathlib

import pytest

from metrics.run_boundary import RunBoundary

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "fail_boundary_ticks.jsonl.gz"


def _windows():
    rows = [json.loads(line) for line in gzip.decompress(FIXTURE.read_bytes()).decode().splitlines()]
    out = []
    for row in rows:
        if "window" in row:
            out.append({"fail_at": row["fail_at"], "ticks": []})
        else:
            out[-1]["ticks"].append(row)
    return out


def _replay(window):
    """Drive the module the way the reader loop does: observe the tick, then process the close."""
    rb = RunBoundary(now=0.0)
    closed = None
    for tick in window["ticks"]:
        rb.observe_monsters([tuple(u) for u in tick["u"]], tick["t"])
        if closed is None and tick["t"] >= window["fail_at"]:
            closed = rb.close(tick["t"], failed=True)
    return closed, rb


class TestRecordedFailBoundaries:
    # Kills: (before the fail, after the fail), then damage after the fail. Kills before the fail and
    # damage match the pre-refactor production order (DpsTracker.update, then the close on the tick the
    # StageFailedLog is seen, then a fresh tracker). Kills after the fail come from an independent
    # replay of the leftover rule over the same capture (#28); the overcounting meter had 196, 206,
    # 199 and 209.
    EXPECTED = [
        (11, 196, 758_203_634.3),
        (20, 205, 809_649_800.0),
        (3, 176, 766_050_819.4),
        (13, 203, 826_890_740.8),
    ]

    @pytest.mark.parametrize("i", range(4))
    def test_kills_and_damage_on_each_side_of_the_fail(self, i):
        closed, rb = _replay(_windows()[i])
        kills_before, kills_after, damage_after = self.EXPECTED[i]
        assert closed.kills == kills_before
        assert rb.kills == kills_after
        assert rb.damage == pytest.approx(damage_after, abs=0.1)

    def test_fixture_has_four_fail_windows(self):
        assert len(_windows()) == 4
