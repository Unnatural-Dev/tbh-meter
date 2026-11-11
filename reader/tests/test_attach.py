"""Attachment behavior through prepare, with in-memory reads and process edges only."""
import json
from pathlib import Path
from dataclasses import FrozenInstanceError

import pytest

from config.offsets import (Array, CommonSaveData, HeroInfoData, HeroRuntime,
                            ItemInfoData, LogManager, List,
                            StageInfoData, StageManager, Unit, Monster, UnitHealthController)
from game import attach
from tests.conftest import MockReader

FP = "attachment-test-build"
MSM, LM, CSD, SM, PSD = 0x100000, 0x200000, 0x300000, 0x400000, 0x500000
STAGE, ITEM, HERO = 0x600000, 0x700000, 0x800000
GOLD = 0x900000
CALIB = {"anchor_rva": 128, "indices": {"StageClearLog": 1}, "idx_ut": 42,
         "stage_info": {1001: (1, 1, 50, 0)}, "item_cat": {30001: (3, 2, 5)},
         "hero_cat": {601: 1}}


class Memory(MockReader):
    def list_ptrs(self, ptr, cap=1000):
        return self.list_iter(ptr, cap)

    def read_array_ptrs(self, ptr, n):
        return [self.rptr(ptr + Array.DATA + i * 8) for i in range(n)]


@pytest.fixture
def world(monkeypatch, tmp_path):
    # Field calibration still runs for real; restore its process-global offset writes.
    for cls, name in [(Monster, "STAGE_KEY"), (UnitHealthController, "HP_MAX"),
                      (StageManager, "HERO_LIST"), (Unit, "CACHE")]:
        monkeypatch.setattr(cls, name, getattr(cls, name))
    mem = {CSD + CommonSaveData.CURRENT_STAGE_KEY: 1001,
           CSD + CommonSaveData.PLAYTIME: 500.0,
           SM + StageManager.HERO_LIST: 0x410000,
           0x410000 + Array.MAX_LENGTH: 1, 0x410000 + Array.DATA: 0x420000,
           0x420000 + Unit.CACHE: 0x430000, 0x430000 + HeroRuntime.INFO: HERO,
           HERO + HeroInfoData.HERO_KEY: 601, HERO + HeroInfoData.CLASS_TYPE: 1,
           STAGE + StageInfoData.STAGE_KEY: 1001, STAGE + StageInfoData.ACT: 1,
           STAGE + StageInfoData.STAGE_NO: 1, STAGE + StageInfoData.DIFFICULTY: 0,
           STAGE + StageInfoData.WAVE_AMOUNT: 10, STAGE + StageInfoData.WAVE_MOB_AMOUNT: 5,
           ITEM + ItemInfoData.ITEM_KEY: 30001, ITEM + ItemInfoData.GRADE: 3,
           ITEM + ItemInfoData.PARTS: 2, ITEM + ItemInfoData.LEVEL: 5,
           LM + LogManager.LOG_LIST: 0x210000, 0x210000 + List.SIZE: 0}
    # Unreadable monster list avoids a real-time wave-gap wait; slow manager selection
    # retains its historical loose fallback when only SIZE is readable.
    classes = {n: {0xA00000 + i * 256} for i, n in enumerate(attach.TARGETS)}
    inst = {n: [] for n in attach.TARGETS}
    inst.update(MonsterSpawnManager=[MSM], LogManager=[LM], CommonSaveData=[CSD],
                PlayerSaveData=[PSD], StageManager=[SM], StageInfoData=[STAGE],
                ItemInfoData=[ITEM], HeroInfoData=[HERO])
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"fmt": attach.CACHE_FMT, "calib": {FP: CALIB}}))
    monkeypatch.setattr(attach.typeinfo, "ga_module", lambda pid: (0xB00000, 0x100000))
    monkeypatch.setattr(attach.typeinfo, "table_base", lambda *a: 0xC00000)
    monkeypatch.setattr(attach, "regions", lambda r: [])
    monkeypatch.setattr(attach, "resolve_via_rva", lambda *a: (classes, inst))
    monkeypatch.setattr(attach, "instances_of", lambda *a, **k: dict(inst))
    monkeypatch.setattr(attach, "resolve", lambda *a: pytest.fail("unexpected cold scan"))
    monkeypatch.setattr(attach, "resolve_combat_gold_klass_by_index", lambda *a: GOLD)
    monkeypatch.setattr(attach, "combat_gold_klass_ok", lambda *a: True, raising=False)
    monkeypatch.setattr(attach, "combat_gold_live", lambda *a: 123, raising=False)
    return Memory(mem), classes, inst, str(cache), tmp_path


