from pathlib import Path

from shadowci.events import Signal
from shadowci.signals import SignalStore


def test_all_is_empty_when_no_file_exists(tmp_path: Path):
    store = SignalStore(tmp_path / ".shadowci" / "signals.json")
    assert store.all() == ()


def test_record_persists_and_round_trips(tmp_path: Path):
    store = SignalStore(tmp_path / ".shadowci" / "signals.json")

    record = store.record("pytest-targeted", Signal.ACCEPT, "abc123")

    assert record.fingerprint == "pytest-targeted"
    assert record.signal is Signal.ACCEPT
    assert record.patch_digest == "abc123"
    assert record.recorded_at > 0

    reloaded = SignalStore(store.path)
    assert reloaded.all() == (record,)


def test_record_appends_rather_than_overwriting(tmp_path: Path):
    store = SignalStore(tmp_path / ".shadowci" / "signals.json")

    store.record("pytest-targeted", Signal.ACCEPT, "a")
    store.record("mypy", Signal.REJECT, "b")
    store.record("pytest-targeted", Signal.IGNORE, "c")

    assert len(store.all()) == 3
    assert len(store.for_fingerprint("pytest-targeted")) == 2
    assert len(store.for_fingerprint("mypy")) == 1
    assert store.for_fingerprint("ruff") == ()


def test_store_creates_parent_directory(tmp_path: Path):
    path = tmp_path / "nested" / ".shadowci" / "signals.json"
    store = SignalStore(path)

    store.record("pytest", Signal.ACCEPT, "x")

    assert path.exists()
