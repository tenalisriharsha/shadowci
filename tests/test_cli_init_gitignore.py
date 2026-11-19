import subprocess
from pathlib import Path

from shadowci.cli import main


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def test_init_adds_shadowci_dir_to_gitignore(git_repo: Path, capsys):
    exit_code = main(["init", str(git_repo)])
    capsys.readouterr()

    assert exit_code == 0
    gitignore = git_repo / ".gitignore"
    assert gitignore.exists()
    assert ".shadowci" in gitignore.read_text()


def test_init_does_not_duplicate_existing_gitignore_entry(git_repo: Path, capsys):
    (git_repo / ".gitignore").write_text("*.log\n.shadowci/\n")

    exit_code = main(["init", str(git_repo)])
    capsys.readouterr()

    assert exit_code == 0
    lines = (git_repo / ".gitignore").read_text().splitlines()
    assert lines.count(".shadowci/") == 1
    assert "*.log" in lines


def test_init_appends_to_existing_gitignore_without_shadowci_entry(git_repo: Path, capsys):
    (git_repo / ".gitignore").write_text("*.log\n")

    exit_code = main(["init", str(git_repo)])
    capsys.readouterr()

    assert exit_code == 0
    content = (git_repo / ".gitignore").read_text()
    assert "*.log" in content
    assert ".shadowci" in content


def test_git_add_all_does_not_stage_shadow_worktree_after_init(git_repo: Path, capsys):
    exit_code = main(["init", str(git_repo)])
    capsys.readouterr()
    assert exit_code == 0

    result = _run_git(["add", "-A"], cwd=git_repo)
    assert "embedded git repository" not in result.stderr

    staged = _run_git(["status", "--porcelain"], cwd=git_repo).stdout
    assert ".shadowci/shadow" not in staged
    assert ".shadowci" not in staged
