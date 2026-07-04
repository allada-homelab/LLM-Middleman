"""Smoke tests — verify the package imports and exposes a version."""

import llm_middleman


def test_version_is_nonempty_string() -> None:
    assert isinstance(llm_middleman.__version__, str)
    assert llm_middleman.__version__