def prepare(world, *, startup=True, fp=FP):
    reader, _, _, cache, output = world
    return attach.prepare(reader, 123, fp, cache, str(output), "test-version", startup=startup)


def test_fast_path_prepares_carriers_and_catalogs_without_scan(world):
    state = prepare(world)
    assert isinstance(state, attach.AttachedState)
    assert (state.msm, state.lm, state.csd, state.sm) == (MSM, LM, CSD, SM)
    assert state.sc_class == 0xA00200
    assert state.sf_class == 0xA00300
    assert state.psd_list == [PSD]
    assert state.stage_info == {1001: (1, 1, 50, 0)}
    assert state.item_cat == {30001: (3, 2, 5)}
    assert state.hero_cat == {601: 1}
    assert (state.gold_klass, state.tbase, state.idx_ut) == (GOLD, 0xC00000, 42)
    assert state.catalog_instances == {"StageInfoData": [STAGE], "ItemInfoData": [ITEM]}
    assert state.stage_waves == {1001: (10, 5)}
    with pytest.raises(FrozenInstanceError):
        state.sm = None


@pytest.mark.parametrize("missing", ["MonsterSpawnManager", "LogManager", "StageClearLog", "StageFailedLog"])
def test_incomplete_resolution_returns_none_without_calibration(world, monkeypatch, missing, capsys):
    reader, classes, inst, cache, output = world
    # Exercise the scan's completeness gate with readable loose-fallback managers.
    reader._mem[List.SIZE] = 0
    if missing in ("MonsterSpawnManager", "LogManager"):
        inst[missing] = []
    else:
        classes[missing] = set()
    Path(cache).unlink()
    monkeypatch.setattr(attach, "resolve", lambda *a: (classes, inst))
    monkeypatch.setattr(attach, "resolve_combat_gold_klass", lambda *a: GOLD)
    monkeypatch.setattr(attach.typeinfo, "discover_anchor", lambda *a: pytest.fail("incomplete calibrated"))
    assert prepare(world) is None
    assert not Path(cache).exists()
    assert not (output / "drop_catalog.json").exists()
    assert "[[STATUS]] ready" not in capsys.readouterr().out


@pytest.mark.parametrize("startup", [True, False])
def test_reattach_gets_startup_gold_recheck_and_diagnostics(world, monkeypatch, capsys, startup):
    lines = []
    monkeypatch.setattr(attach, "diag", lines.append)
    monkeypatch.setattr(attach, "combat_gold_klass_ok", lambda *a: False)
    # The first by-index resolution succeeds, but its startup re-check fails.
    answers = iter([GOLD, None])
    monkeypatch.setattr(attach, "resolve_combat_gold_klass_by_index", lambda *a: next(answers))
    monkeypatch.setattr(attach, "resolve_combat_gold_klass", lambda *a: GOLD + 256)
    state = prepare(world, startup=startup)
    # A stale klass is re-resolved on BOTH paths (re-attach used to keep it until a run self-healed).
    assert state.gold_klass == GOLD + 256
    label = "startup" if startup else "re-attach"
    assert any(f"[party-pick] {label} candidates=" in line and "party_read=" in line for line in lines)
    assert any("[save-pick]" in line for line in lines)
    assert any("[gold]" in line for line in lines)
    assert any(line.startswith("[resolve] fp=") for line in lines)
    stdout = capsys.readouterr().out
    assert "[ok] resolved in" in stdout
    assert "[live party]" in stdout
    assert "[live gold]" in stdout
    assert "[[STATUS]] ready" not in stdout and "[[STATUS]] searching" not in stdout


