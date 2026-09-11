"""Guard: every third-party module the integration imports is declared in manifest.json.

This is the regression guard for the 2026-09-10 outage. ``llm_middleman`` imports
``voluptuous_openapi`` (in ``backends/ollama.py`` and ``backends/openai_compat.py``)
but ``manifest.json`` did not list ``voluptuous-openapi`` in ``requirements``. The
dev test env never caught it because ``homeassistant`` pulls ``voluptuous-openapi``
transitively, so any test that imported the package passed — but HA's runtime
environment does not ship it, so the integration failed to import at load and the
``conversation.new_jarvis`` agent went ``unavailable``.

The check is deliberately independent of what the current interpreter happens to
have installed: it walks the integration's imports and asserts each third-party
distribution is *declared* in ``manifest.json`` ``requirements``. That is the
contract HA relies on when it installs custom-component dependencies at load.
"""

from __future__ import annotations

import ast
import json
from importlib.metadata import packages_distributions
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = REPO_ROOT / "custom_components" / "llm_middleman"
MANIFEST = INTEGRATION / "manifest.json"


def _stdlib_module_names() -> set[str]:
    """Return the set of stdlib top-level module names for this interpreter."""
    import sys

    return set(sys.stdlib_module_names)


def _third_party_imports() -> set[str]:
    """Return the third-party top-level modules the integration imports."""
    stdlib = _stdlib_module_names()
    imported: set[str] = set()

    for path in INTEGRATION.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])

    # Filter to modules that are neither stdlib nor this package.
    third_party = imported - stdlib - {"homeassistant", "custom_components"}
    # Drop our own intra-package imports that are root-relative but named after
    # the package (only if the package's own top-level name ever sneaks in).
    third_party -= {"llm_middleman"}
    return third_party


def _manifest_requirements() -> set[str]:
    """Return the distribution names declared in manifest.json ``requirements``."""
    data = json.loads(MANIFEST.read_text())
    # Normalise the pip requirement strings ("pkg==1.2", "pkg>=1") to bare names.
    return {req.split("=")[0].split("<")[0].split(">")[0].strip() for req in data.get("requirements", [])}


def test_third_party_imports_are_declared_in_manifest() -> None:
    """Every third-party distribution the integration imports must be declared.

    No ``homeassistant``-provides exemption: the guard must be independent of the
    specific ``homeassistant`` the dev env happens to resolve. ``homeassistant``
    dropped ``voluptuous-openapi`` from its own deps (2026.9.x), which is exactly
    why an exemption-keyed check would have missed this regression. A distribution
    that is already provided by HA is harmless if redeclared here — HA just sees an
    already-satisfied requirement.
    """
    declared = _manifest_requirements()
    dist_map = packages_distributions()
    missing: list[str] = []

    for mod in sorted(_third_party_imports()):
        dists = dist_map.get(mod, [])
        if not dists:
            # A module with no registered distribution (a vendored or namespace
            # module) can't be declared; only flag real distributions.
            continue
        for dist in dists:
            if dist not in declared:
                missing.append(f"{mod} -> {dist}")

    assert not missing, (
        "Integration imports third-party modules whose distributions are not declared "
        "in manifest.json requirements. HA will not install them, so the integration "
        "will fail to import at load. Declare each in `requirements`.\n" + "\n".join(f"  - {m}" for m in missing)
    )


def test_manifest_declares_requirements_field() -> None:
    """The manifest must have a ``requirements`` field at all."""
    data = json.loads(MANIFEST.read_text())
    assert "requirements" in data, "manifest.json is missing the `requirements` field"
