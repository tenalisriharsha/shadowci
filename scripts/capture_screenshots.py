"""Capture real terminal output from shadowci commands as PNG screenshots.

Every command below is actually executed; its real stdout/stderr/exit code
is captured and rendered verbatim into a terminal-styled HTML page, which is
then screenshotted with a headless browser. Nothing here is hand-typed or
reworded.
"""

from __future__ import annotations

import html
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "docs" / "screenshots"
PYTHON = sys.executable

TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#11111b; display:flex; justify-content:center; padding:28px; }}
  .term {{
    width: 920px;
    background: #1a1b26;
    border-radius: 10px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.55);
    overflow: hidden;
    font-family: 'Menlo', 'SF Mono', Consolas, monospace;
  }}
  .titlebar {{
    background: #24253a;
    padding: 11px 16px;
    display:flex; align-items:center; gap:8px;
  }}
  .dot {{ width:12px; height:12px; border-radius:50%; }}
  .red {{ background:#ff5f56; }}
  .yellow {{ background:#ffbd2e; }}
  .green {{ background:#27c93f; }}
  .title {{ color:#7d7fa3; font-size:12.5px; margin-left:10px; }}
  .body {{
    padding: 20px 22px 26px;
    color: #c6c9e8;
    font-size: 14px;
    line-height: 1.55;
    white-space: pre-wrap;
    word-break: break-word;
    min-height: 40px;
  }}
  .prompt {{ color: #7ee787; }}
  .path {{ color:#79c0ff; }}
  .cmd {{ color: #f0f0ff; font-weight: 600; }}
  .out {{ color: #b8bce0; }}
  .err {{ color: #ff8080; }}
  .exit-ok {{ color: #4fd98a; }}
  .exit-fail {{ color: #ff6b6b; }}
</style></head>
<body><div class="term">
<div class="titlebar"><div class="dot red"></div><div class="dot yellow"></div><div class="dot green"></div><div class="title">{window_title}</div></div>
<div class="body">{body}</div>
</div></body></html>
"""


def esc(text: str) -> str:
    return html.escape(text)


def render(path_label: str, command: str, stdout: str, stderr: str, returncode: int, window_title: str) -> str:
    lines = [
        f'<span class="prompt">&#10095;</span> <span class="path">{esc(path_label)}</span> '
        f'<span class="cmd">{esc(command)}</span>'
    ]
    if stdout:
        lines.append(f'<span class="out">{esc(stdout.rstrip())}</span>')
    if stderr:
        lines.append(f'<span class="err">{esc(stderr.rstrip())}</span>')
    if returncode != 0:
        lines.append(f'<span class="exit-fail">[exit code {returncode}]</span>')
    body = "\n".join(lines)
    return TEMPLATE.format(window_title=esc(window_title), body=body)


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def label(cwd: Path) -> str:
    text = str(cwd)
    home = str(Path.home())
    return text.replace(home, "~", 1) if text.startswith(home) else text


def shot(html_source: str, out_path: Path, browser) -> None:
    page = browser.new_page(viewport={"width": 980, "height": 10})
    page.set_content(html_source)
    page.wait_for_timeout(50)
    page.screenshot(path=str(out_path), full_page=True)
    page.close()
    print(f"wrote {out_path}")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    demo_repo = Path(tempfile.gettempdir()) / "shadowci-demo-repo"
    shutil.rmtree(demo_repo, ignore_errors=True)
    demo_repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=demo_repo, check=True)
    subprocess.run(["git", "config", "user.name", "Demo User"], cwd=demo_repo, check=True)
    subprocess.run(["git", "config", "user.email", "demo@example.com"], cwd=demo_repo, check=True)
    (demo_repo / "calculator.py").write_text(
        "def divide(a, b):\n    return a / b\n"
    )
    subprocess.run(["git", "add", "calculator.py"], cwd=demo_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial commit"], cwd=demo_repo, check=True)

    not_a_repo = Path(tempfile.gettempdir()) / "shadowci-not-a-repo"
    shutil.rmtree(not_a_repo, ignore_errors=True)
    not_a_repo.mkdir(parents=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        # 1. --help
        cmd = [PYTHON, "-m", "shadowci", "--help"]
        result = run(cmd, cwd=demo_repo)
        shot(
            render(label(demo_repo), "shadowci --help", result.stdout, result.stderr, result.returncode, "shadowci --help"),
            OUT_DIR / "01-help.png",
            browser,
        )

        # 2. init (success path)
        cmd = [PYTHON, "-m", "shadowci", "init", "."]
        result = run(cmd, cwd=demo_repo)
        shot(
            render(label(demo_repo), "shadowci init .", result.stdout, result.stderr, result.returncode, "shadowci init"),
            OUT_DIR / "02-init.png",
            browser,
        )

        # 3. status (after init)
        cmd = [PYTHON, "-m", "shadowci", "status", "."]
        result = run(cmd, cwd=demo_repo)
        shot(
            render(label(demo_repo), "shadowci status .", result.stdout, result.stderr, result.returncode, "shadowci status"),
            OUT_DIR / "03-status.png",
            browser,
        )

        # 4. watch (live detection + shadow sync) — the core ambient feature
        (demo_repo / "shadowci.toml").write_text(
            "[shadowci]\ndebounce_seconds = 0.1\nidle_seconds = 0.3\n"
        )
        watch_cmd = [
            PYTHON, "-m", "shadowci", "watch", ".",
            "--max-cycles", "20", "--interval", "0.1",
        ]
        proc = subprocess.Popen(
            watch_cmd, cwd=demo_repo,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        time.sleep(0.4)
        (demo_repo / "calculator.py").write_text(
            "def divide(a, b):\n    if b == 0:\n        raise ValueError('division by zero')\n    return a / b\n"
        )
        stdout, _ = proc.communicate(timeout=15)
        shot(
            render(
                label(demo_repo),
                "shadowci watch . --max-cycles 20 --interval 0.1",
                stdout, "", proc.returncode, "shadowci watch",
            ),
            OUT_DIR / "04-watch-detects-and-syncs.png",
            browser,
        )

        # 5. watch finds a real bug, runs checks, and escalates to the LLM provider
        bug_repo = Path(tempfile.gettempdir()) / "shadowci-bug-repo"
        shutil.rmtree(bug_repo, ignore_errors=True)
        bug_repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=bug_repo, check=True)
        subprocess.run(["git", "config", "user.name", "Demo User"], cwd=bug_repo, check=True)
        subprocess.run(["git", "config", "user.email", "demo@example.com"], cwd=bug_repo, check=True)
        (bug_repo / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
        (bug_repo / "tests").mkdir()
        (bug_repo / "tests" / "test_calculator.py").write_text(
            "from calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
        )
        subprocess.run(["git", "add", "calculator.py", "tests"], cwd=bug_repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial commit"], cwd=bug_repo, check=True)
        (bug_repo / "shadowci.toml").write_text(
            "[shadowci]\ndebounce_seconds = 0.1\nidle_seconds = 0.3\n"
        )
        watch_checks_cmd = [
            PYTHON, "-m", "shadowci", "watch", ".",
            "--max-cycles", "30", "--interval", "0.1",
        ]
        proc = subprocess.Popen(
            watch_checks_cmd, cwd=bug_repo,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        time.sleep(0.4)
        (bug_repo / "calculator.py").write_text("def add(a, b):\n    return a + b + 1\n")
        stdout, _ = proc.communicate(timeout=20)
        shot(
            render(
                label(bug_repo),
                "shadowci watch . --max-cycles 30 --interval 0.1",
                stdout, "", proc.returncode, "shadowci watch (check fails, escalates)",
            ),
            OUT_DIR / "05-watch-checks-and-escalates.png",
            browser,
        )
        shutil.rmtree(bug_repo, ignore_errors=True)

        # 6. init failure path — not a git repository
        cmd = [PYTHON, "-m", "shadowci", "init", str(not_a_repo)]
        result = run(cmd, cwd=REPO_ROOT)
        shot(
            render(label(REPO_ROOT), f"shadowci init {not_a_repo}", result.stdout, result.stderr, result.returncode, "shadowci init (failure path)"),
            OUT_DIR / "06-init-failure.png",
            browser,
        )

        # 7. apply-ghost — the one-keystroke apply of a verified ghost patch
        patch_repo = Path(tempfile.gettempdir()) / "shadowci-patch-repo"
        shutil.rmtree(patch_repo, ignore_errors=True)
        patch_repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=patch_repo, check=True)
        subprocess.run(["git", "config", "user.name", "Demo User"], cwd=patch_repo, check=True)
        subprocess.run(["git", "config", "user.email", "demo@example.com"], cwd=patch_repo, check=True)
        (patch_repo / "calculator.py").write_text("def add(a, b):\n    return a + b + 1\n")
        subprocess.run(["git", "add", "calculator.py"], cwd=patch_repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial commit"], cwd=patch_repo, check=True)
        (patch_repo / "fix.patch").write_text(
            "--- a/calculator.py\n"
            "+++ b/calculator.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def add(a, b):\n"
            "-    return a + b + 1\n"
            "+    return a + b\n"
        )
        cmd = [PYTHON, "-m", "shadowci", "apply-ghost", "fix.patch", "."]
        result = run(cmd, cwd=patch_repo)
        shot(
            render(
                label(patch_repo),
                "shadowci apply-ghost fix.patch .",
                result.stdout, result.stderr, result.returncode,
                "shadowci apply-ghost",
            ),
            OUT_DIR / "08-apply-ghost.png",
            browser,
        )
        shutil.rmtree(patch_repo, ignore_errors=True)

        # 8. test suite, green
        cmd = [PYTHON, "-m", "pytest", "-v"]
        result = run(cmd, cwd=REPO_ROOT)
        shot(
            render(label(REPO_ROOT), "python -m pytest -v", result.stdout, result.stderr, result.returncode, "shadowci test suite"),
            OUT_DIR / "09-test-suite.png",
            browser,
        )

        # 9-12. the interruption policy: accept/reject/ignore signals feed a
        # per-fingerprint score that decides whether a future fix like this
        # one gets surfaced or stays quiet on disk.
        signals_repo = Path(tempfile.gettempdir()) / "shadowci-signals-repo"
        shutil.rmtree(signals_repo, ignore_errors=True)
        signals_repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=signals_repo, check=True)
        subprocess.run(["git", "config", "user.name", "Demo User"], cwd=signals_repo, check=True)
        subprocess.run(["git", "config", "user.email", "demo@example.com"], cwd=signals_repo, check=True)
        baseline = "def add(a, b):\n    return a + b\n"
        (signals_repo / "app.py").write_text(baseline)
        subprocess.run(["git", "add", "app.py"], cwd=signals_repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial commit"], cwd=signals_repo, check=True)

        accept_fix = (
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def add(a, b):\n"
            "-    return a + b\n"
            "+    return a + b  # verified fix\n"
        )

        def write_patch(name: str, diff: str, fingerprint: str, digest: str) -> Path:
            patch_path = signals_repo / name
            patch_path.write_text(diff)
            patch_path.with_suffix(".json").write_text(
                f'{{"digest": "{digest}", "fingerprint": "{fingerprint}", "summary": "demo fix"}}'
            )
            return patch_path

        # Build up three accepted signals for "pytest-targeted" — the first
        # is the one we screenshot, the other two just add to its history.
        for i in range(3):
            (signals_repo / "app.py").write_text(baseline)
            patch_path = write_patch(f"accept{i}.patch", accept_fix, "pytest-targeted", f"a{i}")
            cmd = [PYTHON, "-m", "shadowci", "apply-ghost", patch_path.name, "."]
            result = run(cmd, cwd=signals_repo)
            if i == 0:
                shot(
                    render(
                        label(signals_repo),
                        f"shadowci apply-ghost {patch_path.name} .",
                        result.stdout, result.stderr, result.returncode,
                        "shadowci apply-ghost (records an accept signal)",
                    ),
                    OUT_DIR / "10-apply-ghost-accept-signal.png",
                    browser,
                )

        # One rejected signal for the same fingerprint.
        reject_patch = write_patch(
            "reject0.patch",
            "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
            " def add(a, b):\n-    return a + b  # verified fix\n+    return a + b  # alt fix\n",
            "pytest-targeted",
            "r0",
        )
        cmd = [PYTHON, "-m", "shadowci", "reject-ghost", reject_patch.name, "."]
        result = run(cmd, cwd=signals_repo)
        shot(
            render(
                label(signals_repo),
                f"shadowci reject-ghost {reject_patch.name} .",
                result.stdout, result.stderr, result.returncode,
                "shadowci reject-ghost",
            ),
            OUT_DIR / "11-reject-ghost.png",
            browser,
        )

        # One ignored signal for a different fingerprint — not enough
        # history yet for the policy to form an opinion either way.
        ignore_patch = write_patch(
            "ignore0.patch",
            "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
            " def add(a, b):\n-    return a + b  # verified fix\n+    return a + b  # type-fix\n",
            "mypy",
            "i0",
        )
        cmd = [PYTHON, "-m", "shadowci", "ignore-ghost", ignore_patch.name, "."]
        result = run(cmd, cwd=signals_repo)
        shot(
            render(
                label(signals_repo),
                f"shadowci ignore-ghost {ignore_patch.name} .",
                result.stdout, result.stderr, result.returncode,
                "shadowci ignore-ghost",
            ),
            OUT_DIR / "12-ignore-ghost.png",
            browser,
        )

        # The aggregate view: per-fingerprint counts and the policy's
        # current verdict for each.
        cmd = [PYTHON, "-m", "shadowci", "signals", "."]
        result = run(cmd, cwd=signals_repo)
        shot(
            render(
                label(signals_repo),
                "shadowci signals .",
                result.stdout, result.stderr, result.returncode,
                "shadowci signals",
            ),
            OUT_DIR / "13-signals.png",
            browser,
        )
        shutil.rmtree(signals_repo, ignore_errors=True)

        browser.close()

    shutil.rmtree(demo_repo, ignore_errors=True)
    shutil.rmtree(not_a_repo, ignore_errors=True)


if __name__ == "__main__":
    main()