@pytest.mark.parametrize("startup", [True, False])
@pytest.mark.parametrize("case", ["incomplete", "resolution-error", "carrier-error", "gold-error", "complete"])
def test_preparation_stdout_matches_supervisor_contract(world, monkeypatch, capsys, case, startup):
    """The human ready fallback is a success signal, never a resolution-only signal.

    The supervisor integration test consumes this same full stdout fixture; no copy
    of its private parser is involved. A fixed clock makes elapsed diagnostics stable.
    """
    contract = json.loads((Path(__file__).parent / "fixtures" / "attachment-status.json")
                          .read_text(encoding="utf-8"))
    reader, _, inst, _, _ = world
    monkeypatch.setattr(attach.time, "time", lambda: 1000.0)
    if case == "incomplete":
        inst["MonsterSpawnManager"] = []
    elif case == "resolution-error":
        def fail_resolution(*a, **k):
            raise RuntimeError("resolution read failed")
        monkeypatch.setattr(attach, "instances_of", fail_resolution)
    elif case == "carrier-error":
        original = reader.ri32

        def read(a):
            if a == CSD + CommonSaveData.CURRENT_STAGE_KEY:
                raise RuntimeError("carrier read failed")
            return original(a)
        monkeypatch.setattr(reader, "ri32", read)
    elif case == "gold-error":
        def fail_gold(*a):
            raise RuntimeError("gold read failed")
        monkeypatch.setattr(attach, "combat_gold_klass_ok", fail_gold)

    if case in ("carrier-error", "gold-error") or (case == "resolution-error" and startup):
        with pytest.raises(RuntimeError, match="read failed"):
            prepare(world, startup=startup)
    else:
        state = prepare(world, startup=startup)
        assert (state is not None) == (case == "complete")
    assert capsys.readouterr().out == contract[case]["stdout"]


def test_two_prepares_keep_their_own_catalog_instances(world):
    first = prepare(world)
    world[2]["StageInfoData"] = []
    world[2]["ItemInfoData"] = []
    second = prepare(world, startup=False)
    assert first.catalog_instances == {"StageInfoData": [STAGE], "ItemInfoData": [ITEM]}
    assert first.stage_waves == {1001: (10, 5)}
    assert second.catalog_instances == {"StageInfoData": [], "ItemInfoData": []}
    assert second.stage_waves == {}
    assert not hasattr(attach, "_LAST_CATALOG_INSTANCES")


def scan_world(world, monkeypatch):
    reader, classes, inst, cache, _ = world
    # Structural list header unreadable, SIZE readable: existing loose fallback.
    reader._mem[List.SIZE] = 0
    monkeypatch.setattr(attach, "resolve", lambda *a: (classes, inst))
    monkeypatch.setattr(attach, "resolve_combat_gold_klass", lambda *a: GOLD)
    monkeypatch.setattr(attach.typeinfo, "discover_anchor",
                        lambda *a: (256, 0xC00000, {"StageClearLog": 2}))
    monkeypatch.setattr(attach, "gold_index_of_klass", lambda *a: 43)
    return cache


