from shadowci.providers.fences import strip_code_fence


def test_strip_code_fence_removes_python_fence():
    fenced = "```python\ndef test_average():\n    assert 1 == 1\n```"

    stripped = strip_code_fence(fenced)

    assert stripped == "def test_average():\n    assert 1 == 1"
    compile(stripped, "t.py", "exec")


def test_strip_code_fence_removes_diff_fence():
    fenced = "```diff\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n```"

    stripped = strip_code_fence(fenced)

    assert stripped.startswith("---")
    assert "```" not in stripped


def test_strip_code_fence_leaves_unfenced_text_unchanged():
    text = "def test_x():\n    assert True"

    assert strip_code_fence(text) == text


def test_strip_code_fence_preserves_backticks_inside_the_code():
    fenced = (
        "```python\n"
        "def test_repr():\n"
        "    assert repr('a') == '`a`'\n"
        "```"
    )

    stripped = strip_code_fence(fenced)

    assert stripped == "def test_repr():\n    assert repr('a') == '`a`'"
    compile(stripped, "t.py", "exec")


def test_strip_code_fence_extracts_single_fence_surrounded_by_prose():
    reply = (
        "Here is the regression test:\n"
        "```python\n"
        "def test_x():\n"
        "    assert 1 == 1\n"
        "```\n"
        "It covers the failing case."
    )

    stripped = strip_code_fence(reply)

    assert stripped == "def test_x():\n    assert 1 == 1"
    compile(stripped, "t.py", "exec")


def test_strip_code_fence_leaves_text_with_two_fenced_blocks_unchanged():
    reply = (
        "First:\n"
        "```python\n"
        "def test_a():\n"
        "    assert True\n"
        "```\n"
        "Second:\n"
        "```python\n"
        "def test_b():\n"
        "    assert True\n"
        "```\n"
    )

    assert strip_code_fence(reply) == reply
