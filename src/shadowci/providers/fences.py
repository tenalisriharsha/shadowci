"""Strip a single surrounding Markdown code fence from a model reply.

Models asked for code or a diff routinely wrap the whole reply in a
triple-backtick fence (optionally tagged with a language, e.g. ```python```
or ```diff```). ShadowCI writes that reply straight to a test file or feeds
it to `git apply`, so a leftover fence line turns valid source into a
`SyntaxError` or an unparsable diff.
"""

from __future__ import annotations

import re

_FENCE_RE = re.compile(
    r"\A```[^\n]*\n(?P<body>.*)\n```\Z",
    re.DOTALL,
)

_ANY_FENCE_RE = re.compile(
    r"```[^\n]*\n(?P<body>.*?)\n```",
    re.DOTALL,
)


def strip_code_fence(text: str) -> str:
    """Remove one outer Markdown code fence wrapping `text`, if present.

    If a fence wraps the *entire* text, that fence is removed: an opening
    ```` ``` ```` (with an optional language tag) on the first line and a
    closing ```` ``` ```` on the last line. Otherwise, if `text` contains
    exactly one fenced block anywhere (surrounded by other prose), the body
    of that block is returned. Text with no fence, or with more than one
    fenced block, including text that merely contains backticks, is
    returned unchanged.
    """
    match = _FENCE_RE.match(text)
    if match is not None:
        return match.group("body")

    fences = list(_ANY_FENCE_RE.finditer(text))
    if len(fences) == 1:
        return fences[0].group("body")
    return text
