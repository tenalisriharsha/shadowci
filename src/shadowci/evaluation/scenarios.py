"""Synthetic bug-injection scenarios for the evaluation harness.

A `Scenario` pairs a `RecordedSession` with the ground truth the harness
scores its replay against: whether the session's edit actually introduces a
real bug a fix should be detected for (`bug_expected`), and — when it does —
the exact regression test and fix diff a correct response looks like. That
lets `run_scenario` drive a real `ScriptedProvider` through the real
pipeline (`GhostPatchBuilder` included) instead of asserting against a
mocked one: the harness only fixes what the provider *would* say, not
whether the pipeline accepts it.

"Clean" scenarios (`bug_expected=False`) carry no test/fix — an edit that
doesn't break anything should never make it past deterministic checks far
enough to need one. If one ever does, that is itself a false escalation
worth surfacing in the harness report.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from shadowci.events import ChangeKind
from shadowci.evaluation.session import (
    RecordedEdit,
    RecordedSession,
    session_from_dict,
    session_to_dict,
)


@dataclass(frozen=True)
class Scenario:
    """A session plus the ground truth its replay is scored against."""

    session: RecordedSession
    bug_expected: bool
    regression_test: str | None = None
    proposed_fix: str | None = None


def scenario_to_dict(scenario: Scenario) -> dict:
    return {
        "session": session_to_dict(scenario.session),
        "bug_expected": scenario.bug_expected,
        "regression_test": scenario.regression_test,
        "proposed_fix": scenario.proposed_fix,
    }


def scenario_from_dict(data: dict) -> Scenario:
    return Scenario(
        session=session_from_dict(data["session"]),
        bug_expected=bool(data["bug_expected"]),
        regression_test=data.get("regression_test"),
        proposed_fix=data.get("proposed_fix"),
    )


def save_scenario(scenario: Scenario, path: Path) -> None:
    path.write_text(json.dumps(scenario_to_dict(scenario), indent=2) + "\n")


def load_scenario(path: Path) -> Scenario:
    return scenario_from_dict(json.loads(path.read_text()))


def _bug_scenario(
    name: str,
    description: str,
    module_filename: str,
    test_filename: str,
    clean_content: str,
    buggy_content: str,
    regression_test: str,
    proposed_fix: str,
) -> Scenario:
    session = RecordedSession(
        name=name,
        description=description,
        initial_files={module_filename: clean_content, test_filename: regression_test},
        edits=(RecordedEdit(module_filename, ChangeKind.MODIFIED, 0.0, buggy_content),),
    )
    return Scenario(
        session=session,
        bug_expected=True,
        regression_test=regression_test,
        proposed_fix=proposed_fix,
    )


def _clean_scenario(
    name: str,
    description: str,
    module_filename: str,
    test_filename: str,
    clean_content: str,
    test_content: str,
    harmless_edit: str,
) -> Scenario:
    session = RecordedSession(
        name=name,
        description=description,
        initial_files={module_filename: clean_content, test_filename: test_content},
        edits=(RecordedEdit(module_filename, ChangeKind.MODIFIED, 0.0, harmless_edit),),
    )
    return Scenario(session=session, bug_expected=False)


def off_by_one_operator_scenario() -> Scenario:
    """A save flips `+` to `-` in `calc.add`, breaking its test."""
    return _bug_scenario(
        name="off-by-one-operator",
        description="a save flips + to - in calc.add, breaking the suite",
        module_filename="calc.py",
        test_filename="tests/test_calc.py",
        clean_content="def add(a, b):\n    return a + b\n",
        buggy_content="def add(a, b):\n    return a - b\n",
        regression_test="from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        proposed_fix=(
            "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
            " def add(a, b):\n-    return a - b\n+    return a + b\n"
        ),
    )


def off_by_one_bound_scenario() -> Scenario:
    """A save makes `clamp`'s upper bound return `high - 1` instead of `high`."""
    return _bug_scenario(
        name="off-by-one-bound",
        description="a save makes clamp()'s upper bound off by one",
        module_filename="guard.py",
        test_filename="tests/test_guard.py",
        clean_content=(
            "def clamp(value, low, high):\n"
            "    if value > high:\n"
            "        return high\n"
            "    return value\n"
        ),
        buggy_content=(
            "def clamp(value, low, high):\n"
            "    if value > high:\n"
            "        return high - 1\n"
            "    return value\n"
        ),
        regression_test=(
            "from guard import clamp\n\n\n"
            "def test_clamp_upper_bound():\n    assert clamp(15, 0, 10) == 10\n"
        ),
        proposed_fix=(
            "--- a/guard.py\n+++ b/guard.py\n@@ -1,4 +1,4 @@\n"
            " def clamp(value, low, high):\n"
            "     if value > high:\n"
            "-        return high - 1\n"
            "+        return high\n"
            "     return value\n"
        ),
    )


def inverted_condition_scenario() -> Scenario:
    """A save inverts a guard clause in `is_valid_age`, breaking its test."""
    return _bug_scenario(
        name="inverted-condition",
        description="a save inverts a guard clause's comparison",
        module_filename="validate.py",
        test_filename="tests/test_validate.py",
        clean_content=(
            "def is_valid_age(age):\n"
            "    return 0 <= age <= 120\n"
        ),
        buggy_content=(
            "def is_valid_age(age):\n"
            "    return 0 >= age <= 120\n"
        ),
        regression_test=(
            "from validate import is_valid_age\n\n\n"
            "def test_is_valid_age():\n    assert is_valid_age(30) is True\n"
        ),
        proposed_fix=(
            "--- a/validate.py\n+++ b/validate.py\n@@ -1,2 +1,2 @@\n"
            " def is_valid_age(age):\n"
            "-    return 0 >= age <= 120\n"
            "+    return 0 <= age <= 120\n"
        ),
    )


def clean_rename_scenario() -> Scenario:
    """A save only renames a local variable — no behavior change."""
    return _clean_scenario(
        name="clean-rename",
        description="a save only renames a local variable, no behavior change",
        module_filename="greet.py",
        test_filename="tests/test_greet.py",
        clean_content="def greet(name):\n    message = f'hi {name}'\n    return message\n",
        test_content=(
            "from greet import greet\n\n\ndef test_greet():\n    assert greet('a') == 'hi a'\n"
        ),
        harmless_edit="def greet(name):\n    text = f'hi {name}'\n    return text\n",
    )


def clean_comment_scenario() -> Scenario:
    """A save only adds a comment — no behavior change."""
    return _clean_scenario(
        name="clean-comment",
        description="a save only adds a comment, no behavior change",
        module_filename="util.py",
        test_filename="tests/test_util.py",
        clean_content="def double(x):\n    return x * 2\n",
        test_content=(
            "from util import double\n\n\ndef test_double():\n    assert double(3) == 6\n"
        ),
        harmless_edit="def double(x):\n    # scale by two\n    return x * 2\n",
    )


def built_in_scenarios() -> tuple[Scenario, ...]:
    """The default scenario suite the `evaluate` CLI command replays."""
    return (
        off_by_one_operator_scenario(),
        off_by_one_bound_scenario(),
        inverted_condition_scenario(),
        clean_rename_scenario(),
        clean_comment_scenario(),
    )