@pytest.mark.parametrize("failure", ["rva", "table", "gold", "cache", "fp", "module"])
def test_fast_path_failure_falls_back_to_scan(world, monkeypatch, failure, capsys):
    cache = scan_world(world, monkeypatch)
    fp = FP
    if failure == "rva":
        monkeypatch.setattr(attach, "resolve_via_rva", lambda *a: None)
    elif failure == "table":
        monkeypatch.setattr(attach.typeinfo, "table_base", lambda *a: None)
    elif failure == "gold":
        monkeypatch.setattr(attach, "resolve_combat_gold_klass_by_index", lambda *a: None)
    elif failure == "cache":
        Path(cache).unlink()
    elif failure == "fp":
        fp = None
        monkeypatch.setattr(attach, "resolve_via_rva", lambda *a: pytest.fail("no fp used RVA"))
    else:
        monkeypatch.setattr(attach.typeinfo, "ga_module", lambda *a: (None, None))
        monkeypatch.setattr(attach, "resolve_via_rva", lambda *a: pytest.fail("no module used RVA"))
    state = prepare(world, startup=False, fp=fp)
    assert state is not None
    assert (state.msm, state.lm, state.csd, state.sm) == (MSM, LM, CSD, SM)
    assert state.stage_info == {1001: (1, 1, 50, 0)}
    assert "[[STATUS]] scanning" in capsys.readouterr().out
    if failure not in ("fp", "module"):
        entry = json.loads(Path(cache).read_text())["calib"][FP]
        assert (entry["anchor_rva"], entry["idx_ut"]) == (256, 43)
        assert entry["indices"] == {"StageClearLog": 2}


@pytest.mark.parametrize("failure, message", [
    ("anchor", "FAILED to discover anchor"),
    ("index", "FAILED to locate gold idx in table"),
    ("module", "FAILED to read GameAssembly.dll"),
])
def test_calibration_failure_logs_without_crashing(world, monkeypatch, failure, message, capsys):
    cache = scan_world(world, monkeypatch)
    Path(cache).unlink()
    if failure == "anchor":
        monkeypatch.setattr(attach.typeinfo, "discover_anchor", lambda *a: None)
    elif failure == "index":
        monkeypatch.setattr(attach, "gold_index_of_klass", lambda *a: None)
        monkeypatch.setattr(attach, "gold_index_by_structure", lambda *a: None)
    else:
        monkeypatch.setattr(attach.typeinfo, "ga_module", lambda *a: (None, None))
    assert prepare(world) is not None
    assert not Path(cache).exists()
    assert message in capsys.readouterr().out


def test_calibration_uses_structure_when_value_scan_has_no_gold(world, monkeypatch):
    cache = scan_world(world, monkeypatch)
    Path(cache).unlink()
    monkeypatch.setattr(attach, "resolve_combat_gold_klass", lambda *a: None)
    monkeypatch.setattr(attach, "gold_index_of_klass", lambda *a: pytest.fail("missing gold shortcut"))
    monkeypatch.setattr(attach, "gold_index_by_structure", lambda *a: 44)
    state = prepare(world, startup=False)
    # The scan found no gold; the re-check recovers it via the freshly calibrated index.
    assert state.gold_klass == GOLD
    assert state.idx_ut == 44
    assert json.loads(Path(cache).read_text())["calib"][FP]["idx_ut"] == 44


def test_catalog_sweep_preserves_catalogs_but_bounds_carrier_candidates(world, monkeypatch):
    def instances(reader, regs, needles, cap):
        assert cap == attach.CATALOG_INSTANCE_CAP
        assert needles == {"PlayerSaveData": 0xA00B00, "CommonSaveData": 0xA00700,
                           "StageManager": 0xA00E00, "StageInfoData": 0xA00A00,
                           "ItemInfoData": 0xA00C00}
        return {name: [0] * 9000 for name in needles}
    monkeypatch.setattr(attach, "instances_of", instances)
    state = prepare(world, startup=False)
    assert len(state.catalog_instances["ItemInfoData"]) == 9000
    assert len(state.catalog_instances["StageInfoData"]) == 9000
    assert len(state.psd_list) == len(state.csd_list) == len(state.sm_list) == 4000


