"""metrics/anomaly_dump.py: the opt-in (--anomaly-dump) ring of the last ~6 min of ticks and the rules that
decide when a closed run leaves it on disk. Pure module: no memory reads, no meter_windows import."""
import glob
import gzip
import json
import os

from metrics import anomaly_dump
from metrics.anomaly_dump import AnomalyDump


def _ticks(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_ring_keeps_only_the_last_six_minutes_of_ticks_at_the_configured_hz(tmp_path):
    ad = AnomalyDump(hz=2, out_dir=str(tmp_path))
    for i in range(1000):
        ad.observe_tick(1000.0 + i * 0.5, [(i, 10.0, 20.0)], 7, i)
    ticks = ad.ticks()
    assert len(ticks) == 720                                   # ceil(360 * 2)
    assert ticks[0]["dead"] == 280 and ticks[-1]["dead"] == 999   # oldest evicted first


def test_tick_stores_what_the_loop_already_read_as_plain_json_values(tmp_path):
    ad = AnomalyDump(hz=1, out_dir=str(tmp_path))
    ad.observe_tick(5.0, [(0x10, 40.5, 100.0), (0x20, 100.0, 100.0)], 101, 9)   # valid snapshot
    ad.observe_tick(6.0, None, None, None)                                      # invalid snapshot
    ad.observe_tick(7.0, [], 101, 9)                                            # between waves: empty
    a, b, c = ad.ticks()
    assert a == {"t": 5.0, "monsters": [[16, 40.5, 100.0], [32, 100.0, 100.0]], "stage_key": 101,
                 "dead": 9, "closed": []}
    assert b["monsters"] is None and b["stage_key"] is None and b["dead"] is None
    assert c["monsters"] == []
    json.dumps(ad.ticks())                                     # serialisable as-is


def _close(ad, run_id=111, outcome="success", mobs=10, total_mobs=8, duration=20.0, clear_time=19, stage_key=101):
    return ad.on_run_close(run_id=run_id, outcome=outcome, mobs=mobs, total_mobs=total_mobs,
                           duration=duration, clear_time=clear_time, stage_key=stage_key)


def test_overcounted_success_writes_ring_and_summary_and_keeps_rolling(tmp_path):
    ad = AnomalyDump(hz=1, out_dir=str(tmp_path), reader_version="abc1234")
    for i in range(5):
        ad.observe_tick(100.0 + i, [(1, 5.0, 10.0)], 101, i)
    ad.note_closed("StageClearLog")                            # the close happened on the last fed tick
    path = _close(ad, run_id=111)
    assert path == os.path.join(str(tmp_path), "anomalies", "111.ticks.jsonl.gz")
    ticks = _ticks(path)
    assert [t["dead"] for t in ticks] == [0, 1, 2, 3, 4]
    assert ticks[-1]["closed"] == ["StageClearLog"] and ticks[0]["closed"] == []
    with open(os.path.join(str(tmp_path), "anomalies", "111.json"), encoding="utf-8") as f:
        meta = json.load(f)
    assert meta == {"run_id": 111, "outcome": "success", "mobs": 10, "total_mobs": 8, "duration": 20.0,
                    "clear_time": 19, "stage_key": 101, "reason": "mobs>total_mobs",
                    "reader_version": "abc1234", "hz": 1, "ticks": 5}
    ad.observe_tick(105.0, [], 101, 5)
    assert len(ad.ticks()) == 6                                # the ring was not cleared by the dump


def test_duration_far_past_clear_time_triggers_even_when_mobs_are_fine(tmp_path):
    ad = AnomalyDump(hz=1, out_dir=str(tmp_path))
    ad.observe_tick(1.0, [], 101, 0)
    assert _close(ad, mobs=8, total_mobs=8, duration=30.0, clear_time=24) is not None   # 6 s over
    meta = json.load(open(glob.glob(str(tmp_path / "anomalies" / "*.json"))[0], encoding="utf-8"))
    assert meta["reason"] == "duration-clear_time>5"


def test_ordinary_runs_leave_nothing(tmp_path):
    ad = AnomalyDump(hz=1, out_dir=str(tmp_path))
    ad.observe_tick(1.0, [], 101, 0)
    assert _close(ad, mobs=8, total_mobs=8, duration=29.0, clear_time=24) is None    # exactly 5 s over: not > 5
    assert _close(ad, mobs=7, total_mobs=8, duration=24.0, clear_time=24) is None
    assert not (tmp_path / "anomalies").exists()


def test_unknown_values_never_trigger(tmp_path):
    ad = AnomalyDump(hz=1, out_dir=str(tmp_path))
    ad.observe_tick(1.0, [], 101, 0)
    assert _close(ad, mobs=99, total_mobs=None, duration=None, clear_time=0) is None   # total + duration unknown
    assert _close(ad, mobs=None, total_mobs=8, duration=99.0, clear_time=None) is None
    assert _close(ad, mobs=5, total_mobs=8, duration=99.0, clear_time=0) is None       # 0 = unreadable clear time
    assert not (tmp_path / "anomalies").exists()


def test_dumps_are_capped_per_process(tmp_path):
    ad = AnomalyDump(hz=1, out_dir=str(tmp_path), max_dumps=3)
    ad.observe_tick(1.0, [], 101, 0)
    written = [_close(ad, run_id=i) for i in range(10)]
    assert [w is not None for w in written] == [True] * 3 + [False] * 7
    assert len(glob.glob(str(tmp_path / "anomalies" / "*.json"))) == 3
    assert anomaly_dump.MAX_DUMPS == 20                        # the default cap


def test_io_errors_are_logged_and_swallowed(tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(anomaly_dump, "diag", logged.append)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")                                    # <out>/anomalies cannot be created under a file
    ad = AnomalyDump(hz=1, out_dir=str(blocker))
    ad.observe_tick(1.0, [], 101, 0)
    assert _close(ad) is None                                  # no raise
    assert len(logged) == 1 and "anomaly" in logged[0] and "111" in logged[0]


def test_feeding_garbage_never_raises(tmp_path):
    ad = AnomalyDump(hz=1, out_dir=str(tmp_path))
    ad.note_closed("StageClearLog")                            # nothing fed yet
    ad.observe_tick(1.0, [7, "x"], 101, 0)                     # not (addr, hp, hp_max) triples
    assert ad.ticks()[-1]["monsters"] is None


def test_only_successes_are_judged(tmp_path):
    ad = AnomalyDump(hz=1, out_dir=str(tmp_path))
    ad.observe_tick(1.0, [], 101, 0)
    assert _close(ad, outcome="fail", mobs=50, total_mobs=8, duration=90.0, clear_time=0) is None
    assert _close(ad, outcome="abandoned", mobs=50, total_mobs=8, duration=90.0, clear_time=0) is None
    assert not (tmp_path / "anomalies").exists()


def test_reader_version_is_a_best_effort_string_never_an_error(monkeypatch):
    import subprocess

    class Done:
        returncode, stdout = 0, "c42a998-dirty\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
    assert anomaly_dump.describe_reader_version() == "c42a998-dirty"

    def boom(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(subprocess, "run", boom)
    assert anomaly_dump.describe_reader_version() is None      # frozen build / no git: simply unknown


class TestCliFlag:
    """`--anomaly-dump` reaches run() only when given; the app never passes it."""

    def _main(self, monkeypatch, tmp_path, *argv):
        import sys

        import meter_windows
        ran = []
        monkeypatch.setattr(meter_windows, "acquire_single_instance", lambda *a, **k: True)
        monkeypatch.setattr(meter_windows, "run", lambda *a, **k: ran.append((a, k)))
        monkeypatch.setattr(meter_windows, "tee_stdio", lambda *_: None)
        monkeypatch.setattr(meter_windows, "init_diag_log", lambda *_: None)
        monkeypatch.setattr(sys, "argv", ["meter_windows.py", "--output", str(tmp_path / "o"), *argv])
        meter_windows.main()
        return ran

    def test_default_is_off(self, monkeypatch, tmp_path):
        (args, kwargs), = self._main(monkeypatch, tmp_path)
        assert not (args[3] if len(args) > 3 else kwargs.get("anomaly_dump", False))

    def test_flag_turns_it_on(self, monkeypatch, tmp_path):
        (args, kwargs), = self._main(monkeypatch, tmp_path, "--anomaly-dump")
        assert (args[3] if len(args) > 3 else kwargs.get("anomaly_dump")) is True
