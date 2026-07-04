# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Fixtures for testcontainers-based integration tests.

Opt-in: tests here are marked ``@pytest.mark.integration`` and deselected from the
default run. Execute with ``pytest -m integration`` (requires a Docker daemon).
``testcontainers`` is imported lazily inside the fixture so the default run does
not require it installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(scope="session")
def http_base_url() -> Iterator[str]:
    """Start a throwaway nginx container once per session; yield its base URL.

    A generic, runnable example. Swap nginx for the real service this project
    integrates against (database, API, message broker, …) and adjust the image,
    exposed port, and readiness wait accordingly.
    """
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    with DockerContainer("nginx:1.27-alpine").with_exposed_ports(80) as container:
        wait_for_logs(container, "start worker process", timeout=60)
        host = container.get_container_host_ip()
        port = container.get_exposed_port(80)
        yield f"http://{host}:{port}"
