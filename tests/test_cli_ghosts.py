"""Regression tests for issue #27: no command lists saved ghost patches.

`shadowci --help` never mentioned a command for listing the ghost patches
saved under `.shadowci/ghost/`, so a user had to find a ten-character hex
patch file by hand (in scrollback or a file explorer) and pass the full
path to `apply-ghost`. These tests exercise the `ghosts` command and the
`--latest` flag described in the issue's suggested fix, against a real
temporary git repository with hand-placed ghost patches (mirroring the
on-disk layout `shadowci.pipeline.save_ghost_patch` produces).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from shadowci.cli import main

GHOST_DIRNAME = Path(".shadowci") / "ghost"
SIGNALS_PATH = Path(".shadowci") / "signals.json"

APP_PY = (
    "def add(a, b):\n"
    "    return a + b + 1\n"
    "\n"
    "def subtract(a, b):\n"
    "    return a - b - 1\n"
)

READY_ADD_FIX = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,3 +1,3 @@\n"
    " def add(a, b):\n"
    "-    return a + b + 1\n"
    "+    return a + b\n"
    " \n"
)

READY_SUBTRACT_FIX = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -4,2 +4,2 @@\n"
    " def subtract(a, b):\n"
    "-    return a - b - 1\n"
    "+    return a - b\n"
)

STALE_ADD_FIX = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,3 +1,3 @@\n"
    " def add(a, b):\n"
    "-    return a + b + 999\n"
    "+    return a + b\n"
    " \n"
)


def _write_ghost_patch(
    root: Path,
    digest: str,
    diff: str,
    summary: str,
    files: list[str],
    fingerprint: str | None = None,
    mtime: float | None = None,
) -> Path:
    ghost_dir = root / GHOST_DIRNAME
    ghost_dir.mkdir(parents=True, exist_ok=True)
    patch_path = ghost_dir / f"{digest}.patch"
    patch_path.write_text(diff)
    metadata_path = ghost_dir / f"{digest}.json"
    metadata_path.write_text(
        json.dumps(
            {
                "digest": digest,
                "fingerprint": fingerprint or f"fp-{digest}",
                "summary": summary,
                "files": files,
            }
        )
    )
    if mtime is not None:
        os.utime(patch_path, (mtime, mtime))
        os.utime(metadata_path, (mtime, mtime))
    return patch_path


def _line_mentioning(out: str, needle: str) -> str:
    for line in out.splitlines():
        if needle in line:
            return line
    raise AssertionError(f"no line in output mentions {needle!r}:\n{out}")


def test_ghosts_lists_ready_and_stale_patches(git_repo: Path, capsys):
    (git_repo / "app.py").write_text(APP_PY)
    _write_ghost_patch(
        git_repo, "readyaaa11", READY_ADD_FIX, "fix the off-by-one in add", ["app.py"]
    )
    _write_ghost_patch(
        git_repo, "staleaaa11", STALE_ADD_FIX, "a fix that no longer applies", ["app.py"]
    )

    exit_code = main(["ghosts", str(git_repo)])
    out = capsys.readouterr().out

    assert exit_code == 0

    ready_line = _line_mentioning(out, "readyaaa11")
    assert re.search(r"\bready\b", ready_line, re.IGNORECASE)
    assert not re.search(r"\bstale\b", ready_line, re.IGNORECASE)
    assert "fix the off-by-one in add" in ready_line
    assert "app.py" in ready_line

    stale_line = _line_mentioning(out, "staleaaa11")
    assert re.search(r"\bstale\b", stale_line, re.IGNORECASE)


def test_ghosts_clean_removes_only_stale_patches_and_records_no_signal(
    git_repo: Path, capsys
):
    (git_repo / "app.py").write_text(APP_PY)
    ready_patch = _write_ghost_patch(
        git_repo, "readybbb22", READY_ADD_FIX, "fix the off-by-one in add", ["app.py"]
    )
    stale_patch = _write_ghost_patch(
        git_repo, "stalebbb22", STALE_ADD_FIX, "a fix that no longer applies", ["app.py"]
    )

    exit_code = main(["ghosts", str(git_repo), "--clean"])
    capsys.readouterr()

    assert exit_code == 0
    assert ready_patch.exists()
    assert ready_patch.with_suffix(".json").exists()
    assert not stale_patch.exists()
    assert not stale_patch.with_suffix(".json").exists()
    assert not (git_repo / SIGNALS_PATH).exists()


def test_apply_ghost_latest_applies_the_newest_ready_patch(
    git_repo: Path, capsys, monkeypatch
):
    (git_repo / "app.py").write_text(APP_PY)
    older = _write_ghost_patch(
        git_repo,
        "readyccc01",
        READY_ADD_FIX,
        "fix add",
        ["app.py"],
        fingerprint="fix-add",
        mtime=1_700_000_000,
    )
    newer = _write_ghost_patch(
        git_repo,
        "readyccc02",
        READY_SUBTRACT_FIX,
        "fix subtract",
        ["app.py"],
        fingerprint="fix-subtract",
        mtime=1_700_000_100,
    )

    monkeypatch.chdir(git_repo)
    exit_code = main(["apply-ghost", "--latest"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "applied" in out

    app_contents = (git_repo / "app.py").read_text()
    assert "return a + b + 1" in app_contents  # add fix NOT applied
    assert "return a - b\n" in app_contents  # subtract fix applied
    assert "return a - b - 1" not in app_contents

    assert not newer.exists()  # the applied (newest) patch is removed
    assert older.exists()  # the untouched (older) patch is left alone

    signals_path = git_repo / SIGNALS_PATH
    assert signals_path.exists()
    assert "fix-subtract" in signals_path.read_text()
    assert "fix-add" not in signals_path.read_text()


def test_apply_ghost_latest_reports_clearly_when_none_is_ready(
    git_repo: Path, capsys, monkeypatch
):
    (git_repo / "app.py").write_text(APP_PY)
    stale_patch = _write_ghost_patch(
        git_repo, "staleccc03", STALE_ADD_FIX, "a fix that no longer applies", ["app.py"]
    )

    monkeypatch.chdir(git_repo)
    exit_code = main(["apply-ghost", "--latest"])
    captured = capsys.readouterr()

    assert exit_code == 1
    combined = captured.out + captured.err
    assert re.search(r"\bready\b", combined, re.IGNORECASE)

    # nothing was touched: the unreadied patch is left exactly as it was
    assert stale_patch.exists()
    assert (git_repo / "app.py").read_text() == APP_PY
    assert not (git_repo / SIGNALS_PATH).exists()
