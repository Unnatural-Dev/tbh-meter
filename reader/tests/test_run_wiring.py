"""Wiring of RunBoundary into meter_windows.run(): drives meter_windows.run() with a fake Reader + scripted clock and asserts on the
EMITTED raw/*.json record, the live.json lines and the RunBoundary call order at re-attach.
hz=1 => one loop tick == 1 s of fake time."""
import glob
import json
import os
from typing import Any

import pytest

import meter_windows as mw
from game import attach
from metrics import run_boundary as rbm
from tests import test_attach as attachment

# Register the existing memory fixture here; it also restores calibrated offsets.
attachment_world = attachment.world

A, B = 101, 202
STAGE_INFO = {A: (1, 1, 5, 0), B: (1, 2, 5, 0)}   # (act, stage, ?, diff)
SC, SF, MSM, LM, CSD = 0x10, 0x20, 0x3000, 0x4000, 0x5000   # distinct address ranges


class FakeReader:
    """A tiny scripted memory: log list, dead-monster counter; everything else unreadable (None)."""
    def __init__(self, world, dies_at=None):
        self.world, self.dies_at = world, dies_at

    def rptr(self, a):
        w = self.world
        if a == LM + mw.LogManager.LOG_LIST:
            dead = self.dies_at is not None and w.tick >= self.dies_at
            return None if dead else 0x1000
        if a == 0x1000 + mw.List.ITEMS:
            return 0x1100
        if 0x1100 + mw.Array.DATA <= a < 0x1100 + mw.Array.DATA + 8 * 64:
            return 0x9000 + (a - 0x1100 - mw.Array.DATA) // 8 * 0x100     # log entry object pointer
        if a >= 0x9000 and (a - 0x9000) % 0x100 == 0:
            index = (a - 0x9000) // 0x100
            return w.log_classes.get(index, SC)                             # default: StageClearLog
        if a == MSM + mw.MonsterSpawnManager.DEAD_MONSTER_LIST:
            return 0x2000
        return None

    def ri32(self, a):
        w = self.world
        if a == 0x1000 + mw.List.SIZE:
            return w.last_log
        if a == 0x2000 + mw.List.SIZE:
            return w.script.get(w.tick, {}).get("dead")
        if a >= 0x9000 and (a - 0x9000) % 0x100 == mw.StageClearLog.CLEAR_TIME:
            return 5
        return None

    def __getattr__(self, name):          # any other read primitive -> unreadable
        return lambda *a, **k: None


class World:
    def __init__(self):
        self.t = 1000.0
        self.tick = 0
        self.events = []          # ordered log of RunBoundary calls + new_run reads
        self.script: dict[int, dict[str, Any]] = {}  # tick -> key/snap/dead/log reads
        self.stop_after = 10
        self.last_log = 0
        self.log_classes = {}     # log-entry index -> klass; unspecified entries are success
        self.live = {}            # tick -> live.json record
        self.diag = []            # diag() lines (run-close summary incl. partial=)
        self.die_at: int | None = None  # tick at which the FIRST reader starts failing


