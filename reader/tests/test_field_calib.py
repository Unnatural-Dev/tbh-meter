"""Guard tests for game/field_calib.py — the auto-calibration of the four fragile field offsets.

Each discovery is tested against a byte-level fake memory where the target field sits at a NON-default
offset, proving the signature finds it wherever it moved. test_calibrate_keeps_static_on_no_signal
proves the critical safety property: no signal -> static offsets are left untouched (never makes a
working build worse). See docs/invariants/field-offset-autocalibration.
"""
import struct

import pytest

from config.offsets import (Array, Unit, Monster, UnitHealthController, StageManager,
                            MonsterSpawnManager, HeroRuntime, HeroInfoData)
from game import field_calib


class ByteReader:
    """Byte-addressable fake memory. Backs ri32/rptr/read/read_struct/read_array_ptrs/list_ptrs —
    the exact subset field_calib uses. Writers place a value at an absolute address."""

    def __init__(self):
        self.mem = bytearray(0x400000)  # 4 MB flat space; addresses are offsets into it

    def _w(self, addr, data):
        self.mem[addr:addr + len(data)] = data

    def wi32(self, addr, v):
        self._w(addr, struct.pack("<i", v))

    def wptr(self, addr, v):
        self._w(addr, struct.pack("<Q", v))

    def wf32(self, addr, v):
        self._w(addr, struct.pack("<f", v))

    # --- Reader interface used by field_calib ---
    def read(self, addr, size):
        if addr < 0 or addr + size > len(self.mem):
            return None
        return bytes(self.mem[addr:addr + size])

    def ri32(self, addr):
        b = self.read(addr, 4)
        return struct.unpack("<i", b)[0] if b else None

    def rptr(self, addr):
        b = self.read(addr, 8)
        return struct.unpack("<Q", b)[0] if b else None

    def read_struct(self, addr, fmt):
        b = self.read(addr, struct.calcsize(fmt))
        return struct.unpack(fmt, b) if b else None

    def read_array_ptrs(self, arr, count):
        b = self.read(arr + Array.DATA, count * 8)
        if not b:
            return []
        return list(struct.unpack("<%dQ" % count, b))

    def list_ptrs(self, list_obj, cap=600):
        if not list_obj:
            return []
        from config.offsets import List
        size = self.ri32(list_obj + List.SIZE)
        items = self.rptr(list_obj + List.ITEMS)
        if not size or not items or size < 0 or size > cap:
            return []
        return [p for p in self.read_array_ptrs(items, size) if p]


@pytest.fixture(autouse=True)
def restore_offsets():
    """Snapshot/restore the mutable offset class attributes so one test's calibration can't leak."""
    saved = (Monster.STAGE_KEY, Unit.CACHE, UnitHealthController.HP_MAX, StageManager.HERO_LIST)
    yield
    (Monster.STAGE_KEY, Unit.CACHE, UnitHealthController.HP_MAX, StageManager.HERO_LIST) = saved


STAGE_INFO = {3301: (3, 1, 459, 2), 1107: (1, 7, 72, 0)}
HERO_CAT = {101: 1, 201: 2, 301: 3, 401: 4}


def _msm_with_monsters(r, monster_addrs, msm=0x1000, mlist=0x2000, items=0x3000):
    """Wire an MSM whose MONSTER_LIST holds the given monster addresses."""
    from config.offsets import List
    r.wptr(msm + MonsterSpawnManager.MONSTER_LIST, mlist)
    r.wi32(mlist + List.SIZE, len(monster_addrs))
    r.wptr(mlist + List.ITEMS, items)
    for i, a in enumerate(monster_addrs):
        r.wptr(items + Array.DATA + i * 8, a)
    return msm


def test_discover_stage_key_by_catalog():
    """STAGE_KEY found at a non-default offset because its value is a catalog key."""
    r = ByteReader()
    moved = 0x444  # deliberately not the current Monster.STAGE_KEY
    m0, m1 = 0x10000, 0x20000
    for m in (m0, m1):
        r.wi32(m + moved, 3301)  # a real stage_info key
    msm = _msm_with_monsters(r, [m0, m1])
    assert field_calib.discover_stage_key(r, msm, STAGE_INFO) == moved


