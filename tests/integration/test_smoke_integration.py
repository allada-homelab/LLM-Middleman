"""Integration-tier example: hit the containerized service from ``conftest``.

Marked ``@pytest.mark.integration`` so it is deselected from the default run and
only executes under ``pytest -m integration`` (with a Docker daemon available).
"""

from __future__ import annotations

import urllib.request

import pytest

pytestmark = pytest.mark.integration


def test_http_service_responds(http_base_url: str) -> None:
    with urllib.request.urlopen(http_base_url, timeout=10) as resp:  # noqa: S310
        assert resp.status == 200