def _drive(monkeypatch, tmp_path, world, hz=1, *, reader_factory=FakeReader,
           real_prepare=False, **run_kwargs):
    complete = attach.AttachedState(
        sc_class=SC, sf_class=SF, msm=MSM, lm=LM, csd_list=[CSD], psd_list=[0x60],
        stage_info=STAGE_INFO, item_cat={2: (1, 1, 1)}, hero_cat={3: 1}, sm_list=[0x70],
        gold_klass=None, gb_class=None, die_class=None, res_class=None,
        catalog_instances={}, tbase=None, idx_ut=None, csd=CSD, sm=None, stage_waves={})
    monkeypatch.setattr(mw, "find_pid", lambda: 1234)
    monkeypatch.setattr(mw, "open_process", lambda pid: 0xDEAD)
    readers = []

    def make_reader(handle):
        r = reader_factory(world, dies_at=world.die_at if not readers else None)   # only the 1st reader dies
        readers.append(r)
        return r
    monkeypatch.setattr(mw, "Reader", make_reader)
    monkeypatch.setattr(mw, "close", lambda h: None)
    monkeypatch.setattr(mw, "_detect_game_version", lambda h: "1.00.19")
    monkeypatch.setattr(mw.typeinfo, "ga_module", lambda pid: (0x7ff800000000, 0x62ea000))
    monkeypatch.setattr(mw.typeinfo, "build_fingerprint", lambda r, b, version=None: "fp")

    def prepare(r, pid, fp, cache, output_dir, game_version, *, startup):
        if len(readers) > 1:              # a re-attach resolve takes ~90 s of wall clock
            world.t += 90.0
        return complete
    if not real_prepare:
        monkeypatch.setattr(attach, "prepare", prepare)
    monkeypatch.setattr(mw, "describe_reader_version", lambda: "test-ver")
    monkeypatch.setattr(mw, "diag", lambda *a, **k: world.diag.append(a[0] if a else ""))
    monkeypatch.setattr(mw, "_emit_status", lambda *a, **k: None)
    _real_write = mw._write_atomic

    def write_atomic(path, text):
        if os.path.basename(path) == "live.json":
            world.live[world.tick] = json.loads(text)
        return _real_write(path, text)
    monkeypatch.setattr(mw, "_write_atomic", write_atomic)
    monkeypatch.setattr(mw.save, "pick_live_psd", lambda *a, **k: None)
    monkeypatch.setattr(mw.save, "read_heroes", lambda *a, **k: {})

    def read_gold(reader, p):
        world.events.append(("new_run.read_gold", world.t))
        return 0
    monkeypatch.setattr(mw.save, "read_gold", read_gold)
    monkeypatch.setattr(mw, "read_live_stage_key", lambda reader, msm: world.script.get(world.tick, {}).get("key"))
    monkeypatch.setattr(mw, "read_live_monsters", lambda reader, msm: world.script.get(world.tick, {}).get("snap", []))

    class Spy(rbm.RunBoundary):
        def __init__(self, now):
            world.events.append(("init", now))
            super().__init__(now)

        def restart(self, now):
            world.events.append(("restart", now))
            super().restart(now)

        def close(self, now):
            world.events.append(("close", now))
            return super().close(now)
    monkeypatch.setattr(mw, "RunBoundary", Spy)
    monkeypatch.setattr(mw.time, "time", lambda: world.t)

    def sleep(s):
        world.t += s
        world.last_log = world.script.get(world.tick + 1, {}).get("log", world.last_log)   # visible next tick
        world.tick += 1
        if world.tick >= world.stop_after:
            raise KeyboardInterrupt
    monkeypatch.setattr(mw.time, "sleep", sleep)
    mw.run(hz=hz, output_dir=str(tmp_path), debug=False, **run_kwargs)
    return readers


def _prepared_restart(monkeypatch, tmp_path, attachment_world, failure, *, fail_at=2):
    """Reuse prepare's memory fixture, with the existing clock/log script at its edges.

    No replacement of prepare or _resolve_all: failures cross the real exception
    boundary. The shared fixture restores all four process-global calibrated offsets.
    """
    memory, classes, instances, _, _ = attachment_world
    classes["StageClearLog"] = {SC}
    classes["StageFailedLog"] = {SF}
    instances["MonsterSpawnManager"] = [MSM]
    instances["LogManager"] = [LM]
    (tmp_path / "resolve_cache.json").write_text(json.dumps({
        "fmt": attach.CACHE_FMT, "calib": {"fp": attachment.CALIB}}), encoding="utf-8")
    w = World()
    w.die_at = 1
    w.stop_after = 13
    w.script = {t: dict(key=1001) for t in range(13)}
    w.script[6] = dict(key=1001, snap=[_mob(1)])
    w.script[7] = dict(key=1001, snap=[])
    w.script[8] = dict(key=1001, log=1)
    readers = []

    class PreparedReader(attachment.Memory):
        def __init__(self, world, dies_at):
            super().__init__(memory._mem)
            self.scripted = FakeReader(world, dies_at)
            self.generation = len(readers) + 1
            readers.append(self)

        def rptr(self, a):
            if (a in (LM + mw.LogManager.LOG_LIST, 0x1000 + mw.List.ITEMS,
                      MSM + mw.MonsterSpawnManager.DEAD_MONSTER_LIST)
                    or 0x1100 + mw.Array.DATA <= a < 0x1100 + mw.Array.DATA + 8 * 64
                    or 0x9000 <= a < 0xD000):
                return self.scripted.rptr(a)
            return super().rptr(a)

        def ri32(self, a):
            if (failure == "carrier" and self.generation == fail_at
                    and a == attachment.CSD + mw.CommonSaveData.CURRENT_STAGE_KEY):
                raise RuntimeError("carrier read failed")
            if a in (0x1000 + mw.List.SIZE, 0x2000 + mw.List.SIZE) or 0x9000 <= a < 0xD000:
                return self.scripted.ri32(a)
            return super().ri32(a)

    backrefs = attach.instances_of

    def read_instances(reader, *a, **k):
        if failure == "resolution" and reader.generation == fail_at:
            raise RuntimeError("resolution read failed")
        return backrefs(reader, *a, **k)
    monkeypatch.setattr(attach, "instances_of", read_instances)
    return w, PreparedReader


