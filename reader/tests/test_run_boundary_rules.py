"""Hand-built tick sequences for the run-end RULES of metrics/run_boundary.py (seam 2 of
tests/test_run_boundary.py: the recording can't exercise these, its DeadMonsterUnit count is 0).
They pin today's meter_windows.run() behaviour: adoption grace 3.0 s, reload = dead count dropping
by MORE than 2, switch = another catalogued stage key, abandoned closes with the OLD stage key."""

import pytest

from metrics.run_boundary import ADOPT_GRACE, ClosedRun, RunBoundary

A, B = 101, 102


def _mob(addr, hp, hp_max=100.0):
    return (addr, hp, hp_max)


class TestStageAdoption:
    def test_grace_constant_is_three_seconds(self):
        assert ADOPT_GRACE == 3.0

    def test_first_catalogued_key_is_adopted(self):
        rb = RunBoundary(now=0.0)
        assert rb.stage_key is None
        rb.observe_stage(A, True, 10.0)          # long past the grace: still adopted while None
        assert rb.stage_key == A

    def test_missing_key_does_not_clobber_the_adopted_stage_inside_grace(self):
        rb = RunBoundary(now=0.0)
        rb.observe_stage(A, True, 0.1)
        rb.observe_stage(None, True, 0.5)        # failed stage-key read
        assert rb.stage_key == A

    def test_key_outside_catalog_or_none_is_not_adopted(self):
        rb = RunBoundary(now=0.0)
        rb.observe_stage(A, False, 0.1)
        rb.observe_stage(None, True, 0.2)
        assert rb.stage_key is None

    def test_readopts_inside_grace(self):
        rb = RunBoundary(now=0.0)
        rb.observe_stage(A, True, 0.5)
        rb.observe_stage(B, True, 2.9)           # auto-replay/advance right after a clear
        assert rb.stage_key == B

    def test_does_not_readopt_at_or_after_grace(self):
        rb = RunBoundary(now=0.0)
        rb.observe_stage(A, True, 0.5)
        rb.observe_stage(B, True, 3.0)           # `now < adopt_until` is strict
        assert rb.stage_key == A
        rb.observe_stage(B, True, 30.0)
        assert rb.stage_key == A

    def test_grace_is_measured_from_the_run_start(self):
        rb = RunBoundary(now=100.0)
        rb.observe_stage(A, True, 100.0)
        rb.observe_stage(B, True, 102.9)
        assert rb.stage_key == B
        rb.observe_stage(A, True, 103.0)
        assert rb.stage_key == B


class TestReloadRule:
    def test_no_baseline_first_read_is_not_a_reload(self):
        rb = RunBoundary(now=0.0)
        assert rb.observe_dead_count(0) is False

    @pytest.mark.parametrize("prev, now_, reloaded", [
        (50, 50, False),    # unchanged
        (50, 60, False),    # grew
        (50, 48, False),    # drop of exactly 2 is inside the tolerance
        (50, 47, True),     # drop of 3 = reload
        (50, 0, True),
    ])
    def test_tolerance_of_two(self, prev, now_, reloaded):
        rb = RunBoundary(now=0.0)
        rb.observe_dead_count(prev)
        assert rb.observe_dead_count(now_) is reloaded

    def test_failed_read_is_not_a_reload_and_keeps_the_baseline(self):
        rb = RunBoundary(now=0.0)
        rb.observe_dead_count(50)
        assert rb.observe_dead_count(None) is False
        assert rb.observe_dead_count(10) is True      # baseline was still 50

    def test_baseline_follows_every_good_read(self):
        rb = RunBoundary(now=0.0)
        rb.observe_dead_count(50)
        rb.observe_dead_count(48)                     # tolerated, but becomes the new baseline
        assert rb.observe_dead_count(46) is False     # 46 is not < 48 - 2
        assert rb.observe_dead_count(43) is True

    def test_reset_dead_baseline_forgets_the_previous_count(self):
        rb = RunBoundary(now=0.0)
        rb.observe_dead_count(50)
        rb.reset_dead_baseline()
        assert rb.observe_dead_count(0) is False

    def test_baseline_survives_close(self):
        # prev_dead was never reset by new_run(): the game's counter is not per run.
        rb = RunBoundary(now=0.0)
        rb.observe_dead_count(50)
        rb.close(5.0)
        assert rb.observe_dead_count(0) is True


