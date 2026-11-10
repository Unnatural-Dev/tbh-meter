"""game/drops.py — the live drop catalog (per-stage box keys/rates + per-box DropCooldown).

Values mirror the LIVE 1.3.2 read (StageInfoData/ItemInfoData dumped read-only): 1-1 Normal
(1101) drops Normal Monster Box 1 (910011, 160000 ppm, 120s) + Stage Boss Box 4 (920011,
1000000 ppm, 120s); 3-8 Nightmare (2308) 910501 @15000 (180s) + 920501 @150000 (300s).
"""

import json

from game import attach as mw
from config.offsets import StageInfoData, ItemInfoData
from game import drops
from tests.conftest import MockReader

STAGE_INFO = {1101: (1, 1, 10, 0), 2308: (3, 8, 345, 1)}   # (act, stage_no, horde, diff)


def _stage(mem, a, sk, mk, mr, bk, br):
    mem.update({a + StageInfoData.STAGE_KEY: sk,
                a + StageInfoData.MONSTER_DROP_KEY: mk, a + StageInfoData.MONSTER_DROP_RATE: mr,
                a + StageInfoData.BOSS_DROP_KEY: bk, a + StageInfoData.BOSS_DROP_RATE: br})


def _item(mem, a, key, cd):
    mem.update({a + ItemInfoData.ITEM_KEY: key, a + ItemInfoData.DROP_COOLDOWN: cd})


def _reader():
    mem = {}
    _stage(mem, 0x1000, 1101, 910011, 160000, 920011, 1000000)
    _stage(mem, 0x2000, 2308, 910501, 15000, 920501, 150000)
    _stage(mem, 0x3000, 9999, 123, 5, -7, 2_000_000)          # garbage instance: no box keys
    _stage(mem, 0x4000, 7777, 910011, 1, 920011, 1)           # not in the validated stage_info
    _stage(mem, 0x5000, 1101, 1, 1, 1, 1)                     # duplicate key: first row wins
    _stage(mem, 0x6000, 8888, 910011, 1_000_001, 920011, None)  # box keys, implausible/unread rates
    for a, k, cd in ((0x10000, 910011, 120), (0x11000, 920011, 120), (0x12000, 910501, 180),
                     (0x13000, 920501, 300), (0x14000, 930101, 0),
                     (0x15000, 101001, 999),                    # not a box — must be ignored
                     (0x16000, 910777, 999_999),                # implausible cooldown — dropped
                     (0x17000, 910888, -1),                     # negative cooldown — dropped
                     (0x18000, 910999, None),                   # unreadable cooldown — dropped
                     (0x19000, 910011, 777)):                   # duplicate key: first row wins
        _item(mem, a, k, cd)
    return MockReader(mem=mem)


STAGES = [0x1000, 0x2000, 0x3000, 0x4000, 0x5000, 0x6000]
ITEMS = [0x10000, 0x11000, 0x12000, 0x13000, 0x14000, 0x15000, 0x16000, 0x17000, 0x18000, 0x19000]


def test_box_cooldowns_only_boxes_and_plausible():
    cds = drops.read_box_cooldowns(_reader(), ITEMS)
    assert cds == {910011: 120, 920011: 120, 910501: 180, 920501: 300, 930101: 0}


def test_stage_drops_rejects_non_box_slots():
    sd = drops.read_stage_drops(_reader(), STAGES)
    assert sd[1101] == {"monster": (910011, 160000), "boss": (920011, 1000000)}
    assert sd[9999] == {"monster": None, "boss": None}
    assert sd[8888] == {"monster": None, "boss": None}         # rate > 1e6 ppm / unread rate


def test_catalog_joins_cooldowns_and_gates_on_stage_info():
    r = _reader()
    doc = drops.build_catalog(drops.read_stage_drops(r, STAGES), drops.read_box_cooldowns(r, ITEMS),
                              STAGE_INFO, "1.3.2")
    assert doc["fmt"] == drops.CATALOG_FMT and doc["gameVersion"] == "1.3.2"
    assert set(doc["stages"]) == {"1101", "2308"}               # 9999/7777 not in stage_info
    s = doc["stages"]["2308"]
    assert (s["act"], s["stageNo"], s["difficulty"]) == (3, 8, 1)
    assert s["monster"] == {"boxKey": 910501, "rate": 0.015, "cooldownSec": 180}
    assert s["boss"] == {"boxKey": 920501, "rate": 0.15, "cooldownSec": 300}
    assert doc["boxCooldowns"]["910011"] == 120


def test_catalog_cooldown_null_when_box_row_unread():
    doc = drops.build_catalog({1101: {"monster": (910011, 160000), "boss": None}}, {}, STAGE_INFO, "x")
    assert doc["stages"]["1101"]["monster"] == {"boxKey": 910011, "rate": 0.16, "cooldownSec": None}
    assert doc["stages"]["1101"]["boss"] is None


def test_never_raises_on_unreadable_reader():
    class Dead:
        def ri32(self, a):
            raise OSError("process gone")
    assert drops.read_box_cooldowns(Dead(), [1, 2]) == {}
    assert drops.read_stage_drops(Dead(), [1, 2]) == {}


class TestWriteDropCatalog:
    """game.attach.write_drop_catalog: the orchestrator's never-raises glue. Writes ONLY when the
    latest resolve left both instance lists AND at least one stage survives the stage_info gate."""

    def test_writes_gated_catalog(self, monkeypatch, tmp_path):
        inst = {"StageInfoData": STAGES, "ItemInfoData": ITEMS}
        assert mw.write_drop_catalog(_reader(), str(tmp_path), STAGE_INFO, "1.3.2", inst) == 2
        doc = json.loads((tmp_path / "drop_catalog.json").read_text(encoding="utf-8"))
        assert set(doc["stages"]) == {"1101", "2308"} and doc["gameVersion"] == "1.3.2"
        assert doc["stages"]["1101"]["boss"] == {"boxKey": 920011, "rate": 1.0, "cooldownSec": 120}
        assert not (tmp_path / "drop_catalog.json.tmp").exists()

    def test_missing_instances_writes_nothing(self, monkeypatch, tmp_path):
        for inst in ({}, {"StageInfoData": STAGES, "ItemInfoData": []},
                     {"StageInfoData": [], "ItemInfoData": ITEMS}):
            assert mw.write_drop_catalog(_reader(), str(tmp_path), STAGE_INFO, "1.3.2", inst) == 0
        assert not (tmp_path / "drop_catalog.json").exists()

    def test_no_stage_in_stage_info_writes_nothing(self, monkeypatch, tmp_path):
        inst = {"StageInfoData": STAGES, "ItemInfoData": ITEMS}
        assert mw.write_drop_catalog(_reader(), str(tmp_path), {}, "1.3.2", inst) == 0
        assert not (tmp_path / "drop_catalog.json").exists()

    def test_write_failure_returns_zero(self, monkeypatch, tmp_path):
        inst = {"StageInfoData": STAGES, "ItemInfoData": ITEMS}
        missing_dir = str(tmp_path / "does-not-exist")
        assert mw.write_drop_catalog(_reader(), missing_dir, STAGE_INFO, "1.3.2", inst) == 0

    def test_never_raises(self, monkeypatch, tmp_path):
        inst = {"StageInfoData": STAGES, "ItemInfoData": ITEMS}
        assert mw.write_drop_catalog(_reader(), str(tmp_path), None, "1.3.2", inst) == 0   # bad stage_info
