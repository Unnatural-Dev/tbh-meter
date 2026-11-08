"""Regression for validate_live.py [dps]/[stage]: sample a bounded window, not one instant.

WHY: the live MonsterSpawnManager.MONSTER_LIST is validly EMPTY (size 0, header/array readable,
same manager + list pointer) for ~0.9 s between every wave and ~2.7 s after a stage clear —
measured live on 1.3.2 at 10 Hz: 194/1200 samples. A single-instant check fails [dps] and [stage]
TOGETHER on ~16% of runs and "passes on rerun", which proves nothing. The fix samples until a
readable monster (positive hp_max) AND a catalog stage key are seen, within a bounded window.

It must NOT accept zero monsters: an always-empty list, an unreadable list, or a list/manager that
changes mid-window still FAIL. Tests use injected snapshot readers (no game, no sleeping).
"""

import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "validate_live_under_test", os.path.normpath(os.path.join(_HERE, "..", "scripts", "validate_live.py")))
assert _spec is not None and _spec.loader is not None
vl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vl)

STAGES = {4309: (3, 9, 651, 3)}
LIST = 0xAAA0
POP = [(0x1, 100.0, 100.0), (0x2, 50.0, 100.0)]


def _seq(frames):
    """frames: list of (list_ptr, snapshot, stage_key). Returns a sampler + call counter."""
    it = iter(frames)
    calls = []

    def sample():
        calls.append(1)
        return next(it)
    return sample, calls


def _run(frames, budget=10):
    sample, calls = _seq(frames)
    clock = iter(range(1000))
    res = vl.sample_monster_window(sample, STAGES, max_samples=budget, interval=0,
                                   sleep=lambda s: None, now=lambda: float(next(clock)))
    return res, len(calls)


def test_empty_wave_gap_then_populated_passes():
    """RED on the single-sample validator: first sample is the inter-wave gap."""
    res, n = _run([(LIST, [], None), (LIST, [], None), (LIST, POP, 4309)])
    assert res["dps_ok"] and res["stage_ok"]
    assert res["samples"] == 3 and res["empty"] == 2 and n == 3
    assert res["stage_key"] == 4309


def test_stops_at_first_sample_with_both():
    res, n = _run([(LIST, POP, 4309), (LIST, POP, 4309)])
    assert res["dps_ok"] and res["stage_ok"] and n == 1


def test_always_empty_fails_within_bound():
    res, n = _run([(LIST, [], None)] * 10, budget=10)
    assert not res["dps_ok"] and not res["stage_ok"]
    assert n == 10 and res["empty"] == 10


def test_unreadable_snapshot_fails_and_is_reported():
    """None (unreadable header/array) is never treated as an empty wave gap."""
    res, _ = _run([(LIST, None, None), (LIST, POP, 4309)])
    assert not res["dps_ok"] and not res["stage_ok"]
    assert res["invalid"] == 1 and "unreadable" in res["reason"]


def test_list_pointer_change_fails():
    res, _ = _run([(LIST, [], None), (LIST + 8, POP, 4309)])
    assert not res["dps_ok"] and not res["stage_ok"]
    assert "list pointer changed" in res["reason"]


def test_null_list_pointer_fails():
    res, _ = _run([(0, [], None), (0, [], None)], budget=2)
    assert not res["dps_ok"] and not res["stage_ok"] and "null" in res["reason"]


def test_null_after_dps_proven_still_fails_both():
    res, n = _run([(LIST, POP, None), (0, [], None), (LIST, POP, 4309)])
    assert not res["dps_ok"] and not res["stage_ok"] and n == 2


def test_pointer_change_after_dps_proven_still_fails_both():
    res, n = _run([(LIST, POP, None), (LIST + 8, POP, 4309)])
    assert not res["dps_ok"] and not res["stage_ok"] and n == 2
    assert "list pointer changed" in res["reason"]


def test_out_of_catalog_key_is_reported():
    """The garbage-key diagnostic (1.00.17) must still show WHICH key was read."""
    res, _ = _run([(LIST, POP, 999999)] * 3, budget=3)
    assert not res["stage_ok"] and res["stage_key"] == 999999


def test_monsters_without_max_hp_are_still_counted_in_the_detail():
    """A shifted HP_MAX looks like 'monsters=3 withHpMax=0'; that must stay visible."""
    res, _ = _run([(LIST, [(0x1, 10.0, 0.0)] * 3, 4309)] * 3, budget=3)
    assert not res["dps_ok"] and res["monsters"] == 3 and res["with_hp_max"] == 0


def test_stage_outside_catalog_fails_stage_only():
    res, _ = _run([(LIST, POP, 999999)] * 3, budget=3)
    assert res["dps_ok"] and not res["stage_ok"]


def test_monsters_without_positive_max_hp_do_not_pass_dps():
    res, _ = _run([(LIST, [(0x1, 10.0, 0.0)], 4309)] * 3, budget=3)
    assert not res["dps_ok"]


def test_dps_and_stage_may_come_from_different_valid_samples():
    res, _ = _run([(LIST, POP, None), (LIST, [], None), (LIST, POP, 4309)])
    assert res["dps_ok"] and res["stage_ok"]


def test_unreadable_after_dps_proven_still_fails_both():
    """A read failure mid-window is never excused by an earlier good sample."""
    res, n = _run([(LIST, POP, None), (None, None, None), (LIST, POP, 4309)])
    assert not res["dps_ok"] and not res["stage_ok"] and res["invalid"] == 1 and n == 2


def test_default_window_is_bounded_and_covers_measured_gaps():
    """Measured: ~0.9 s ordinary gaps, 2.9 s between populated samples after one stage clear (10 Hz).
    6 s is a PROVISIONAL bound (one transition measured); the window failing still fails the gate."""
    span = vl.MONSTER_WINDOW_SAMPLES * vl.MONSTER_WINDOW_INTERVAL
    assert 2.9 < span <= 10.0
    assert vl.MONSTER_WINDOW_INTERVAL <= 0.1
