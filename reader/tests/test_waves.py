"""game/waves.py — the Brevity-corrected mob total.

Numbers mirror the maintainer's live 1.3.2 account (the three Brevity nodes at level 1 → −3 waves)
and the modal kill count of its `counted` successes: 3-8 Nightmare (2308) wa=23 wm=15 → 301 kills,
3-6 Hell (3306) 27×17 → 409, 3-8 Torment (4308) 31×21 → 589, 1-1 Normal (1101) 10×1 → 8.
"""

import pytest

from config.offsets import StageInfoData
from game import waves
from tests.conftest import MockReader

BREVITY_ALL = [{"key": 1171, "level": 1}, {"key": 1242, "level": 1}, {"key": 1301, "level": 1}]


@pytest.mark.parametrize("wa,wm,horde,kills", [
    (23, 15, 345, 301),     # 3-8 Nightmare
    (27, 17, 459, 409),     # 3-6 Hell
    (31, 21, 651, 589),     # 3-8 Torment
    (10, 1, 10, 8),         # 1-1 Normal
])
def test_total_matches_measured_kills_with_three_brevity_nodes(wa, wm, horde, kills):
    row = (3, 8, horde, 1)
    assert waves.total_mobs(row, (wa, wm), waves.wave_reduction(BREVITY_ALL)) == kills


def test_no_reduction_is_the_historical_total():
    assert waves.total_mobs((3, 8, 345, 1), (23, 15), 0) == 346


def test_missing_wave_data_falls_back_to_catalog_horde():
    """No live wave row (e.g. stage missing from the sweep) → the old horde + 1, never a guess."""
    assert waves.total_mobs((3, 8, 345, 1), None, 3) == 346


def test_act_boss_row_is_just_the_boss():
    assert waves.total_mobs((3, 10, 0, 3), None, 3) == 1


def test_unknown_stage_is_none():
    assert waves.total_mobs(None, (23, 15), 3) is None


def test_reduction_never_goes_below_the_boss():
    assert waves.total_mobs((1, 1, 2, 0), (2, 1), 5) == 1


def test_wave_reduction_counts_only_brevity_nodes_with_levels():
    runes = [{"key": 1171, "level": 1}, {"key": 1242, "level": 0},   # unbought node
             {"key": 101, "level": 5},                                # unrelated rune
             {"key": 1301, "level": None}, {"bad": 1}, "junk"]        # unreadable entries
    assert waves.wave_reduction(runes) == 1


@pytest.mark.parametrize("runes", [None, []])
def test_wave_reduction_unread_or_empty_is_zero(runes):
    assert waves.wave_reduction(runes) == 0


def _stage(mem, a, sk, wa, wm):
    mem.update({a + StageInfoData.STAGE_KEY: sk, a + StageInfoData.WAVE_AMOUNT: wa,
                a + StageInfoData.WAVE_MOB_AMOUNT: wm})


def test_read_stage_waves_gates_rows():
    mem = {}
    _stage(mem, 0x1000, 2308, 23, 15)
    _stage(mem, 0x2000, 4310, 0, 0)            # act boss x-10: no horde → skipped
    _stage(mem, 0x3000, 9999, 999, 15)         # implausible wave count → skipped
    _stage(mem, 0x4000, 2308, 1, 1)            # duplicate key: first row wins
    _stage(mem, 0x5000, None, 23, 15)          # unreadable key → skipped
    out = waves.read_stage_waves(MockReader(mem=mem), [0x1000, 0x2000, 0x3000, 0x4000, 0x5000])
    assert out == {2308: (23, 15)}


def test_read_stage_waves_never_raises():
    class Boom:
        def ri32(self, a):
            raise OSError("ReadProcessMemory failed")
    assert waves.read_stage_waves(Boom(), [0x1000]) == {}
    assert waves.read_stage_waves(MockReader(), None) == {}
