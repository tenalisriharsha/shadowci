"""Phase 5: the Bug-Radar terminal dashboard, a thin curses loop over `radar.py`.

All state and rendering logic lives in `radar.py` and is plain-text
testable; this module only drives `curses` — polling `RadarEngine.step()`
on a timer, rendering `radar.render_lines()` to the screen, and turning
keypresses into calls to `pipeline.accept_patch` / `reject_patch` /
`ignore_patch`, the same functions the `shadowci apply-ghost` /
`reject-ghost` / `ignore-ghost` commands call.
"""

from __future__ import annotations

import curses

from shadowci import pipeline, radar
from shadowci.config import ShadowCIConfig

_ACCEPT_KEYS = (ord("a"), ord("A"))
_REJECT_KEYS = (ord("r"), ord("R"))
_IGNORE_KEYS = (ord("i"), ord("I"))
_DOWN_KEYS = (curses.KEY_DOWN, ord("j"))
_UP_KEYS = (curses.KEY_UP, ord("k"))
_QUIT_KEYS = (ord("q"), ord("Q"))


def run(
    config: ShadowCIConfig,
    skip_checks: bool = False,
    max_cycles: int | None = None,
) -> int:
    engine = radar.RadarEngine(config)
    engine.ensure_worktree()
    return curses.wrapper(_main_loop, engine, skip_checks, max_cycles)


def _main_loop(
    stdscr: "curses._CursesWindow",
    engine: radar.RadarEngine,
    skip_checks: bool,
    max_cycles: int | None,
) -> int:
    curses.curs_set(0)
    stdscr.timeout(_poll_timeout_ms(engine))

    selected = 0
    cycle = 0
    _render(stdscr, engine.state, selected)

    while max_cycles is None or cycle < max_cycles:
        key = stdscr.getch()
        selected = _handle_key(engine, key, selected)
        if key in _QUIT_KEYS:
            break

        state = engine.step(skip_checks=skip_checks)
        selected = min(selected, max(0, len(state.pending_patches) - 1))
        _render(stdscr, state, selected)
        cycle += 1

    return 0


def _poll_timeout_ms(engine: radar.RadarEngine) -> int:
    return max(1, int(engine.config.poll_interval_seconds * 1000))


def _handle_key(engine: radar.RadarEngine, key: int, selected: int) -> int:
    patches = engine.state.pending_patches
    if key in _DOWN_KEYS:
        return min(selected + 1, max(0, len(patches) - 1))
    if key in _UP_KEYS:
        return max(selected - 1, 0)

    if not patches or key not in (*_ACCEPT_KEYS, *_REJECT_KEYS, *_IGNORE_KEYS):
        return selected

    index = min(selected, len(patches) - 1)
    patch = patches[index]
    root = engine.state.project_root

    if key in _ACCEPT_KEYS:
        result = pipeline.accept_patch(root, patch.path)
    elif key in _REJECT_KEYS:
        result = pipeline.reject_patch(root, patch.path)
    else:
        result = pipeline.ignore_patch(root, patch.path)

    engine.note(result.message if result.ok else f"error: {result.message}")
    engine.refresh_patches()
    return min(index, max(0, len(engine.state.pending_patches) - 1))


def _render(stdscr: "curses._CursesWindow", state: radar.RadarState, selected: int) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    for row, line in enumerate(radar.render_lines(state, selected)):
        if row >= height:
            break
        stdscr.addnstr(row, 0, line, max(0, width - 1))
    stdscr.refresh()
