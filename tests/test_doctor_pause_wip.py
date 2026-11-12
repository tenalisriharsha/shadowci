"""`shadowci doctor` coverage for issue #40 (acceptance test 5): doctor
reports an active pause and warns on a committed `# shadowci: wip`
marker, since that marker can be committed by accident. Neither check
had any test coverage before this file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from shadowci import pause as pause_module
from shadowci.cli import main


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_doctor_reports_not_paused_by_default(git_repo: Path, capsys):
    main(["init", str(git_repo)])
    capsys.readouterr()

    main(["doctor", str(git_repo)])
    combined = capsys.readouterr().out

    pause_line = next(line for line in combined.splitlines() if "pause:" in line)
    assert "OK" in pause_line
    assert "not paused" in pause_line


def test_doctor_reports_an_active_indefinite_pause(git_repo: Path, capsys):
    main(["init", str(git_repo)])
    capsys.readouterr()
    pause_module.pause(git_repo)

    main(["doctor", str(git_repo)])
    combined = capsys.readouterr().out

    pause_line = next(line for line in combined.splitlines() if "pause:" in line)
    assert "WARN" in pause_line
    assert "no fix will be generated" in pause_line
    assert "shadowci resume" in combined


def test_doctor_reports_an_active_timed_pause_with_remaining_seconds(git_repo: Path, capsys):
    main(["init", str(git_repo)])
    capsys.readouterr()
    pause_module.pause(git_repo, duration_seconds=1800.0)

    main(["doctor", str(git_repo)])
    combined = capsys.readouterr().out

    pause_line = next(line for line in combined.splitlines() if "pause:" in line)
    assert "WARN" in pause_line
    assert "remaining" in pause_line


def test_doctor_warns_on_a_committed_wip_marker(git_repo: Path, capsys):
    main(["init", str(git_repo)])
    capsys.readouterr()

    (git_repo / "app.py").write_text(
        "# shadowci: wip\ndef add(a, b):\n    return a + b\n"
    )
    _git(["add", "app.py"], cwd=git_repo)
    _git(["commit", "-q", "-m", "mid-feature edit"], cwd=git_repo)

    main(["doctor", str(git_repo)])
    combined = capsys.readouterr().out

    wip_line = next(line for line in combined.splitlines() if "wip marker:" in line)
    assert "WARN" in wip_line
    assert "app.py" in wip_line
    assert "Try:" in combined


def test_doctor_warns_on_a_staged_but_uncommitted_wip_marker(git_repo: Path, capsys):
    """Staged is still "git-tracked" — the marker can be committed from
    the index without another edit, so the warning should fire before the
    commit actually happens, not only after."""
    main(["init", str(git_repo)])
    capsys.readouterr()

    (git_repo / "app.py").write_text(
        "# shadowci: wip\ndef add(a, b):\n    return a + b\n"
    )
    _git(["add", "app.py"], cwd=git_repo)

    main(["doctor", str(git_repo)])
    combined = capsys.readouterr().out

    wip_line = next(line for line in combined.splitlines() if "wip marker:" in line)
    assert "WARN" in wip_line
    assert "app.py" in wip_line


def test_doctor_does_not_warn_for_a_wip_marker_in_an_untracked_file(git_repo: Path, capsys):
    main(["init", str(git_repo)])
    capsys.readouterr()

    (git_repo / "scratch.py").write_text("# shadowci: wip\nX = 1\n")

    main(["doctor", str(git_repo)])
    combined = capsys.readouterr().out

    wip_line = next(line for line in combined.splitlines() if "wip marker:" in line)
    assert "OK" in wip_line
    assert "no git-tracked file" in wip_line


def test_doctor_does_not_warn_when_no_file_has_the_marker(git_repo: Path, capsys):
    main(["init", str(git_repo)])
    capsys.readouterr()

    main(["doctor", str(git_repo)])
    combined = capsys.readouterr().out

    wip_line = next(line for line in combined.splitlines() if "wip marker:" in line)
    assert "OK" in wip_line
