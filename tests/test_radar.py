import threading
import time
from pathlib import Path

from shadowci import pipeline, radar
from shadowci.config import ShadowCIConfig
from shadowci.events import ChangeKind, CheckResult, FileChangeEvent
from shadowci.interruption import InterruptionDecision


def _config(repo: Path, **overrides) -> ShadowCIConfig:
    config = ShadowCIConfig(
        project_root=repo,
        shadow_dir=Path(".shadowci/shadow"),
        debounce_seconds=0.02,
        idle_seconds=0.05,
        poll_interval_seconds=0.02,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def test_render_lines_is_pure_text(tmp_path: Path):
    decision = InterruptionDecision(interrupt=True, score=0.5, fingerprint="pytest", sample_size=4)
    patch = radar.PendingGhostPatch(
        path=tmp_path / "abc123.patch",
        digest="abc123",
        fingerprint="pytest",
        summary="a verified fix",
        decision=decision,
    )
    state = radar.RadarState(
        project_root=tmp_path,
        provider="scripted",
        cycle=3,
        pending_changesets=1,
        recent_events=(FileChangeEvent(path=tmp_path / "app.py", kind=ChangeKind.MODIFIED),),
        last_check_results=(CheckResult(name="pytest", passed=False, duration_seconds=0.1),),
        pending_patches=(patch,),
        log_lines=("something happened",),
    )

    lines = radar.render_lines(state, selected=0)
    text = "\n".join(lines)

    assert "Bug-Radar" in text
    assert "cycle: 3" in text
    assert "modified" in text
    assert "[pytest] FAIL" in text
    assert "abc123.patch" in text
    assert "verdict=surface" in text
    assert "something happened" in text
    assert "[a] accept" in text


def test_render_lines_handles_empty_state(tmp_path: Path):
    state = radar.RadarState(project_root=tmp_path, provider="scripted")
    lines = radar.render_lines(state)
    text = "\n".join(lines)

    assert "(none yet)" in text
    assert "(no checks run yet)" in text
    assert "(none pending)" in text
    assert "(nothing yet)" in text


def test_scan_pending_patches_reads_saved_metadata(git_repo: Path):
    config = _config(git_repo)
    ghost_dir = pipeline.ghost_patch_dir(git_repo)
    patch_path = ghost_dir / "aaa111.patch"
    patch_path.write_text("irrelevant diff")
    patch_path.with_suffix(".json").write_text(
        '{"digest": "aaa111", "fingerprint": "pytest", "summary": "fix summary"}'
    )

    patches = radar.scan_pending_patches(git_repo, config)

    assert len(patches) == 1
    assert patches[0].fingerprint == "pytest"
    assert patches[0].summary == "fix summary"
    assert patches[0].decision.interrupt is False  # no signal history yet


def test_scan_pending_patches_skips_files_without_metadata(git_repo: Path):
    config = _config(git_repo)
    ghost_dir = pipeline.ghost_patch_dir(git_repo)
    (ghost_dir / "orphan.patch").write_text("no sidecar")

    assert radar.scan_pending_patches(git_repo, config) == ()


def test_engine_note_and_refresh_patches_do_not_run_a_poll_cycle(git_repo: Path):
    config = _config(git_repo)
    engine = radar.RadarEngine(config)

    state = engine.note("hello")
    assert state.log_lines == ("hello",)
    assert state.cycle == 0

    ghost_dir = pipeline.ghost_patch_dir(git_repo)
    patch_path = ghost_dir / "aaa111.patch"
    patch_path.write_text("irrelevant")
    patch_path.with_suffix(".json").write_text(
        '{"digest": "aaa111", "fingerprint": "pytest", "summary": "fix summary"}'
    )

    state = engine.refresh_patches()
    assert len(state.pending_patches) == 1
    assert state.cycle == 0


def test_engine_step_detects_a_changeset_and_runs_checks(git_repo: Path):
    config = _config(git_repo)
    engine = radar.RadarEngine(config)
    engine.ensure_worktree()

    def drive() -> None:
        for _ in range(60):
            engine.step()
            time.sleep(0.02)

    thread = threading.Thread(target=drive)
    thread.start()
    time.sleep(0.05)
    (git_repo / "feature.py").write_text("def feature():\n    return True\n")
    thread.join(timeout=10)

    state = engine.state
    assert state.cycle > 0
    assert state.last_changeset is not None
    assert any(result.name == "pytest" for result in state.last_check_results)
    shadow_copy = git_repo / ".shadowci" / "shadow" / "feature.py"
    assert shadow_copy.exists()
    assert any("changeset ready" in line for line in state.log_lines)