def test_discover_hp_max_ge_current():
    """HP_MAX found: the float >= HP_CURRENT(0x40) and > 0, at a moved offset."""
    r = ByteReader()
    moved = 0x58
    m0 = 0x10000
    hc = 0x50000
    r.wptr(m0 + Unit.HEALTH_CONTROLLER, hc)
    r.wf32(hc + UnitHealthController.HP_CURRENT, 500.0)
    r.wf32(hc + moved, 900.0)  # the real max
    msm = _msm_with_monsters(r, [m0])
    assert field_calib.discover_hp_max(r, msm) == moved


def test_discover_hero_list_and_cache():
    """HERO_LIST + CACHE found together: array of hero ptrs whose INFO->HERO_KEY is in hero_cat."""
    r = ByteReader()
    sm = 0x1000
    hl_off, cache_off = 0x58, 0x3a0  # both moved from defaults
    hl = 0x60000
    heroes = [0x70000, 0x71000, 0x72000]
    keys = [401, 301, 201]  # >= 2 distinct, all in HERO_CAT
    r.wptr(sm + hl_off, hl)
    r.wi32(hl + Array.MAX_LENGTH, len(heroes))
    for i, (ha, hk) in enumerate(zip(heroes, keys, strict=True)):
        r.wptr(hl + Array.DATA + i * 8, ha)
        uf = ha + 0x8000
        info = uf + 0x4000
        r.wptr(ha + cache_off, uf)
        r.wptr(uf + HeroRuntime.INFO, info)
        r.wi32(info + HeroInfoData.HERO_KEY, hk)
    found_hl, found_cache = field_calib.discover_hero_list_and_cache(r, [sm], HERO_CAT)
    assert (found_hl, found_cache) == (hl_off, cache_off)


def test_calibrate_rejects_same_key_repeated():
    """The [2,2,2] false positive: an array where every element maps to the SAME key is rejected."""
    r = ByteReader()
    sm = 0x1000
    hl = 0x60000
    heroes = [0x70000, 0x71000, 0x72000]
    r.wptr(sm + 0x58, hl)
    r.wi32(hl + Array.MAX_LENGTH, len(heroes))
    for i, ha in enumerate(heroes):
        r.wptr(hl + Array.DATA + i * 8, ha)
        uf = ha + 0x8000
        info = uf + 0x4000
        r.wptr(ha + 0x3a0, uf)
        r.wptr(uf + HeroRuntime.INFO, info)
        r.wi32(info + HeroInfoData.HERO_KEY, 2)  # same key repeated -> not a real party
    found_hl, found_cache = field_calib.discover_hero_list_and_cache(r, [sm], HERO_CAT)
    assert (found_hl, found_cache) == (None, None)


def test_calibrate_keeps_static_on_no_signal():
    """SAFETY: with no live signal (empty MSM / no sm list), calibrate leaves every static offset."""
    r = ByteReader()
    before = (Monster.STAGE_KEY, Unit.CACHE, UnitHealthController.HP_MAX, StageManager.HERO_LIST)
    report = field_calib.calibrate(r, msm=0, sm_list=[], stage_info=STAGE_INFO, hero_cat=HERO_CAT)
    after = (Monster.STAGE_KEY, Unit.CACHE, UnitHealthController.HP_MAX, StageManager.HERO_LIST)
    assert before == after
    assert all(v[2] == "fallback" for v in report.values())


# --- hardening: a coincidental match must never override a working static ----------------------


def _monsters_with(r, n, base=0x10000, step=0x10000):
    return [base + i * step for i in range(n)]


def test_stage_key_static_wins_over_coincidental_match():
    """Both the static and a lower slot carry the catalog key on every mob: the static is kept (the
    old max-tally picked the LOWEST offset)."""
    r = ByteReader()
    mons = _monsters_with(r, 4)
    for m in mons:
        r.wi32(m + Monster.STAGE_KEY, 3301)
        r.wi32(m + 0x3C0, 3301)
    msm = _msm_with_monsters(r, mons)
    assert field_calib.discover_stage_key(r, msm, STAGE_INFO) == Monster.STAGE_KEY


