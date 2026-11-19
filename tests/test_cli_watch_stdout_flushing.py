"""Regression test for issue #18.

`shadowci watch` must flush its status output promptly even when stdout
isn't a terminal (logged to a file, piped through `tee`/`cat`, run under
`nohup`, in CI, ...). Long-running `watch` sessions are normally stopped
with a signal rather than left to exit on their own, so output sitting in
Python's fully-buffered stdout is lost outright if it is never flushed.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def test_watch_flushes_startup_line_before_being_signalled(git_repo: Path) -> None:
    env = dict(os.environ)
    env.pop("PYTHONUNBUFFERED", None)

    process = subprocess.Popen(
        [sys.executable, "-m", "shadowci", "watch", str(git_repo), "--interval", "0.05"],
        cwd=git_repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    try:
        time.sleep(1.0)
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert "watching" in out, (
        "expected the startup line to already be flushed to stdout before "
        f"the process was signalled; got stdout={out!r} stderr={err!r}"
    )