def test_reattach_resolution_read_error_retries_and_resumes_real_output(
        monkeypatch, tmp_path, attachment_world, capsys):
    w, reader = _prepared_restart(monkeypatch, tmp_path, attachment_world, "resolution")
    _drive(monkeypatch, tmp_path, w, reader_factory=reader, real_prepare=True)
    recs = _records(tmp_path)
    assert [r["run_outcome"] for r in recs] == ["success"]
    assert _val(recs[0]["stageKey"]) == 1001
    assert _val(recs[0]["mobs"]) == 1
    assert _val(recs[0]["total_damage"]) == 100
    assert recs[0]["duration"] == 2
    assert w.live[7]["mobs"] == 1 and w.live[7]["damage_now"] == 100
    assert w.live[7]["elapsed"] == 1
    out = capsys.readouterr().out
    assert out.count("[re-attaching] game came back") == 2
    assert out.count("[ok] re-attached") == 1
    assert "mobs 1/" in out


def test_reattach_carrier_read_error_after_resolution_escapes_run(
        monkeypatch, tmp_path, attachment_world, capsys):
    w, reader = _prepared_restart(monkeypatch, tmp_path, attachment_world, "carrier")
    with pytest.raises(RuntimeError, match="carrier read failed"):
        _drive(monkeypatch, tmp_path, w, reader_factory=reader, real_prepare=True)
    assert _records(tmp_path) == []
    out = capsys.readouterr().out
    assert out.count("[re-attaching] game came back") == 1
    assert "[ok] re-attached" not in out


def test_startup_resolution_read_error_escapes_run(monkeypatch, tmp_path, attachment_world):
    w, reader = _prepared_restart(monkeypatch, tmp_path, attachment_world, "resolution", fail_at=1)
    with pytest.raises(RuntimeError, match="resolution read failed"):
        _drive(monkeypatch, tmp_path, w, reader_factory=reader, real_prepare=True)
    assert _records(tmp_path) == []
    assert w.live == {}


def _records(tmp_path):
    return [json.load(open(p, encoding="utf-8")) for p in sorted(glob.glob(str(tmp_path / "raw" / "*.json")))]


def _mob(addr, hp=100.0):
    return (addr, hp, 100.0)


def _val(env):
    return env["value"] if isinstance(env, dict) else env


def test_abandoned_record_carries_ledger_mobs_damage_duration_and_old_stage(monkeypatch, tmp_path):
    """stage A adopted, 3 mobs die (one takes 40 dmg first), stage switches to B => 'abandoned' record on A."""
    w = World()
    w.stop_after = 8
    w.script = {
        0: dict(key=A, snap=[_mob(1), _mob(2), _mob(3)]),
        1: dict(key=A, snap=[_mob(1), _mob(2), _mob(3, 60.0)]),
        2: dict(key=A, snap=[_mob(3, 60.0)]),
        3: dict(key=A, snap=[]),
        4: dict(key=A), 5: dict(key=A),
        6: dict(key=B), 7: dict(key=B),
    }
    _drive(monkeypatch, tmp_path, w)
    recs = _records(tmp_path)
    assert len(recs) == 1, w.events
    r = recs[0]
    assert r["run_outcome"] == "abandoned"
    assert _val(r["mobs"]) == 3
    assert _val(r["total_damage"]) == pytest.approx(40 + 100 + 100 + 60)   # drop 40 + finishing blows 100,100,60
    assert _val(r["stageKey"]) == A                       # closed with the OLD stage
    assert r["duration"] == pytest.approx(6.0, abs=0.01)                   # t=1000 -> 1006