def test_prepare_exports_drop_catalog_from_this_attachment(world):
    reader, _, _, _, output = world
    reader._mem.update({
        STAGE + StageInfoData.MONSTER_DROP_KEY: 910011,
        STAGE + StageInfoData.MONSTER_DROP_RATE: 160000,
        ITEM + ItemInfoData.ITEM_KEY: 910011,
        ITEM + ItemInfoData.DROP_COOLDOWN: 120,
    })
    state = prepare(world)
    doc = json.loads((output / "drop_catalog.json").read_text())
    assert state.catalog_instances["ItemInfoData"] == [ITEM]
    assert doc["gameVersion"] == "test-version"
    assert doc["stages"]["1001"]["monster"] == {
        "boxKey": 910011, "rate": 0.16, "cooldownSec": 120}
    assert not (output / "drop_catalog.json.tmp").exists()


@pytest.mark.parametrize("startup", [True, False])
def test_ghost_diagnostics_on_both_paths(world, monkeypatch, startup):
    reader, _, inst, _, _ = world
    ghost = 0xD00000
    inst["StageManager"].insert(0, ghost)
    reader._mem.update({ghost + StageManager.HERO_LIST: 0xD10000,
                        0xD10000 + Array.MAX_LENGTH: 1, 0xD10000 + Array.DATA: 0xD20000,
                        0xD20000 + Unit.CACHE: 0xD30000,
                        0xD30000 + HeroRuntime.INFO: 0xD40000,
                        0xD40000 + HeroInfoData.HERO_KEY: 999})
    lines = []
    monkeypatch.setattr(attach, "diag", lines.append)
    state = prepare(world, startup=startup)
    assert state.sm == SM
    assert any("candidates=2 hk-accept=2 carriers=1" in line for line in lines)
    assert any("[party-pick]   ghost 0xd00000" in line for line in lines)

def test_reattach_resolution_error_reports_incomplete_but_startup_raises(world, monkeypatch):
    def boom(*a):
        raise RuntimeError("resolution exploded")
    monkeypatch.setattr(attach, "_resolve_all", boom)
    assert prepare(world, startup=False) is None
    with pytest.raises(RuntimeError):
        prepare(world, startup=True)


