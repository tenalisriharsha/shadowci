from pathlib import Path

from shadowci.config import ShadowCIConfig, load_config


def test_defaults_are_sensible(tmp_path: Path):
    config = load_config(tmp_path)
    assert config.project_root == tmp_path
    assert config.shadow_path == tmp_path / ".shadowci" / "shadow"
    assert ".py" in config.watch_extensions
    assert config.debounce_seconds > 0
    assert config.idle_seconds > config.debounce_seconds
    assert config.provider == "scripted"
    assert config.interruption_threshold > 0
    assert config.interruption_min_signals >= 1


def test_load_config_reads_toml_overrides(tmp_path: Path):
    (tmp_path / "shadowci.toml").write_text(
        "[shadowci]\n"
        'shadow_dir = "custom/shadow"\n'
        "debounce_seconds = 1.5\n"
        "idle_seconds = 5.0\n"
        'provider = "claude-headless"\n'
        'watch_extensions = [".py", ".ts"]\n'
        "interruption_threshold = 0.5\n"
        "interruption_min_signals = 3\n"
    )
    config = load_config(tmp_path)
    assert config.shadow_path == tmp_path / "custom" / "shadow"
    assert config.debounce_seconds == 1.5
    assert config.idle_seconds == 5.0
    assert config.provider == "claude-headless"
    assert config.watch_extensions == (".py", ".ts")
    assert config.interruption_threshold == 0.5
    assert config.interruption_min_signals == 3


def test_should_watch_respects_extension_and_ignore_dirs():
    config = ShadowCIConfig(watch_extensions=(".py",), ignore_dirs=("node_modules",))
    assert config.should_watch(Path("src/app.py")) is True
    assert config.should_watch(Path("src/app.ts")) is False
    assert config.should_watch(Path("node_modules/pkg/index.py")) is False


def test_should_watch_allows_everything_when_no_extensions_configured():
    config = ShadowCIConfig(watch_extensions=())
    assert config.should_watch(Path("README.md")) is True
