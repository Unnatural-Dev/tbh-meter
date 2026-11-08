"""Regression: attach-time field calibration must not silently fall back because it sampled a wave gap.

WHY: _autocalibrate_fields sampled MonsterSpawnManager.MONSTER_LIST ONCE at resolve. The list is
validly empty ~16% of the time (between waves; live 1.3.2 measurement), and the stable reader's
reader-diag.log shows 9 of 27 resolves keeping BOTH Monster.STAGE_KEY and HP_MAX static ("signature
not found live"). Harmless while the statics are right, but after a game update that shifts them the
reader would silently keep wrong offsets whenever it attached during a gap.

Fix: when the monster list is EMPTY (not unreadable), wait a short bounded window for monsters, then
run the discoveries. Static fallback is preserved when no monster ever appears.
"""

import struct

from config.offsets import (Array, List, Monster, MonsterSpawnManager, StageManager, Unit,
                            UnitHealthController)
from game import field_calib

STAGE_INFO = {3301: (3, 1, 459, 2)}


class FlipReader:
    """Byte memory whose MONSTER_LIST size reads 0 for the first `empty_reads` size reads."""

    def __init__(self, empty_reads):
        self.mem = bytearray(0x400000)
        self.empty_reads = empty_reads
        self.size_addr = None

    def w(self, addr, fmt, v):
        self.mem[addr:addr + struct.calcsize(fmt)] = struct.pack(fmt, v)

    def read(self, addr, size):
        if addr == self.size_addr and size == 4 and self.empty_reads > 0:
            self.empty_reads -= 1
            return b"\x00\x00\x00\x00"
        return bytes(self.mem[addr:addr + size])

    def ri32(self, a):
        return struct.unpack("<i", self.read(a, 4))[0]

    def rptr(self, a):
        return struct.unpack("<Q", self.read(a, 8))[0]

    def read_struct(self, a, fmt):
        return struct.unpack(fmt, self.read(a, struct.calcsize(fmt)))

    def read_array_ptrs(self, arr, n):
        return list(struct.unpack(f"<{n}Q", self.read(arr + Array.DATA, n * 8)))

    def list_ptrs(self, lst, cap=600):
        size, items = self.ri32(lst + List.SIZE), self.rptr(lst + List.ITEMS)
        if not size or not items or size > cap:
            return []
        return [p for p in self.read_array_ptrs(items, size) if p]


def _world(empty_reads, stage_off=0x444, hp_off=0x58):
    r = FlipReader(empty_reads)
    msm, lst, items = 0x1000, 0x2000, 0x3000
    mons = [0x10000, 0x20000]
    r.w(msm + MonsterSpawnManager.MONSTER_LIST, "<Q", lst)
    r.w(lst + List.SIZE, "<i", len(mons))
    r.w(lst + List.ITEMS, "<Q", items)
    r.size_addr = lst + List.SIZE
    for i, m in enumerate(mons):
        r.w(items + Array.DATA + i * 8, "<Q", m)
        r.w(m + stage_off, "<i", 3301)
        hc = 0x50000 + i * 0x1000
        r.w(m + Unit.HEALTH_CONTROLLER, "<Q", hc)
        r.w(hc + UnitHealthController.HP_CURRENT, "<f", 500.0)
        r.w(hc + hp_off, "<f", 900.0)
    return r, msm


def _restore(saved):
    (Monster.STAGE_KEY, UnitHealthController.HP_MAX, StageManager.HERO_LIST, Unit.CACHE) = saved


def test_calibrate_retries_through_wave_gap():
    """RED on the single-sample calibrate: the first sample lands in a wave gap."""
    saved = (Monster.STAGE_KEY, UnitHealthController.HP_MAX, StageManager.HERO_LIST, Unit.CACHE)
    try:
        r, msm = _world(empty_reads=6)
        report = field_calib.calibrate(r, msm, [], STAGE_INFO, {}, sleep=lambda s: None)
        assert report["Monster.STAGE_KEY"][2] == "found" and Monster.STAGE_KEY == 0x444
        assert report["UnitHealthController.HP_MAX"][2] == "found" and UnitHealthController.HP_MAX == 0x58
    finally:
        _restore(saved)


def test_calibrate_keeps_static_when_list_stays_empty():
    saved = (Monster.STAGE_KEY, UnitHealthController.HP_MAX, StageManager.HERO_LIST, Unit.CACHE)
    try:
        r, msm = _world(empty_reads=10_000)
        slept = []
        report = field_calib.calibrate(r, msm, [], STAGE_INFO, {}, sleep=slept.append)
        assert (Monster.STAGE_KEY, UnitHealthController.HP_MAX) == saved[:2]
        assert report["Monster.STAGE_KEY"][2] == "fallback"
        assert len(slept) == 30
        assert abs(sum(slept) - 6.0) < 1e-9     # bounded: attach waits at most ~6 s
    finally:
        _restore(saved)


class _NoneReader:
    """Every read fails (the production reader returns None on a failed ReadProcessMemory)."""

    def __init__(self, ptr):
        self.ptr = ptr

    def rptr(self, a):
        return self.ptr

    def ri32(self, a):
        return None


def test_wait_returns_at_once_on_unreadable_list():
    slept = []
    assert field_calib._wait_for_monsters(_NoneReader(None), 0x1000, slept.append) == 0
    assert field_calib._wait_for_monsters(_NoneReader(0x2000), 0x1000, slept.append) == 0
    assert slept == []


def test_wait_returns_at_once_on_null_list():
    slept = []
    assert field_calib._wait_for_monsters(_NoneReader(0), 0x1000, slept.append) == 0
    assert slept == []


def test_calibrate_does_not_retry_without_msm():
    slept = []
    field_calib.calibrate(FlipReader(0), 0, [], STAGE_INFO, {}, sleep=slept.append)
    assert slept == []
