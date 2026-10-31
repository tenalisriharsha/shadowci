from pathlib import Path

from shadowci.events import ChangeKind
from shadowci.evaluation.session import (
    RecordedEdit,
    RecordedSession,
    load_session,
    save_session,
    session_from_dict,
    session_to_dict,
)


def _sample_session() -> RecordedSession:
    return RecordedSession(
        name="sample",
        description="a sample session",
        initial_files={"app.py": "def add(a, b):\n    return a + b\n"},
        edits=(
            RecordedEdit("app.py", ChangeKind.MODIFIED, 0.0, "def add(a, b):\n    return a - b\n"),
            RecordedEdit("app.py", ChangeKind.DELETED, 1.5, None),
        ),
    )


def test_session_round_trips_through_a_dict():
    session = _sample_session()

    restored = session_from_dict(session_to_dict(session))

    assert restored == session


def test_session_round_trips_through_a_file(tmp_path: Path):
    session = _sample_session()
    path = tmp_path / "session.json"

    save_session(session, path)
    restored = load_session(path)

    assert restored == session
    assert path.read_text().strip().startswith("{")


def test_session_from_dict_defaults_missing_optional_fields():
    restored = session_from_dict(
        {
            "name": "minimal",
            "edits": [
                {"relative_path": "a.py", "kind": "created", "offset_seconds": 0.0},
            ],
        }
    )

    assert restored.description == ""
    assert restored.initial_files == {}
    assert restored.edits[0].kind is ChangeKind.CREATED
    assert restored.edits[0].content is None
