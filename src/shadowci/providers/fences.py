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


def strip_code_fence(text: str) -> str:
    """Remove one outer Markdown code fence wrapping `text`, if present.

    Only a fence that wraps the *entire* text is removed: an opening
    ```` ``` ```` (with an optional language tag) on the first line and a
    closing ```` ``` ```` on the last line. Text without such a wrapping
    fence, including text that merely contains backticks, is returned
    unchanged.
    """
    match = _FENCE_RE.match(text)
    if match is None:
        return text
    return match.group("body")