def test_reattach_errors_after_resolution_still_escape(world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("carrier pick exploded")
    monkeypatch.setattr(attach.save, "pick_live_sm", boom)
    with pytest.raises(RuntimeError):
        prepare(world, startup=False)


def test_fast_path_carriers_come_from_the_backref_not_rva_instances(world, monkeypatch):
    _, classes, inst, _, _ = world
    rva_inst = dict(inst, PlayerSaveData=[], CommonSaveData=[], StageManager=[])
    monkeypatch.setattr(attach, "resolve_via_rva", lambda *a: (classes, rva_inst))
    state = prepare(world, startup=False)
    assert (state.psd_list, state.csd_list, state.sm_list) == ([PSD], [CSD], [SM])


def test_successful_scan_calibration_announces_fast_path(world, monkeypatch, capsys):
    cache = scan_world(world, monkeypatch)
    Path(cache).unlink()
    prepare(world, startup=False)
    assert "fast path armed" in capsys.readouterr().out


def _field_state():
    return (Monster.STAGE_KEY, UnitHealthController.HP_MAX, StageManager.HERO_LIST, Unit.CACHE)


def _moved_field_memory(world, monkeypatch):
    from config.offsets import MonsterSpawnManager

    reader = world[0]
    ml, monster, hc = 0x110000, 0x120000, 0x130000
    reader._mem.update({MSM + MonsterSpawnManager.MONSTER_LIST: ml, ml + List.SIZE: 1,
                        monster + 0x444: 1001, monster + Unit.HEALTH_CONTROLLER: hc,
                        hc + UnitHealthController.HP_CURRENT: 500.0, hc + 0x58: 900.0})
    reader._lists[ml] = [monster]
    monkeypatch.setattr(reader, "list_ptrs", lambda ptr, cap=600: list(reader.list_iter(ptr, cap)))
    monkeypatch.setattr(reader, "read_struct",
                        lambda addr, fmt: (reader._mem.get(addr, 0),), raising=False)
    reader._mem[SM + 0x58] = reader._mem.pop(SM + StageManager.HERO_LIST)
    reader._mem[0x420000 + 0x3A0] = reader._mem.pop(0x420000 + Unit.CACHE)


@pytest.mark.parametrize("path", ["fast", "scan"])
@pytest.mark.parametrize("startup", [True, False])
def test_prepare_field_calibration_error_retains_previous_values(world, monkeypatch, path, startup):
    """Real prepare -> resolution -> wrapper -> calibration must undo a late logging failure."""
    _moved_field_memory(world, monkeypatch)
    if path == "scan":
        Path(scan_world(world, monkeypatch)).unlink()
    before = _field_state()
    lines = []

    def diag(message):
        lines.append(message)
        if message.startswith("[field-calib] Unit.CACHE:"):
            raise RuntimeError("final field diagnostic failed")

    monkeypatch.setattr(attach, "diag", diag)
    state = prepare(world, startup=startup)
    assert isinstance(state, attach.AttachedState)
    assert _field_state() == before
    assert any("previous values retained" in line and "final field diagnostic failed" in line
               for line in lines)
    assert not any("static offsets kept" in line for line in lines)


@pytest.mark.parametrize("changed_fp", [False, True])
@pytest.mark.parametrize("calibration_fails", [False, True])
def test_incomplete_prepare_retry_keeps_calibration_lifetime(world, monkeypatch,
                                                            changed_fp, calibration_fails):
    """Incomplete preparation is not a new offset lifetime, even across fingerprints."""
    _moved_field_memory(world, monkeypatch)
    Path(scan_world(world, monkeypatch)).unlink()
    before = _field_state()
    classes = world[1]
    logs = classes["StageClearLog"]
    classes["StageClearLog"] = set()
    lines = []

    def diag(message):
        lines.append(message)
        if calibration_fails and message.startswith("[field-calib] Unit.CACHE:"):
            raise RuntimeError("calibration diagnostic failed")

    monkeypatch.setattr(attach, "diag", diag)
    assert prepare(world) is None
    expected = before if calibration_fails else (0x444, 0x58, 0x58, 0x3A0)
    assert _field_state() == expected
    assert any(line.startswith("[field-calib]") for line in lines)
    classes["StageClearLog"] = logs
    # A later no-signal attachment keeps the prior successful transaction (or rolled-back entry).
    world[0]._mem.clear()
    world[0]._mem[List.SIZE] = 0
    world[0]._lists.clear()
    state = prepare(world, startup=False, fp="different-build" if changed_fp else FP)
    assert isinstance(state, attach.AttachedState)
    assert _field_state() == expected


@pytest.mark.parametrize("startup", [True, False])
def test_prepare_diagnostic_handler_failure_still_leaves_offsets_restored(world, monkeypatch, startup):
    _moved_field_memory(world, monkeypatch)
    before = _field_state()

    def diag(message):
        if message.startswith("[field-calib]"):
            raise RuntimeError("diagnostic sink unavailable")

    monkeypatch.setattr(attach, "diag", diag)
    if startup:
        with pytest.raises(RuntimeError, match="diagnostic sink unavailable"):
            prepare(world)
    else:
        assert prepare(world, startup=False) is None
    assert _field_state() == before


def test_scan_without_fingerprint_skips_calibration_silently(world, monkeypatch, capsys):
    scan_world(world, monkeypatch)
    monkeypatch.setattr(attach.typeinfo, "discover_anchor", lambda *a: pytest.fail("calibrated without fp"))
    assert prepare(world, startup=False, fp=None) is not None
    assert "[calib] FAILED" not in capsys.readouterr().out
