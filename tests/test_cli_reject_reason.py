"""Regression tests for issue #40: `reject-ghost --reason intentional-change`
(also `wrong-fix`, `not-now`) records the reason instead of counting as an
ordinary bad-fix reject, and is surfaced separately by `shadowci signals`.
"""

from __future__ import annotations

from shadowci.cli import main


def _write_patch_with_metadata(git_repo, fingerprint: str = "pytest-targeted"):
    patch_file = git_repo / "fix.patch"
    patch_file.write_text(
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a + b\n"
        "+    return a + b  # fixed\n"
    )
    patch_file.with_suffix(".json").write_text(
        f'{{"digest": "abc123", "fingerprint": "{fingerprint}", "summary": "test fix"}}'
    )
    return patch_file


def test_reject_ghost_with_intentional_change_reason_records_it(git_repo, capsys):
    patch_file = _write_patch_with_metadata(git_repo)

    exit_code = main(
        ["reject-ghost", str(patch_file), str(git_repo), "--reason", "intentional-change"]
    )
    capsys.readouterr()

    assert exit_code == 0
    signals_path = git_repo / ".shadowci" / "signals.json"
    assert signals_path.exists()
    assert '"reason": "intentional-change"' in signals_path.read_text()


def test_signals_shows_intentional_change_separately_from_rejects(git_repo, capsys):
    main(["apply-ghost", str(_write_patch_with_metadata(git_repo)), str(git_repo)])
    capsys.readouterr()
    main(["apply-ghost", str(_write_patch_with_metadata(git_repo)), str(git_repo)])
    capsys.readouterr()

    main(
        [
            "reject-ghost",
            str(_write_patch_with_metadata(git_repo)),
            str(git_repo),
            "--reason",
            "intentional-change",
        ]
    )
    capsys.readouterr()

    exit_code = main(["signals", str(git_repo)])
    out = capsys.readouterr().out

    line = next(line for line in out.splitlines() if "pytest-targeted" in line)
    assert "2 accept(s)" in line
    assert "0 reject(s)" in line
    assert "1 intentional-change" in line


def test_reject_ghost_rejects_an_unknown_reason(git_repo, capsys):
    patch_file = _write_patch_with_metadata(git_repo)

    try:
        main(["reject-ghost", str(patch_file), str(git_repo), "--reason", "bogus"])
        raised = False
    except SystemExit as exc:
        raised = True
        assert exc.code == 2

    assert raised, "an unknown --reason value must be rejected, not silently accepted"
    assert patch_file.exists()  # nothing was discarded on a rejected argument
