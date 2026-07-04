# /// script
# requires-python = ">=3.11"
# dependencies = ["copier>=9.16,<10", "pytest>=8", "pyyaml"]
# ///
"""Automated `copier update` test — the one path the render matrix doesn't cover.

Renders the *previous release tag* into a real git project, simulates a consumer
edit, runs `copier update` to HEAD, and asserts the 3-way merge is clean: no
conflict markers, the consumer's edit survives, and `_skip_if_exists` files are
left alone. This is where a consumer's real edits could be silently clobbered on
update, so it is worth a dedicated test even though no flagship template has one.

Requires real git tags (renders from the latest one). Run it with:

    just update-test        # or: uv run --script .github/render-test/test_update.py

Driven via copier's Python API (how copier tests itself) rather than shelling out,
because `run_update` conflict handling is far cleaner to drive from Python.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import copier
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
# Built at runtime so this source file never contains a literal marker (which would
# trip the check-merge-conflict pre-commit hook on itself).
CONFLICT_MARKER = "<" * 7
CONSUMER_MARKER = "\n# consumer edit — must survive `copier update`\n"


def _git(args: list[str], cwd: Path) -> None:
    # Inline identity so the test needs no ambient git config (esp. in CI).
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _prev_tag() -> str:
    tags = subprocess.run(
        ["git", "tag", "--sort=-v:refname"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    if not tags:
        pytest.skip("no release tags present — `copier update` test needs a prior tag")
    return tags[0]


@pytest.mark.parametrize("project_type", ["library", "service"])
def test_update_from_prev_tag_is_clean(project_type: str, tmp_path: Path) -> None:
    dst = (tmp_path / "proj").resolve()  # .resolve() dodges the /var/folders symlink gotcha
    prev = _prev_tag()

    # 1. Render the previous release into a real git project. Tasks run (unsafe=True) so
    #    the committed baseline is real consumer state (uv.lock, ruff-formatted code).
    copier.run_copy(
        str(REPO_ROOT),
        str(dst),
        vcs_ref=prev,
        data={"project_name": "Update Probe", "project_type": project_type},
        defaults=True,
        unsafe=True,  # required: copier refuses templates with _tasks otherwise
        overwrite=True,
    )
    _git(["init", "-q"], dst)
    _git(["add", "-A"], dst)
    _git(["commit", "-qm", f"rendered from {prev}"], dst)

    # 2. Simulate a consumer editing a template-managed file, and commit it (update
    #    refuses a dirty tree).
    pkg_init = next((dst / "src").glob("*/__init__.py"))
    pkg_init.write_text(pkg_init.read_text() + CONSUMER_MARKER)
    _git(["commit", "-aqm", "consumer edit"], dst)

    # Capture pre-update state to prove the update is not a silent no-op and that
    # it leaves _skip_if_exists files (copier.yml) byte-for-byte alone.
    commit_before = yaml.safe_load((dst / ".copier-answers.yml").read_text()).get("_commit")
    protected = {
        rel: (dst / rel).read_bytes()
        for rel in ("uv.lock", "README.md", "CHANGELOG.md")
        if (dst / rel).is_file()
    }

    # 3. Update to HEAD.
    copier.run_update(
        str(dst),
        vcs_ref="HEAD",
        defaults=True,
        unsafe=True,
        overwrite=True,
        skip_answered=True,
        conflict="inline",
    )

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=dst, check=True, capture_output=True, text=True
    ).stdout.split()

    # 4. No conflict markers anywhere. Skip index entries the update deleted from
    #    disk (e.g. a file behind a removed question) — those are still in `git
    #    ls-files` but gone on disk, and their removal is itself a clean update.
    conflicted = [
        f
        for f in tracked
        if (dst / f).is_file() and CONFLICT_MARKER in (dst / f).read_text(errors="ignore")
    ]
    assert not conflicted, f"`copier update` left conflict markers in: {conflicted}"

    # 5. The consumer edit survived.
    assert CONSUMER_MARKER in pkg_init.read_text(), "consumer edit was clobbered by update"

    # 6. The answers file advanced to the new template commit (not a silent no-op).
    commit_after = yaml.safe_load((dst / ".copier-answers.yml").read_text()).get("_commit")
    assert commit_after and commit_after != commit_before, (
        f"`copier update` did not advance _commit ({commit_before!r} -> {commit_after!r})"
    )

    # 7. _skip_if_exists files were left byte-for-byte alone.
    for rel, before in protected.items():
        assert (dst / rel).read_bytes() == before, f"`copier update` touched _skip_if_exists file: {rel}"


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
