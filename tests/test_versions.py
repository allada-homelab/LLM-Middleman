"""Guard: pyproject.toml's version tracks manifest.json, the version HACS actually reads."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_version_matches_manifest_version() -> None:
    manifest = json.loads((REPO_ROOT / "custom_components" / "llm_middleman" / "manifest.json").read_text())
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())

    assert pyproject["project"]["version"] == manifest["version"], (
        "Bump `version` in pyproject.toml and manifest.json together on release."
    )
