"""Unit tests for `shadowci.pause` (issue #40): pause/resume state and the
`# shadowci: wip` marker, independent of the CLI or pipeline wiring that
uses them. None of this had any test coverage before this file.
"""

from __future__ import annotations

import pytest

from shadowci import pause as pause_module
from shadowci.pause import (
    current_pause,
    file_has_wip_marker,
    parse_duration,
    pause,
    resume,
)


@pytest.mark.parametrize(
    "text,expected_seconds",
    [
        ("30m", 1800.0),
        ("2h", 7200.0),
        ("45s", 45.0),
        ("1.5h", 5400.0),
        (" 10m ", 600.0),
    ],
)
def test_parse_duration_accepts_valid_forms(text, expected_seconds):
    assert parse_duration(text) == expected_seconds


@pytest.mark.parametrize(
    "text", ["30", "m30", "-5m", "", "30x", "30 m", "half an hour", "30mm"]
)
def test_parse_duration_rejects_invalid_forms(text):
    with pytest.raises(ValueError):
        parse_duration(text)


def test_pause_then_current_pause_is_active_indefinitely(tmp_path):
    pause(tmp_path)

    state = current_pause(tmp_path)

    assert state is not None
    assert state.until is None
    assert state.active()


def test_current_pause_is_none_when_never_paused(tmp_path):
    assert current_pause(tmp_path) is None


def test_resume_ends_an_active_pause(tmp_path):
    pause(tmp_path)

    ended = resume(tmp_path)

    assert ended is True
    assert current_pause(tmp_path) is None


def test_resume_when_not_paused_returns_false(tmp_path):
    assert resume(tmp_path) is False


def test_timed_pause_is_still_active_before_expiry_using_a_patched_clock(
    tmp_path, monkeypatch
):
    fake_now = [1_000_000.0]
    monkeypatch.setattr(pause_module.time, "time", lambda: fake_now[0])

    pause(tmp_path, duration_seconds=1800.0)
    fake_now[0] += 1700.0

    assert current_pause(tmp_path) is not None


def test_timed_pause_ends_after_its_duration_using_a_patched_clock(tmp_path, monkeypatch):
    fake_now = [1_000_000.0]
    monkeypatch.setattr(pause_module.time, "time", lambda: fake_now[0])

    pause(tmp_path, duration_seconds=1800.0)
    fake_now[0] += 1800.1

    assert current_pause(tmp_path) is None


def test_expired_pause_clears_its_own_state_file(tmp_path, monkeypatch):
    fake_now = [1_000_000.0]
    monkeypatch.setattr(pause_module.time, "time", lambda: fake_now[0])

    pause(tmp_path, duration_seconds=10.0)
    fake_now[0] += 20.0
    current_pause(tmp_path)

    assert not pause_module.pause_path(tmp_path).exists()


def test_current_pause_is_none_for_a_corrupt_state_file(tmp_path):
    path = pause_module.pause_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json")

    assert current_pause(tmp_path) is None


def test_current_pause_is_none_for_a_state_file_missing_required_fields(tmp_path):
    path = pause_module.pause_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")

    assert current_pause(tmp_path) is None


def test_file_has_wip_marker_true_when_line_present(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("def add(a, b):\n    # shadowci: wip\n    return a + b\n")

    assert file_has_wip_marker(target) is True


def test_file_has_wip_marker_false_without_the_line(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("def add(a, b):\n    return a + b\n")

    assert file_has_wip_marker(target) is False


def test_file_has_wip_marker_false_for_a_missing_file(tmp_path):
    assert file_has_wip_marker(tmp_path / "missing.py") is False
