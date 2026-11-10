"""Dev-only `--instance-tag`: lets a second reader run beside the first (live A/B).

Without the flag the single-instance mutex is the historical one (the app relies on it)."""

import os
import sys

import pytest

import meter_windows
from shared import single_instance

BACKSLASH = chr(92)
HISTORICAL = "Local" + BACKSLASH + "TBH_Meter_Reader"


class TestMutexName:
    def test_untagged_keeps_the_historical_name(self):
        assert single_instance.mutex_name() == HISTORICAL
        assert single_instance.mutex_name(None) == HISTORICAL

    def test_tag_is_appended_with_a_dash(self):
        assert single_instance.mutex_name("old") == HISTORICAL + "-old"


class TestTagValidation:
    @pytest.mark.parametrize("tag", ["old", "A", "rev_2", "pre-fix", "x" * 32, "0"])
    def test_accepts_simple_names(self, tag):
        assert single_instance.validate_tag(tag) == tag

    @pytest.mark.parametrize("tag", [
        "", "x" * 33, "has space", "a/b", "a" + BACKSLASH + "b", "dot.dot",
        "caf" + chr(233), "tab" + chr(9), "new" + chr(10),
    ])
    def test_rejects_anything_else(self, tag):
        with pytest.raises(ValueError):
            single_instance.validate_tag(tag)

    def test_mutex_name_refuses_a_bad_tag(self):
        # defence in depth: a tag with a backslash must never reach CreateMutexW
        with pytest.raises(ValueError):
            single_instance.mutex_name("a" + BACKSLASH + "b")


class _Main:
    """Drives meter_windows.main() with every side effect stubbed: nothing attaches to a game,
    no real mutex is taken, nothing is written outside tmp_path."""

    def __init__(self, monkeypatch, capsys):
        self.capsys = capsys
        self.acquired = []   # (args, kwargs) of each acquire_single_instance() call
        self.ran = []        # args of each run() call: (hz, output_dir, debug)
        monkeypatch.setattr(meter_windows, "acquire_single_instance",
                            lambda *a, **k: self.acquired.append((a, k)) or True)
        monkeypatch.setattr(meter_windows, "run", lambda *a: self.ran.append(a))
        monkeypatch.setattr(meter_windows, "tee_stdio", lambda *_: None)
        monkeypatch.setattr(meter_windows, "init_diag_log", lambda *_: None)
        self.monkeypatch = monkeypatch

    def __call__(self, *argv):
        self.monkeypatch.setattr(sys, "argv", ["meter_windows.py", *argv])
        try:
            meter_windows.main()
            code = None
        except SystemExit as e:
            code = e.code
        captured = self.capsys.readouterr()
        return code, captured.out, captured.err


@pytest.fixture
def run_main(monkeypatch, capsys):
    return _Main(monkeypatch, capsys)


class TestMainWithTag:
    def test_tag_with_its_own_output_takes_a_tagged_mutex_and_warns(self, run_main, tmp_path):
        out_dir = str(tmp_path / "ab-old")
        code, out, _ = run_main("--instance-tag", "old", "--output", out_dir)
        assert code is None
        assert run_main.acquired == [(("old",), {})]
        assert run_main.ran and run_main.ran[0][1] == out_dir
        assert "--instance-tag" in out and "dev" in out.lower() and "A/B" in out
        assert "gold" in out   # concurrent readers can perturb the live gold read

    @pytest.mark.parametrize("tag", ["", "has space", "a/b", "x" * 33, "dot.dot"])
    def test_a_bad_tag_is_an_argparse_error_and_nothing_starts(self, run_main, tmp_path, tag):
        code, _, err = run_main("--instance-tag", tag, "--output", str(tmp_path / "o"))
        assert code == 2
        assert "invalid instance tag" in err
        assert run_main.acquired == [] and run_main.ran == []

    def test_tag_without_output_is_refused(self, run_main):
        code, _, err = run_main("--instance-tag", "old")
        assert code == 2
        assert "--output" in err and "--instance-tag" in err
        assert run_main.acquired == [] and run_main.ran == []

    @pytest.mark.parametrize("spell", ["same", "trailing-sep", "dotted"])
    def test_tag_with_the_default_output_is_refused(self, run_main, spell):
        d = meter_windows.DEFAULT_OUTPUT
        out_dir = {"same": d, "trailing-sep": d + "/",
                   "dotted": os.path.join(d, "..", os.path.basename(d))}[spell]
        code, _, err = run_main("--instance-tag", "old", "--output", out_dir)
        assert code == 2
        assert "--output" in err and "default" in err
        assert run_main.acquired == [] and run_main.ran == []


class TestMainWithoutTag:
    def test_untagged_run_is_unchanged(self, run_main, tmp_path):
        out_dir = str(tmp_path / "o")
        code, out, _ = run_main("--output", out_dir)
        assert code is None
        assert run_main.acquired == [((None,), {})]   # => the historical mutex name
        assert run_main.ran[0][1] == out_dir
        assert "--instance-tag" not in out
