from pathlib import Path

from shadowci.changeset import ChangeSetDetector
from shadowci.config import ShadowCIConfig
from shadowci.events import ChangeKind, FileChangeEvent


def _config() -> ShadowCIConfig:
    return ShadowCIConfig(debounce_seconds=0.1, idle_seconds=0.3)


def test_poll_returns_none_before_debounce_elapses():
    detector = ChangeSetDetector(_config())
    detector.add_events([FileChangeEvent(Path("a.py"), ChangeKind.MODIFIED, timestamp=100.0)])
    assert detector.poll(now=100.05) is None


def test_poll_returns_none_between_debounce_and_idle():
    detector = ChangeSetDetector(_config())
    detector.add_events([FileChangeEvent(Path("a.py"), ChangeKind.MODIFIED, timestamp=100.0)])
    assert detector.poll(now=100.2) is None


def test_poll_promotes_changeset_once_idle_with_non_python_file(tmp_path: Path):
    target = tmp_path / "notes.txt"
    target.write_text("anything goes\n")
    detector = ChangeSetDetector(_config())
    detector.add_events([FileChangeEvent(target, ChangeKind.MODIFIED, timestamp=100.0)])

    changeset = detector.poll(now=100.5)

    assert changeset is not None
    assert changeset.paths == (target,)
    assert detector.pending_count == 0


def test_valid_python_promotes_after_idle(tmp_path: Path):
    target = tmp_path / "app.py"
    target.write_text("def add(a, b):\n    return a + b\n")
    detector = ChangeSetDetector(_config())
    detector.add_events([FileChangeEvent(target, ChangeKind.MODIFIED, timestamp=100.0)])

    changeset = detector.poll(now=100.5)

    assert changeset is not None
    assert len(changeset.events) == 1


def test_invalid_python_syntax_blocks_promotion(tmp_path: Path):
    target = tmp_path / "broken.py"
    target.write_text("def add(a, b:\n    return a + b\n")  # unclosed paren, mid-edit
    detector = ChangeSetDetector(_config())
    detector.add_events([FileChangeEvent(target, ChangeKind.MODIFIED, timestamp=100.0)])

    assert detector.poll(now=100.5) is None
    assert detector.pending_count == 1  # still buffered, not dropped


def test_fixing_syntax_allows_later_promotion_without_new_event(tmp_path: Path):
    target = tmp_path / "broken.py"
    target.write_text("def add(a, b:\n    return a + b\n")
    detector = ChangeSetDetector(_config())
    detector.add_events([FileChangeEvent(target, ChangeKind.MODIFIED, timestamp=100.0)])
    assert detector.poll(now=100.5) is None

    target.write_text("def add(a, b):\n    return a + b\n")
    changeset = detector.poll(now=100.6)

    assert changeset is not None


def test_deleted_file_event_skips_syntax_check(tmp_path: Path):
    target = tmp_path / "gone.py"
    detector = ChangeSetDetector(_config())
    detector.add_events([FileChangeEvent(target, ChangeKind.DELETED, timestamp=100.0)])

    changeset = detector.poll(now=100.5)

    assert changeset is not None


def test_unrelated_valid_file_promotes_despite_other_file_staying_broken(tmp_path: Path):
    broken = tmp_path / "broken.py"
    broken.write_text("def f(:\n")  # left broken, never fixed
    good = tmp_path / "good.py"
    good.write_text("def g(): return 1\n")

    detector = ChangeSetDetector(_config())
    detector.add_events([FileChangeEvent(broken, ChangeKind.MODIFIED, timestamp=100.0)])
    assert detector.poll(now=100.5) is None  # broken.py isn't done yet

    # much later, an unrelated, syntactically valid file is saved
    detector.add_events([FileChangeEvent(good, ChangeKind.MODIFIED, timestamp=200.0)])
    changeset = detector.poll(now=200.5)

    assert changeset is not None, "good.py should promote even though broken.py never recovers"
    assert changeset.paths == (good,)
    assert detector.pending_count == 1  # broken.py stays buffered, waiting on its own fix


def test_new_events_reset_the_idle_window():
    detector = ChangeSetDetector(_config())
    detector.add_events([FileChangeEvent(Path("a.py"), ChangeKind.MODIFIED, timestamp=100.0)])
    detector.add_events([FileChangeEvent(Path("a.py"), ChangeKind.MODIFIED, timestamp=100.4)])

    # Idle window reset by the second event; 100.5 is only 0.1s after it.
    assert detector.poll(now=100.5) is None