def test_success_close_emits_ledger_and_live_line_shows_pre_close_values(monkeypatch, tmp_path, capsys):
    w = World()
    w.stop_after = 10
    w.script = {
        0: dict(key=A, snap=[_mob(1), _mob(2)]),
        1: dict(key=A, snap=[_mob(1, 50.0), _mob(2)]),
        2: dict(key=A, snap=[_mob(2)]),
        3: dict(key=A, snap=[]),
        4: dict(key=A), 5: dict(key=A),
        6: dict(key=A, log=1),
    }
    _drive(monkeypatch, tmp_path, w)
    out = capsys.readouterr().out
    recs = _records(tmp_path)
    assert [r["run_outcome"] for r in recs] == ["success"], w.events
    r = recs[0]
    assert _val(r["mobs"]) == 2
    expected_damage = 50 + 50 + 100                  # drop 50, finishing blows 50 and 100
    assert _val(r["total_damage"]) == pytest.approx(expected_damage)
    assert "mobs 2/" in out                          # console summary
    assert any("partial=False" in d for d in w.diag), w.diag      # _is_partial fed the closed damage
    assert w.live[5]["mobs"] == 2 and w.live[5]["elapsed"] == 5 and w.live[5]["damage_now"] == expected_damage
    # on the close tick the live line still shows the run that just ended: damage captured BEFORE the close
    assert w.live[6]["damage_now"] == expected_damage, w.live[6]
    assert w.live[6]["mobs"] == 0 and w.live[7]["elapsed"] <= 1       # new ledger afterwards


def test_reload_drop_in_dead_count_abandons_same_stage(monkeypatch, tmp_path):
    w = World()
    w.stop_after = 9
    w.script = {t: dict(key=A, snap=[_mob(1)], dead=50) for t in range(9)}
    w.script[6] = dict(key=A, snap=[_mob(1)], dead=10)        # manual restart: counter drops by > 2, past the grace
    _drive(monkeypatch, tmp_path, w)
    assert [r["run_outcome"] for r in _records(tmp_path)] == ["abandoned"]


@pytest.mark.parametrize("terminal_class,outcome", [(SC, "success"), (SF, "fail")])
@pytest.mark.parametrize("next_dead", [None, 10, 50])
def test_terminal_reload_then_missing_stable_or_increasing_count_keeps_next_ledger(
        monkeypatch, tmp_path, terminal_class, outcome, next_dead):
    """A terminal log wins over a coincident reload; each next observation clears it.

    Keeping the prior reload flag on missing/non-decreasing reads would emit an
    extra abandon after the grace and destroy the second run's ledger.
    """
    w = World()
    w.stop_after = 14
    w.log_classes = {0: terminal_class}
    w.script = {t: dict(key=A, dead=50) for t in range(6)}
    w.script[0]["snap"] = [_mob(1), _mob(2)]
    w.script[6] = dict(key=A, dead=10, log=1)
    w.script.update({t: dict(key=A, dead=next_dead) for t in range(7, 14)})
    w.script[7]["snap"] = [_mob(3)]
    _drive(monkeypatch, tmp_path, w)
    recs = _records(tmp_path)
    assert [r["run_outcome"] for r in recs] == [outcome]
    assert recs[0]["stageKey"]["ok"] and _val(recs[0]["stageKey"]) == A
    assert recs[0]["mobs"]["ok"] and _val(recs[0]["mobs"]) == 2
    assert _val(recs[0]["total_damage"]) == 200
    assert recs[0]["duration"] == 6
    assert w.live[6]["elapsed"] == 0 and w.live[6]["damage_now"] == 200
    assert w.live[13]["elapsed"] == 7
    assert w.live[13]["mobs"] == 1 and w.live[13]["damage_now"] == 100