def test_stage_key_tie_between_non_static_slots_keeps_static():
    """Two non-static slots qualify equally and the static reads nothing: ambiguous -> None."""
    r = ByteReader()
    mons = _monsters_with(r, 4)
    for m in mons:
        r.wi32(m + 0x3C0, 3301)
        r.wi32(m + 0x444, 3301)
    msm = _msm_with_monsters(r, mons)
    assert field_calib.discover_stage_key(r, msm, STAGE_INFO) is None


def test_stage_key_rejects_slot_with_varying_catalog_values():
    """A slot whose catalog-valued reads DIFFER across mobs is not the stage key (every mob of a stage
    has the same one); the consistent slot wins."""
    r = ByteReader()
    mons = _monsters_with(r, 4)
    for i, m in enumerate(mons):
        r.wi32(m + 0x3C0, (3301, 1107)[i % 2])  # catalog values, but not one key
        r.wi32(m + 0x444, 3301)
    msm = _msm_with_monsters(r, mons)
    assert field_calib.discover_stage_key(r, msm, STAGE_INFO) == 0x444


def test_stage_key_tolerates_one_odd_catalog_value():
    """The real slot still wins when one mob reads a different catalog key (3 of 4 agree)."""
    r = ByteReader()
    mons = _monsters_with(r, 4)
    for i, m in enumerate(mons):
        r.wi32(m + 0x444, 1107 if i == 0 else 3301)
    msm = _msm_with_monsters(r, mons)
    assert field_calib.discover_stage_key(r, msm, STAGE_INFO) == 0x444


def test_stage_key_needs_majority_of_sampled_monsters():
    r = ByteReader()
    mons = _monsters_with(r, 4)
    r.wi32(mons[0] + 0x444, 3301)  # 1 of 4
    msm = _msm_with_monsters(r, mons)
    assert field_calib.discover_stage_key(r, msm, STAGE_INFO) is None


def test_stage_key_scan_never_reads_obscured_cache():
    """The stage-key window skips Monster.CACHE_OBSCURED (8-byte pointer slot): a catalog value planted
    there is never seen, and no 4-byte read in the window overlaps it."""
    from config.offsets import FieldCalibWindows
    obs = Monster.CACHE_OBSCURED
    assert all(o + 4 <= obs or o >= obs + 8 for o in FieldCalibWindows.STAGE_KEY)
    assert min(FieldCalibWindows.STAGE_KEY) < obs < max(FieldCalibWindows.STAGE_KEY)  # still spanning
    r = ByteReader()
    mons = _monsters_with(r, 2)
    for m in mons:
        r.wi32(m + obs, 3301)
        r.wi32(m + obs + 4, 3301)
    msm = _msm_with_monsters(r, mons)
    assert field_calib.discover_stage_key(r, msm, STAGE_INFO) is None


def _hp(r, m, hc, cur, fields):
    r.wptr(m + Unit.HEALTH_CONTROLLER, hc)
    r.wf32(hc + UnitHealthController.HP_CURRENT, cur)
    for off, v in fields.items():
        r.wf32(hc + off, v)


def test_hp_max_rejects_copy_of_current():
    """A slot that only ever EQUALS current (a copy/previous-HP field) before the real max is not the
    max — the old `min(good)` took it."""
    r = ByteReader()
    m0, m1 = 0x10000, 0x20000
    _hp(r, m0, 0x50000, 500.0, {0x44: 500.0, 0x58: 900.0})
    _hp(r, m1, 0x51000, 300.0, {0x44: 300.0, 0x58: 900.0})
    msm = _msm_with_monsters(r, [m0, m1])
    assert field_calib.discover_hp_max(r, msm) == 0x58


def test_hp_max_static_wins_over_earlier_qualifying_slot():
    """A shield/duplicate float >= current at a lower offset does not beat a qualifying static."""
    r = ByteReader()
    m0 = 0x10000
    static = UnitHealthController.HP_MAX
    _hp(r, m0, 0x50000, 500.0, {0x44: 800.0, static: 900.0})
    msm = _msm_with_monsters(r, [m0])
    assert field_calib.discover_hp_max(r, msm) == static


