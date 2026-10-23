from pathlib import Path

from shadowci.events import (
    ChangeKind,
    ChangeSet,
    CheckResult,
    EvidenceCard,
    FileChangeEvent,
    GhostPatch,
    Signal,
)


def test_file_change_event_default_timestamp_is_set():
    event = FileChangeEvent(path=Path("a.py"), kind=ChangeKind.MODIFIED)
    assert event.timestamp > 0


def test_changeset_paths_deduplicates_and_preserves_order():
    events = (
        FileChangeEvent(path=Path("a.py"), kind=ChangeKind.MODIFIED, timestamp=1.0),
        FileChangeEvent(path=Path("b.py"), kind=ChangeKind.CREATED, timestamp=2.0),
        FileChangeEvent(path=Path("a.py"), kind=ChangeKind.MODIFIED, timestamp=3.0),
    )
    changeset = ChangeSet(events=events)
    assert changeset.paths == (Path("a.py"), Path("b.py"))


def test_evidence_card_all_checks_passed_true_when_empty():
    card = EvidenceCard(summary="no checks yet", failing_test_before="")
    assert card.all_checks_passed is True


def test_evidence_card_all_checks_passed_false_on_any_failure():
    card = EvidenceCard(
        summary="one check failed",
        failing_test_before="def test_x(): assert False",
        check_results=(
            CheckResult(name="pytest", passed=True),
            CheckResult(name="mypy", passed=False, output="error: bad type"),
        ),
    )
    assert card.all_checks_passed is False


def test_ghost_patch_bundles_diff_and_evidence():
    changeset = ChangeSet(events=())
    evidence = EvidenceCard(summary="fix", failing_test_before="x")
    patch = GhostPatch(diff="--- a\n+++ b\n", evidence=evidence, changeset=changeset)
    assert patch.evidence is evidence
    assert patch.changeset is changeset


def test_signal_values():
    assert {s.value for s in Signal} == {"accept", "reject", "ignore"}