@pytest.mark.parametrize("terminal_class,outcome", [(SC, "success"), (SF, "fail")])
def test_terminal_reload_retains_baseline_for_fresh_post_grace_drop(
        monkeypatch, tmp_path, terminal_class, outcome):
    """Clearing the cumulative baseline on close/None would miss the later reload."""
    w = World()
    w.stop_after = 15
    w.log_classes = {0: terminal_class}
    w.script = {t: dict(key=A, dead=50) for t in range(6)}
    w.script[0]["snap"] = [_mob(1), _mob(2)]
    w.script[6] = dict(key=A, dead=10, log=1)
    w.script.update({t: dict(key=A, dead=None) for t in range(7, 11)})
    w.script[7]["snap"] = [_mob(3)]
    w.script.update({t: dict(key=A, dead=0) for t in range(11, 15)})
    _drive(monkeypatch, tmp_path, w)
    recs = _records(tmp_path)
    assert [r["run_outcome"] for r in recs] == [outcome, "abandoned"]
    assert [_val(r["stageKey"]) for r in recs] == [A, A]
    assert [_val(r["mobs"]) for r in recs] == [2, 1]
    assert [_val(r["total_damage"]) for r in recs] == [200, 100]
    assert [r["duration"] for r in recs] == [6, 5]
    assert w.live[10]["elapsed"] == 4 and w.live[10]["mobs"] == 1
    assert w.live[11]["elapsed"] == 0 and w.live[11]["mobs"] == 0
    assert w.live[14]["elapsed"] == 3


@pytest.mark.parametrize("classes,outcomes", [
    ((SC, SF), ["success", "fail"]),
    ((SF, SC), ["fail", "success"]),
])
def test_repeated_terminal_logs_after_reload_close_separate_ledgers_once(
        monkeypatch, tmp_path, classes, outcomes):
    """The real cursor must not replay retained terminals or carry old kills."""
    w = World()
    w.stop_after = 17
    w.log_classes = dict(enumerate(classes))
    w.script = {t: dict(key=A, dead=50 if t < 6 else 10) for t in range(17)}
    w.script[0]["snap"] = [_mob(1), _mob(2)]
    w.script[6]["log"] = 1
    w.script[7]["snap"] = [_mob(3)]
    w.script[12]["log"] = 2
    _drive(monkeypatch, tmp_path, w)
    recs = _records(tmp_path)
    assert [r["run_outcome"] for r in recs] == outcomes
    assert [_val(r["stageKey"]) for r in recs] == [A, A]
    assert [_val(r["mobs"]) for r in recs] == [2, 1]
    assert [_val(r["total_damage"]) for r in recs] == [200, 100]
    assert [r["duration"] for r in recs] == [6, 6]
    assert w.live[16]["elapsed"] == 4 and w.live[16]["mobs"] == 0


def test_reattach_after_terminal_reload_discards_old_counter_and_keeps_new_close(
        monkeypatch, tmp_path):
    """New attachment skips the old terminal and owns a fresh stage/ledger baseline."""
    w = World()
    w.die_at = 8
    w.stop_after = 22
    w.script = {t: dict(key=A, dead=50 if t < 6 else 10) for t in range(12)}
    w.script[0]["snap"] = [_mob(1), _mob(2)]
    w.script[6]["log"] = 1
    w.script.update({t: dict(key=B, dead=None if t < 17 else 1) for t in range(12, 22)})
    w.script[13]["snap"] = [_mob(3)]
    w.script[18]["log"] = 2
    readers = _drive(monkeypatch, tmp_path, w)
    assert len(readers) == 2
    recs = _records(tmp_path)
    assert [r["run_outcome"] for r in recs] == ["success", "success"]
    assert [_val(r["stageKey"]) for r in recs] == [A, B]
    assert [_val(r["mobs"]) for r in recs] == [2, 1]
    assert [_val(r["total_damage"]) for r in recs] == [200, 100]
    assert [r["duration"] for r in recs] == [6, 6]
    assert w.live[17]["elapsed"] == 5 and w.live[17]["mobs"] == 1