def test_hp_max_none_when_no_monster_damaged():
    """Every mob at full HP: every candidate only equals current -> no proof -> static kept."""
    r = ByteReader()
    m0 = 0x10000
    _hp(r, m0, 0x50000, 900.0, {0x58: 900.0})
    msm = _msm_with_monsters(r, [m0])
    assert field_calib.discover_hp_max(r, msm) is None


def test_hp_max_full_health_static_wins_over_larger_float():
    """Full-health samples cannot disprove the static max, even with a larger float nearby."""
    r = ByteReader()
    static = UnitHealthController.HP_MAX
    mons = [0x10000, 0x20000]
    for i, m in enumerate(mons):
        _hp(r, m, 0x50000 + i * 0x1000, 900.0, {static: 900.0, 0x58: 1200.0})
    msm = _msm_with_monsters(r, mons)
    assert field_calib.discover_hp_max(r, msm) == static

    report = field_calib.calibrate(r, msm, [], {}, {})
    assert UnitHealthController.HP_MAX == static
    assert report["UnitHealthController.HP_MAX"] == (static, static, "found")


def _sm_with_party(r, sm, hl_off, cache_off, keys, hl=0x60000, base=0x70000):
    r.wptr(sm + hl_off, hl)
    r.wi32(hl + Array.MAX_LENGTH, len(keys))
    for i, hk in enumerate(keys):
        ha = base + i * 0x1000
        r.wptr(hl + Array.DATA + i * 8, ha)
        uf = ha + 0x8000
        info = uf + 0x4000
        r.wptr(ha + cache_off, uf)
        r.wptr(uf + HeroRuntime.INFO, info)
        r.wi32(info + HeroInfoData.HERO_KEY, hk)


def test_hero_list_ignores_junk_catalog_keys():
    """hero_cat junk keys (1, 2, 4, 8, 65535, ...) seen in the seed are not hero keys: a 'party' that
    only resolves junk keys is rejected."""
    r = ByteReader()
    sm = 0x1000
    _sm_with_party(r, sm, 0x58, 0x3A0, [2, 4, 8])
    cat = {**HERO_CAT, 1: 0, 2: 2, 4: 1, 8: 8, 65535: -1, 1376256: 5}
    assert field_calib.discover_hero_list_and_cache(r, [sm], cat) == (None, None)


def test_hero_list_static_pair_wins():
    """The static pair matches on one StageManager and another pair matches elsewhere: static kept."""
    r = ByteReader()
    sm_a, sm_b = 0x1000, 0x2000
    _sm_with_party(r, sm_a, 0x58, 0x3A0, [401, 301, 201], hl=0x60000, base=0x70000)
    _sm_with_party(r, sm_b, StageManager.HERO_LIST, Unit.CACHE, [101], hl=0x90000, base=0xA0000)
    found = field_calib.discover_hero_list_and_cache(r, [sm_a, sm_b], HERO_CAT)
    assert found == (StageManager.HERO_LIST, Unit.CACHE)


def test_hero_list_solo_tie_is_ambiguous():
    """Solo party (no >=2-distinct defence) resolving at TWO non-static pairs: ambiguous -> None."""
    r = ByteReader()
    sm_a, sm_b = 0x1000, 0x2000
    _sm_with_party(r, sm_a, 0x58, 0x3A0, [401], hl=0x60000, base=0x70000)
    _sm_with_party(r, sm_b, 0x60, 0x3B0, [301], hl=0x90000, base=0xA0000)
    found = field_calib.discover_hero_list_and_cache(r, [sm_a, sm_b], HERO_CAT)
    assert found == (None, None)