class TestShouldAbandon:
    def _adopted(self):
        rb = RunBoundary(now=0.0)
        rb.observe_stage(A, True, 0.1)
        rb.observe_dead_count(10)
        return rb

    def test_reload_after_grace_abandons(self):
        rb = self._adopted()
        rb.observe_dead_count(0)
        assert rb.should_abandon(A, True, 5.0) is True

    def test_switch_to_another_catalogued_stage_after_grace_abandons(self):
        rb = self._adopted()
        rb.observe_dead_count(10)
        assert rb.should_abandon(B, True, 5.0) is True

    def test_same_stage_no_reload_does_not_abandon(self):
        rb = self._adopted()
        rb.observe_dead_count(11)
        assert rb.should_abandon(A, True, 5.0) is False

    def test_switch_to_unknown_or_missing_key_does_not_abandon(self):
        rb = self._adopted()
        rb.observe_dead_count(10)
        assert rb.should_abandon(B, False, 5.0) is False
        assert rb.should_abandon(None, True, 5.0) is False

    def test_nothing_abandons_inside_the_grace(self):
        rb = self._adopted()
        rb.observe_dead_count(0)
        assert rb.should_abandon(B, True, 2.9) is False

    def test_grace_boundary_is_inclusive_of_adopt_until(self):
        rb = self._adopted()
        rb.observe_dead_count(0)
        assert rb.should_abandon(A, True, 3.0) is True   # `now >= adopt_until`

    def test_fresh_boundary_is_not_a_reload_before_any_dead_read(self):
        rb = RunBoundary(now=0.0)
        rb.observe_stage(A, True, 0.1)
        assert rb.should_abandon(A, True, 5.0) is False

    def test_nothing_to_abandon_without_an_adopted_stage(self):
        rb = RunBoundary(now=0.0)
        rb.observe_dead_count(10)
        rb.observe_dead_count(0)
        assert rb.should_abandon(B, True, 5.0) is False

    def test_abandoned_close_uses_the_old_stage_key(self):
        rb = self._adopted()
        rb.observe_dead_count(10)
        assert rb.should_abandon(B, True, 5.0) is True
        old = rb.stage_key                      # what the orchestrator passes to close_run
        assert old == A
        assert rb.close(5.0).stage_key == A
        assert rb.stage_key is None             # the next run starts un-adopted


class TestLedger:
    def test_close_returns_an_immutable_summary_and_resets_the_ledger(self):
        rb = RunBoundary(now=10.0)
        rb.observe_stage(A, True, 10.0)
        rb.observe_monsters([_mob(1, 100.0), _mob(2, 100.0)], 10.1)
        rb.observe_monsters([_mob(1, 60.0)], 10.2)       # mob 2 dies (100), mob 1 takes 40
        assert rb.kills == 1 and rb.damage == pytest.approx(140.0)
        closed = rb.close(14.5)
        assert closed == ClosedRun(kills=1, damage=140.0, measured=pytest.approx(4.5), stage_key=A)
        with pytest.raises(AttributeError):
            closed.kills = 99
        assert rb.kills == 0 and rb.damage == 0.0 and rb.peak_dps == 0.0 and rb.alive == 0
        assert rb.start == 14.5 and rb.stage_key is None
        assert rb.elapsed(16.0) == pytest.approx(1.5)

    def test_next_run_has_no_cross_run_baseline(self):
        rb = RunBoundary(now=0.0)
        rb.observe_monsters([_mob(1, 100.0)], 0.1)
        rb.close(1.0)
        rb.observe_monsters([_mob(1, 40.0)], 1.1)        # same address, new run: just a first sighting
        assert rb.damage == 0.0 and rb.kills == 0

    def test_next_run_gets_a_fresh_adopt_grace(self):
        rb = RunBoundary(now=0.0)
        rb.observe_stage(A, True, 0.1)
        rb.close(20.0)
        rb.observe_stage(B, True, 21.0)
        rb.observe_stage(A, True, 22.0)                  # still inside the new run's grace
        assert rb.stage_key == A
        rb.observe_stage(B, True, 23.0)                  # 20 + 3.0
        assert rb.stage_key == A

    def test_invalid_snapshot_keeps_state(self):
        rb = RunBoundary(now=0.0)
        rb.observe_monsters([_mob(1, 100.0), _mob(2, 100.0)], 0.1)
        rb.observe_monsters([_mob(1, 80.0), _mob(2, 100.0)], 0.2)
        before = (rb.kills, rb.damage, rb.alive)
        rb.observe_monsters(None, 0.3)
        assert (rb.kills, rb.damage, rb.alive) == before
        rb.observe_monsters([_mob(1, 80.0), _mob(2, 100.0)], 0.4)   # same mobs back: nobody died twice
        assert (rb.kills, rb.damage) == (0, pytest.approx(20.0))

    def test_dps_and_peak_follow_the_tracker(self):
        rb = RunBoundary(now=0.0)
        rb.observe_monsters([_mob(1, 100.0)], 0.0)
        rb.observe_monsters([_mob(1, 50.0)], 1.0)
        assert rb.dps(1.0) > 0
        assert rb.peak_dps >= rb.dps(1.0)
        assert rb.alive == 1

    def test_dps_uses_the_tick_time_and_leaves_the_window(self):
        rb = RunBoundary(now=0.0)
        rb.observe_monsters([_mob(1, 100.0)], 0.0)
        rb.observe_monsters([_mob(1, 50.0)], 1.0)    # 50 damage at t=1.0, 5 s window => 10 dps
        assert rb.dps(1.0) == pytest.approx(10.0)
        assert rb.peak_dps == pytest.approx(10.0)
        assert rb.dps(6.0) == pytest.approx(10.0)    # sample is stamped at the tick time
        assert rb.dps(6.01) == 0.0
        assert rb.peak_dps == pytest.approx(10.0)    # peak survives the window

    def test_restart_discards_the_run_without_touching_the_dead_baseline(self):
        rb = RunBoundary(now=0.0)
        rb.observe_stage(A, True, 0.1)
        rb.observe_monsters([_mob(1, 100.0)], 0.1)
        rb.observe_monsters([], 0.2)
        rb.observe_dead_count(7)
        rb.restart(50.0)
        assert rb.kills == 0 and rb.start == 50.0 and rb.stage_key is None
        assert rb.observe_dead_count(0) is True          # reset_dead_baseline is a separate call