def test_uncatalogued_live_key_is_not_adopted(monkeypatch, tmp_path):
    w = World()
    w.stop_after = 10
    w.script = {t: dict(key=999) for t in range(5)}            # not in stage_info for longer than the grace
    w.script.update({t: dict(key=A) for t in range(5, 10)})
    _drive(monkeypatch, tmp_path, w)
    assert _records(tmp_path) == []                            # no bogus adoption => no bogus abandon


def test_switch_to_uncatalogued_key_does_not_abandon(monkeypatch, tmp_path):
    w = World()
    w.stop_after = 10
    w.script = {t: dict(key=A) for t in range(5)}
    w.script.update({t: dict(key=999) for t in range(5, 10)})
    _drive(monkeypatch, tmp_path, w)
    assert _records(tmp_path) == []
def test_reattach_stamps_start_after_resolve_and_before_new_run_reads(monkeypatch, tmp_path):
    """At re-attach rb.restart(now) runs AFTER resolve (fresh clock) and BEFORE new_run()'s reads."""
    w = World()
    w.die_at = 1
    w.stop_after = 8
    _drive(monkeypatch, tmp_path, w)
    kinds = [e[0] for e in w.events]
    assert kinds[0] == "init", w.events                                    # stamped before start-up new_run
    assert "restart" in kinds, w.events
    i = kinds.index("restart")
    assert kinds[i - 1] == "new_run.read_gold" and i == 2, w.events        # exactly one start-up new_run read before it
    assert kinds[i + 1] == "new_run.read_gold", w.events                   # re-attach new_run right after
    assert w.events[i][1] == 1005.0 + 90.0, w.events                       # re-attach tick + 90 s resolve


def test_reattach_does_not_leak_interrupted_run_into_next_run(monkeypatch, tmp_path):
    """Interrupted run on A had kills + an adopted stage; after re-attach into stage B nothing may be emitted
    and the next run starts clean."""
    w = World()
    w.die_at = 3
    w.stop_after = 12
    w.script = {0: dict(key=A, snap=[_mob(1), _mob(2), _mob(3)]),
                1: dict(key=A, snap=[_mob(3)]), 2: dict(key=A, snap=[])}
    w.script.update({t: dict(key=A, snap=[]) for t in range(3, 8)})   # still A while the reads fail (ticks 3-7)
    w.script.update({t: dict(key=B, snap=[]) for t in range(8, 12)})   # game is back, now on stage B
    _drive(monkeypatch, tmp_path, w)
    assert _records(tmp_path) == [], _records(tmp_path)
    assert w.live[11]["mobs"] == 0 and w.live[11]["elapsed"] < 10, w.live[11]


def test_reattach_with_lower_dead_count_is_not_a_reload(monkeypatch, tmp_path):
    w = World()
    w.die_at = 3
    w.stop_after = 12
    w.script = {t: dict(key=A, dead=50) for t in range(8)}              # reads fail on ticks 3-7 -> re-attach at tick 7
    w.script.update({t: dict(key=A, dead=10) for t in range(8, 12)})    # new instance's counter is lower
    _drive(monkeypatch, tmp_path, w)
    assert _records(tmp_path) == []

def _overcount_world():
    """Stage A holds 5 + 1 = 6 mobs; 7 die, then the clear log lands: a success with mobs > total_mobs."""
    w = World()
    w.stop_after = 8
    w.script = {
        0: dict(key=A, snap=[_mob(i) for i in range(1, 8)], dead=0),
        1: dict(key=A, snap=[_mob(i, 50.0) for i in range(1, 8)], dead=0),
        2: dict(snap=[], dead=7),                  # no live key this tick (between waves): recorded as read, None
        3: dict(key=A, snap=[], dead=7, log=1),
        4: dict(key=A, snap=[], dead=7), 5: dict(key=A, snap=[], dead=7),
    }
    return w


