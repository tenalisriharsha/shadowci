import curses
from pathlib import Path

from shadowci import pipeline, radar, tui
from shadowci.config import ShadowCIConfig


def _config(repo: Path) -> ShadowCIConfig:
    return ShadowCIConfig(project_root=repo, shadow_dir=Path(".shadowci/shadow"))


def _write_patch(ghost_dir: Path, name: str, fingerprint: str, diff: str) -> Path:
    patch_path = ghost_dir / f"{name}.patch"
    patch_path.write_text(diff)
    patch_path.with_suffix(".json").write_text(
        f'{{"digest": "{name}", "fingerprint": "{fingerprint}", "summary": "fix {name}"}}'
    )
    return patch_path


class _FakeScreen:
    """A duck-typed stand-in for a curses window — no real terminal needed."""

    def __init__(self, height: int = 24, width: int = 80) -> None:
        self._height = height
        self._width = width
        self.rows: dict[int, str] = {}
        self.refreshed = False

    def erase(self) -> None:
        self.rows = {}

    def getmaxyx(self) -> tuple[int, int]:
        return self._height, self._width

    def addnstr(self, row: int, col: int, text: str, n: int) -> None:
        self.rows[row] = text[:n]

    def refresh(self) -> None:
        self.refreshed = True


def test_render_draws_every_line_up_to_screen_height():
    state = radar.RadarState(project_root=Path("/tmp/project"), provider="scripted")
    screen = _FakeScreen(height=4, width=80)

    tui._render(screen, state, 0)

    expected = radar.render_lines(state, 0)
    assert screen.refreshed
    assert len(screen.rows) == 4
    for row in range(4):
        assert screen.rows[row] == expected[row][:79]


def test_poll_timeout_ms_reflects_config(git_repo: Path):
    config = _config(git_repo)
    config.poll_interval_seconds = 0.25
    engine = radar.RadarEngine(config)

    assert tui._poll_timeout_ms(engine) == 250


def test_handle_key_moves_selection_without_acting(git_repo: Path):
    config = _config(git_repo)
    engine = radar.RadarEngine(config)
    ghost_dir = pipeline.ghost_patch_dir(git_repo)
    _write_patch(ghost_dir, "aaa", "pytest", "irrelevant")
    _write_patch(ghost_dir, "bbb", "mypy", "irrelevant")
    engine.refresh_patches()

    selected = tui._handle_key(engine, curses.KEY_DOWN, 0)
    assert selected == 1
    selected = tui._handle_key(engine, curses.KEY_UP, selected)
    assert selected == 0
    # still two patches on disk — no action was taken
    assert len(engine.state.pending_patches) == 2


def test_handle_key_accept_applies_the_selected_patch(git_repo: Path):
    config = _config(git_repo)
    engine = radar.RadarEngine(config)
    ghost_dir = pipeline.ghost_patch_dir(git_repo)
    diff = (
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n-    return a + b\n+    return a + b  # fixed\n"
    )
    _write_patch(ghost_dir, "aaa", "pytest", diff)
    engine.refresh_patches()

    selected = tui._handle_key(engine, ord("a"), 0)

    assert selected == 0
    assert (git_repo / "app.py").read_text() == "def add(a, b):\n    return a + b  # fixed\n"
    assert engine.state.pending_patches == ()
    assert any("recorded signal: accept" in line for line in engine.state.log_lines)


def test_handle_key_reject_discards_the_selected_patch(git_repo: Path):
    config = _config(git_repo)
    engine = radar.RadarEngine(config)
    ghost_dir = pipeline.ghost_patch_dir(git_repo)
    patch_path = _write_patch(ghost_dir, "aaa", "pytest", "irrelevant")
    engine.refresh_patches()

    tui._handle_key(engine, ord("r"), 0)

    assert not patch_path.exists()
    assert engine.state.pending_patches == ()
    assert any("recorded signal: reject" in line for line in engine.state.log_lines)


def test_handle_key_ignore_leaves_the_patch_on_disk(git_repo: Path):
    config = _config(git_repo)
    engine = radar.RadarEngine(config)
    ghost_dir = pipeline.ghost_patch_dir(git_repo)
    patch_path = _write_patch(ghost_dir, "aaa", "pytest", "irrelevant")
    engine.refresh_patches()

    tui._handle_key(engine, ord("i"), 0)

    assert patch_path.exists()
    assert len(engine.state.pending_patches) == 1
    assert any("recorded signal: ignore" in line for line in engine.state.log_lines)


def test_handle_key_ignores_action_keys_when_no_patches_pending(git_repo: Path):
    config = _config(git_repo)
    engine = radar.RadarEngine(config)

    selected = tui._handle_key(engine, ord("a"), 0)

    assert selected == 0
    assert engine.state.log_lines == ()
