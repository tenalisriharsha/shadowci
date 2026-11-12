"""CLI coverage for issue #40's `shadowci pause` / `shadowci resume` and
their effect on `shadowci status`. None of the `pause`/`resume` commands
had any test coverage before this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shadowci import pause as pause_module
from shadowci.cli import main


def test_pause_with_no_duration_creates_an_indefinite_pause(git_repo: Path, capsys):
    exit_code = main(["pause", str(git_repo)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "paused" in out
    assert "shadowci resume" in out

    state = pause_module.current_pause(git_repo)
    assert state is not None
    assert state.until is None


def test_pause_with_for_creates_a_timed_pause(git_repo: Path, capsys):
    exit_code = main(["pause", str(git_repo), "--for", "30m"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "30m" in out

    state = pause_module.current_pause(git_repo)
    assert state is not None
    assert state.until is not None


@pytest.mark.parametrize("bad_duration", ["bogus", "30", "-5m", "30 minutes"])
def test_pause_with_an_invalid_for_value_is_rejected(git_repo: Path, capsys, bad_duration):
    # Python 3.10-3.12 argparse itself rejects a value that looks like a flag (``-5m``) and exits
    # with status 2; newer versions pass it through and shadowci's own validation returns 2.
    # Either way the user sees an error and exit status 2, and nothing is written.
    try:
        exit_code = main(["pause", str(git_repo), "--for", bad_duration])
    except SystemExit as exc:
        exit_code = exc.code
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "error" in captured.err.lower()
    # Nothing was written on a rejected argument.
    assert pause_module.current_pause(git_repo) is None


def test_resume_ends_an_active_pause(git_repo: Path, capsys):
    main(["pause", str(git_repo)])
    capsys.readouterr()

    exit_code = main(["resume", str(git_repo)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "resumed" in out.lower()
    assert pause_module.current_pause(git_repo) is None


def test_resume_when_not_paused_reports_so_without_failing(git_repo: Path, capsys):
    exit_code = main(["resume", str(git_repo)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "not paused" in out.lower()


def test_status_reports_not_paused_by_default(git_repo: Path, capsys):
    main(["status", str(git_repo)])
    out = capsys.readouterr().out

    line = next(line for line in out.splitlines() if line.startswith("pause"))
    assert "not paused" in line


def test_status_reports_an_active_indefinite_pause(git_repo: Path, capsys):
    main(["pause", str(git_repo)])
    capsys.readouterr()

    main(["status", str(git_repo)])
    out = capsys.readouterr().out

    line = next(line for line in out.splitlines() if line.startswith("pause"))
    assert "paused indefinitely" in line
    assert "shadowci resume" in line


def test_status_reports_an_active_timed_pause_with_remaining_time(git_repo: Path, capsys):
    main(["pause", str(git_repo), "--for", "1h"])
    capsys.readouterr()

    main(["status", str(git_repo)])
    out = capsys.readouterr().out

    line = next(line for line in out.splitlines() if line.startswith("pause"))
    assert "remaining" in line


def test_status_reports_not_paused_again_after_resume(git_repo: Path, capsys):
    main(["pause", str(git_repo)])
    capsys.readouterr()
    main(["resume", str(git_repo)])
    capsys.readouterr()

    main(["status", str(git_repo)])
    out = capsys.readouterr().out

    line = next(line for line in out.splitlines() if line.startswith("pause"))
    assert "not paused" in line