def test_anomaly_dump_flag_writes_the_ticks_of_an_overcounted_success(monkeypatch, tmp_path):
    import gzip
    w = _overcount_world()
    _drive(monkeypatch, tmp_path, w, anomaly_dump=True)
    recs = _records(tmp_path)
    assert [r["run_outcome"] for r in recs] == ["success"], w.events
    assert _val(recs[0]["mobs"]) == 7 and _val(recs[0]["total_mobs"]) == 6
    rid = recs[0]["id"]
    ring = tmp_path / "anomalies" / f"{rid}.ticks.jsonl.gz"
    meta = json.loads((tmp_path / "anomalies" / f"{rid}.json").read_text(encoding="utf-8"))
    assert meta["run_id"] == rid and meta["reason"] == "mobs>total_mobs" and meta["stage_key"] == A
    assert meta["mobs"] == 7 and meta["total_mobs"] == 6 and meta["hz"] == 1 and meta["reader_version"] == "test-ver"
    with gzip.open(ring, "rt", encoding="utf-8") as f:
        ticks = [json.loads(line) for line in f]
    assert [t["t"] for t in ticks] == [1000.0, 1001.0, 1002.0, 1003.0]       # up to and including the close tick
    assert ticks[0]["monsters"] == [[i, 100.0, 100.0] for i in range(1, 8)]
    assert ticks[0]["stage_key"] == A and ticks[2]["dead"] == 7 and ticks[2]["monsters"] == []
    assert ticks[2]["stage_key"] is None            # the raw live read, not the carried-over current key
    assert [t["closed"] for t in ticks] == [[], [], [], ["StageClearLog"]]


def test_without_the_flag_nothing_is_written_and_records_are_unchanged(monkeypatch, tmp_path):
    on_dir, off_dir = tmp_path / "on", tmp_path / "off"
    on_dir.mkdir(), off_dir.mkdir()
    _drive(monkeypatch, on_dir, _overcount_world(), anomaly_dump=True)
    _drive(monkeypatch, off_dir, _overcount_world())
    assert not (off_dir / "anomalies").exists()
    assert (on_dir / "anomalies").is_dir()
    on_raw = [p.read_bytes() for p in sorted((on_dir / "raw").glob("*.json"))]
    off_raw = [p.read_bytes() for p in sorted((off_dir / "raw").glob("*.json"))]
    assert on_raw == off_raw and len(off_raw) == 1                           # the flag never touches the record


def test_flag_on_but_ordinary_run_leaves_no_anomalies_dir(monkeypatch, tmp_path):
    w = World()
    w.stop_after = 8
    w.script = {0: dict(key=A, snap=[_mob(1), _mob(2)]), 1: dict(key=A, snap=[]),
                2: dict(key=A), 3: dict(key=A, log=1)}
    w.script.update({t: dict(key=A) for t in range(4, 8)})
    _drive(monkeypatch, tmp_path, w, anomaly_dump=True)
    assert [r["run_outcome"] for r in _records(tmp_path)] == ["success"]
    assert not (tmp_path / "anomalies").exists()

def test_incomplete_startup_attachment_reverts_splash_to_searching(monkeypatch, tmp_path, capsys):
    """Incomplete startup preparation must leave the app splash on the deadline-protected
    `searching` marker, never stranded on `resolving`/`scanning`, and never reach `ready`."""
    monkeypatch.setattr(mw, "find_pid", lambda: 1234)
    monkeypatch.setattr(mw, "open_process", lambda pid: 0xDEAD)
    monkeypatch.setattr(mw, "Reader", lambda handle: object())
    monkeypatch.setattr(mw, "_detect_game_version", lambda handle: "1.00.19")
    monkeypatch.setattr(mw.typeinfo, "ga_module", lambda pid: (0x7ff800000000, 0x62ea000))
    monkeypatch.setattr(mw.typeinfo, "build_fingerprint", lambda r, b, version=None: "fp")
    monkeypatch.setattr(attach, "prepare", lambda *a, **k: None)
    mw.run(hz=10, output_dir=str(tmp_path), debug=False)
    markers = [ln.split("[[STATUS]]")[1].strip().split()[0]
               for ln in capsys.readouterr().out.splitlines() if "[[STATUS]]" in ln]
    assert "ready" not in markers
    assert markers[-1] == "searching", markers