def test_hero_list_larger_party_beats_solo_match():
    """A full party at one pair outranks a solo coincidence at another pair."""
    r = ByteReader()
    sm_a, sm_b = 0x1000, 0x2000
    _sm_with_party(r, sm_a, 0x58, 0x3A0, [401, 301, 201], hl=0x60000, base=0x70000)
    _sm_with_party(r, sm_b, 0x60, 0x3B0, [101], hl=0x90000, base=0xA0000)
    found = field_calib.discover_hero_list_and_cache(r, [sm_a, sm_b], HERO_CAT)
    assert found == (0x58, 0x3A0)


def test_live_monsters_skips_summoned_alias_of_dead(monkeypatch):
    """SUMMONED_LIST aliasing DEAD_MONSTER_LIST (1.3.0) must not iterate dead units as summons; a
    distinct SUMMONED_LIST is still read."""
    from config.offsets import List
    from game import models
    r = ByteReader()
    msm = 0x1000
    alive, dead = 0x10000, 0x20000
    for field, items, unit in ((MonsterSpawnManager.MONSTER_LIST, 0x3000, alive),
                               (0x30, 0x5000, dead)):
        lst = items - 0x800
        r.wptr(msm + field, lst)
        r.wi32(lst + List.SIZE, 1)
        r.wptr(lst + List.ITEMS, items)
        r.wptr(items + Array.DATA, unit)
        _hp(r, unit, unit + 0x8000, 100.0, {UnitHealthController.HP_MAX: 200.0})
    monkeypatch.setattr(MonsterSpawnManager, "DEAD_MONSTER_LIST", 0x30)
    monkeypatch.setattr(MonsterSpawnManager, "SUMMONED_LIST", 0x30)
    assert [u for u, *_ in models.live_monsters(r, msm)] == [alive]
    monkeypatch.setattr(MonsterSpawnManager, "DEAD_MONSTER_LIST", 0x40)
    assert [u for u, *_ in models.live_monsters(r, msm)] == [alive, dead]


def test_live_monster_snapshot_marks_failed_reads_invalid():
    """A failed list read is an INVALID snapshot (None), not an empty stage: treating it as empty
    counted every live monster dead, then dead again when it reappeared (kill overcount). An
    unreadable HealthController keeps the unit with hp=None; a null list is a genuine empty slot."""
    from config.offsets import List
    from game import models

    class FlakyReader(ByteReader):
        def __init__(self):
            super().__init__()
            self.bad = set()

        def read(self, addr, size):
            return None if addr in self.bad else super().read(addr, size)

    r = FlakyReader()
    msm, items, a, b = 0x1000, 0x3000, 0x10000, 0x20000
    lst = items - 0x800
    r.wptr(msm + MonsterSpawnManager.MONSTER_LIST, lst)
    r.wi32(lst + List.SIZE, 2)
    r.wptr(lst + List.ITEMS, items)
    r.wptr(items + Array.DATA, a)
    r.wptr(items + Array.DATA + 8, b)
    _hp(r, a, a + 0x8000, 100.0, {UnitHealthController.HP_MAX: 200.0})
    _hp(r, b, b + 0x8000, 50.0, {UnitHealthController.HP_MAX: 200.0})

    assert [(u, c) for u, c, _m in models.live_monster_snapshot(r, msm)] == [(a, 100.0), (b, 50.0)]
    for bad in (msm + MonsterSpawnManager.MONSTER_LIST, lst + List.SIZE, lst + List.ITEMS,
                items + Array.DATA):
        r.bad = {bad}
        assert models.live_monster_snapshot(r, msm) is None, hex(bad)
    r.bad = {b + Unit.HEALTH_CONTROLLER}
    assert [(u, c) for u, c, _m in models.live_monster_snapshot(r, msm)] == [(a, 100.0), (b, None)]
    r.bad = set()
    r.wptr(b + Unit.HEALTH_CONTROLLER, 0)           # null HC (valid read) -> omitted = gone
    assert [u for u, *_ in models.live_monster_snapshot(r, msm)] == [a]
    r.wi32(lst + List.SIZE, 601)                     # implausible size -> invalid
    assert models.live_monster_snapshot(r, msm) is None
    r.wptr(msm + MonsterSpawnManager.MONSTER_LIST, 0)
    assert models.live_monster_snapshot(r, msm) == []
