from pathlib import Path

from shadowci.checks.base import clear_bytecode_cache


def test_clear_bytecode_cache_removes_every_pycache_dir(tmp_path: Path):
    top_cache = tmp_path / "__pycache__"
    top_cache.mkdir()
    (top_cache / "mod.cpython-311.pyc").write_bytes(b"fake")

    nested_cache = tmp_path / "pkg" / "__pycache__"
    nested_cache.mkdir(parents=True)
    (nested_cache / "sub.cpython-311.pyc").write_bytes(b"fake")

    clear_bytecode_cache(tmp_path)

    assert not top_cache.exists()
    assert not nested_cache.exists()


def test_clear_bytecode_cache_is_a_noop_when_nothing_cached(tmp_path: Path):
    clear_bytecode_cache(tmp_path)  # must not raise
