# Plan: a fully-featured development environment for `python-template`

> Status: **reviewed draft.** Scope: tooling for developing the *template repo itself* (not the
> rendered consumer output). Passes: orchestrator + 3 research streams (copier testing, local-env,
> pre-commit) → drafted; then a **Fable-5 critique pass** cross-checked every claim against the repo;
> then the orchestrator re-verified the load-bearing corrections at source. Versions verified live
> **2026-07-03**. `file:line` citations are to the repo state on branch `chore/template-dev-comforts`.

## Thesis

The restraint thesis holds and is confirmed: the repo already has the hard parts — render loop, 6-cell
CI matrix with uv caching (`render-test.yml:22,28-30`), scoped pre-commit, dev container. "Fully
featured" means **close the genuine gaps and harden what exists — not pile on tools.** Anything not
earned is in the **Ruled out** table so it isn't re-litigated.

**Blast radius:** everything is **repo-root only → no version tag** (`_subdirectory: template`,
`copier.yml:11`) EXCEPT Phase 4 (touches `template/`/`copier.yml` → needs `git tag`).

## Corrections applied after the Fable-5 review (what changed from the first draft)

- **Keep `uv tool install rust-just` (do NOT swap to an install script).** Verified at source: casey/just's
  official README lists `uv tool install rust-just` in its Packages/Cross-platform section — it is a
  blessed path, not a suspect third-party wrapper. The proposed `just.systems/install.sh | bash` swap
  was an unchecksummed `curl | sh` that contradicts the repo's own containers-best-practices. Fix is
  smaller: **pin the version** and fix the stale "(feature)" comment at `.devcontainer/post-create.sh:2-3`
  (`devcontainer.json` already installs it as a uv tool, not a feature).
- **Drop the Phase 4 `_migrations` scaffold** — it already exists as a documented comment block at
  `copier.yml:81-87` (shape, when-to-add, example). Adding an empty active key is a no-op.
- **Phase 1 was under-specified** — five concrete mechanics added (see Phase 1) without which the first
  run fails for uninteresting reasons.
- **Release debt surfaced (Phase 4):** `include_code_scanning` — the question, its `_exclude` line, and
  `template/.github/workflows/codeql.yml.jinja` — were **removed between `v0.7.0` and `HEAD`** (commit
  `4ff958c`, merged `3e1f346`), verified by `git diff v0.7.0..HEAD`. Consumers running `copier copy`
  today still get v0.7.0. The next tag ships a **removed question** → per `release-template/SKILL.md`
  step 3 that's the "major" criterion; on 0.x, cut **v0.8.0** with an UPGRADING note.
- **Phase 2 downgraded to skip:** `pytest-snapshot` 0.9.0 is the latest release but dates to **2022-04**
  (unmaintained). Golden-file diff if byte-drift guarding is ever wanted.
- **Phase 1 integrates with, not parallels, `render-test/SKILL.md:68-78`** (which already documents a
  *manual* update test) — rewrite that section to lead with the automated path.
- **Root `.vscode/` trimmed** to `extensions.json` only (the devcontainer already carries settings +
  extensions, `devcontainer.json:50-65`); markdownlint demoted to optional.

**Verified-correct in both passes (no change):** 7 tags v0.1.0–v0.7.0; copier 9.16.0; pre-commit-hooks
v6.0.0; 6-cell matrix + `no-dead-vars` only (`render-test.yml:22,54`); `docs/plans/` untracked while
`README.md:49` advertises it; consumer codespell `v2.4.1` (`template/.pre-commit-config.yaml:14`).

## Session baseline (uncommitted, branch `chore/template-dev-comforts`)

Root `justfile` (render/gate/check/matrix/render-dirty/boot/no-dead-vars/clean → gitignored
`.render/<cell>/`), root `.devcontainer/`, root `.pre-commit-config.yaml`, `.gitignore` += `.render/`,
doc pointers in `CLAUDE.md` + `render-test/SKILL.md` (incl. the `svc-variations` fix). Verified this
session: `just check lib-off` rendered + passed the full gate; shellcheck clean; justfile parses;
pre-commit config validates; `no-dead-vars` OK. **Not yet verified: the devcontainer has not been booted.**

---

## Phase 0 — Land + refine the session baseline `[repo-only, no tag, ~45 min]`

1. **Keep** `uv tool install rust-just` in `.devcontainer/post-create.sh:18`; **pin** the version; fix
   the stale "(feature)" comment (lines 2-3).
2. `.pre-commit-config.yaml` rev bumps: `pre-commit-hooks v5.0.0 → v6.0.0` (drops only unused
   `check-byte-order-marker`/`fix-encoding-pragma`), `shellcheck-py v0.10.0.1 → v0.11.0.1`.
3. Add hooks: `check-jsonschema 0.37.4` (`check-github-workflows` + `check-dependabot` — validates
   `render-test.yml` + `dependabot.yml`; jinja-safe via its own anchored `files:`), `codespell v2.4.2`
   (exclude `^(template|repos)/`), `detect-private-key` (free, in the bumped pre-commit-hooks repo).
   markdownlint-cli2 + `.markdownlint.yaml` (MD013/MD033 off): **optional**, only if the noise-tuning
   is acceptable.
4. Commit `docs/plans/dev-environment.md` (untracked) and reword `README.md:49` to match reality
   ("design + dev-environment plans" — drop the "prior-art audit, per-area scope docs" claim).
5. **Boot the devcontainer once** — `devcontainer up --workspace-folder .` or VS Code "Reopen in
   Container". This is the phase's done-gate. Confirm `bypassPermissions` (`devcontainer.json:62`) is
   deliberate.

**Done:** devcontainer boots; `just --list` works inside it; `pre-commit run --all-files` green; plan
committed; PR open with render-test green.

## Phase 1 — Automated `copier update` test `[repo-only, no tag, ~half day]` ⭐

Highest-value gap. New file `.github/render-test/test_update.py` (co-located with `answers/` +
`no-dead-vars.sh`; the repo has no root Python package) as a **PEP 723 script**:

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["copier>=9.16,<10", "pytest>=8", "pyyaml"]
# ///
```

Parametrized over `["library", "service"]`:

1. `prev = git tag --sort=-v:refname | head -1` — **derive** the previous tag (evergreen "latest
   release → HEAD"), never hardcode `v0.7.0`.
2. `run_copy(str(REPO_ROOT), dst, vcs_ref=prev, data={"project_name": "Update Probe", "project_type": pt}, defaults=True, unsafe=True)` with `dst = tmp_path.resolve() / "proj"`.
   - `unsafe=True` is **required** — copier raises `UnsafeTemplateError` on the mere presence of
     `_tasks`, even though they're `_copier_operation=='copy'`-gated.
   - `tmp_path.resolve()` — dodges the `/var/folders` symlink gotcha CLAUDE.md documents.
   - Use `defaults` + minimal `data`, **not** the HEAD answers files (they dropped
     `include_code_scanning` → drift). Tasks run (real consumer state: `uv.lock`, `ruff format`).
3. `git init` → commit with `-c user.email=t@t -c user.name=t` (as `render-test/SKILL.md:73` does) →
   apply a consumer edit to a **template-managed** file (e.g. append to `src/<pkg>/__init__.py`) →
   commit again. (`copier update` refuses a dirty tree — `update-project/SKILL.md` step 1.)
4. `run_update(str(dst), vcs_ref="HEAD", defaults=True, unsafe=True, overwrite=True, skip_answered=True, conflict="inline")`.
5. **Durable asserts:** exit clean; no `<<<<<<<` markers in tracked files; consumer edit intact;
   `.copier-answers.yml` `_commit` advanced; `uv.lock`/`README.md`/`CHANGELOG.md` untouched
   (`_skip_if_exists`, `copier.yml:65-68`).
6. **First-run manual observation (not a durable assert):** the update should **delete**
   `.github/workflows/codeql.yml` (removed question). If it conflicts instead, that's the real finding —
   triage before the next tag.

Wiring: `just update-test` recipe (`uv run --script .github/render-test/test_update.py`); new
`update-test` job in `render-test.yml` (checkout `fetch-depth: 0` fetches tags, setup-uv + cache, one
run step); **rewrite** `render-test/SKILL.md:68-78` to lead with `just update-test`, keeping the manual
loop as the debug path; one line into CLAUDE.md's matrix paragraph.

**Done:** `just update-test` green locally + CI for both archetypes; first-run findings triaged (clean
pass, or a documented issue + fix before the next tag).

## Phase 2 — Snapshots: **SKIP** `[recommend against]`

`pytest-snapshot`'s only release is 2022-04 (unmaintained); the gate already proves renders *work*.
If ever needed: checked-in golden copies of the 3 fragile files per cell (`pyproject.toml`, workflow
YAML, `.copier-answers.yml`) + `git diff --no-index` in a just recipe — zero new deps. Re-open only
after a real byte-drift incident.

## Phase 3 — CI + editor hardening `[repo-only, no tag, ~1–2 hr]`

1. `.github/dependabot.yml`: add the `devcontainers` ecosystem stanza (covers the stale `va-h/uv`
   feature drift that bit this session).
2. `pre-commit` CI job in `render-test.yml`: checkout + setup-uv + `uvx pre-commit run --all-files`,
   with `~/.cache/pre-commit` cached (`actions/cache` keyed on the config hash). Skip
   `pre-commit/action` (maintenance mode).
3. `.vscode/extensions.json` only (mirror `devcontainer.json:52-59`) — no settings/tasks.
4. Open a `va-h/uv` tracking issue (fallback: install uv in post-create instead of the feature).

**Done:** dependabot would open feature PRs; a PR with a deliberate workflow-schema error fails the
pre-commit job.

## Phase 4 — Next release `[consumer-visible, needs tag]`

- Bump `template/.pre-commit-config.yaml:14` codespell `v2.4.1 → v2.4.2` (batch it).
- The tag also ships the already-merged CodeQL/`include_code_scanning` removal (commit `4ff958c`) — a
  removed question. Per `release-template/SKILL.md` step 3, cut **v0.8.0** with an UPGRADING/CHANGELOG
  note ("question removed; `copier update` deletes `codeql.yml` — verified by the Phase 1 test"). No
  `_migrations` entry needed — copier handles the file deletion itself.

---

## Added items (net +4; 6 rejected on the same "earned" bar)

- **`just update-test` recipe** — the justfile is the dev loop's entry point; a CI-only test would rot.
- **Evergreen prev-tag derivation** — hardcoding a tag makes the test decay after one release.
- **`detect-private-key` hook** — zero-cost (in the bumped pre-commit-hooks repo), cheap secrets cover.
- **Release-debt callout (Phase 4)** — main carries an untagged removed-question change; changes the
  next bump's size.

**Rejected:** actionlint (one root workflow; check-yaml + check-jsonschema suffice), gitleaks (no root
secret material; detect-private-key covers it), root `.editorconfig` (pre-commit whitespace hooks
normalize), CONTRIBUTING.md (CLAUDE.md is the manual; single-maintainer), local matrix parallelism
(CI matrix already parallel), extra CI caching (already present, `render-test.yml:29-30`).

## Ruled out (carry forward)

All 10 first-draft rows stand: act, mise/asdf, direnv, yamllint, shfmt, jinja linters (djlint/j2lint/
curlylint), second spell tool (typos/cspell), conventional-pre-commit, pytest-copie, whole-tree
snapshots. **Added:** `pytest-snapshot` (unmaintained since 2022; golden-file diff if ever needed),
`pre-commit/action` (maintenance mode; plain `uvx pre-commit` step instead), root `.vscode/settings+tasks`
(duplicates devcontainer + just).

## Open questions / least confident (ranked)

1. **Devcontainer still never booted** — Phase 0's done-gate; nothing else verifies `onCreateCommand`'s
   chown/mount interplay or the `initialize` script on this host. Load-bearing.
2. **Update-test first run — the `ruff format` task interplay:** consumer projects are formatted by the
   copy-time task, but copier's internal old/new re-renders during update run *without* tasks — if any
   template file renders non-format-clean, that could look like a consumer edit and produce spurious
   conflicts. **Inferred**; confirmable only by running Phase 1.
3. **`skip_tasks` exact behavior in copier 9.16.0** — verified on master source, not the 9.16.0 tag.
   Only matters if the CI speed knob is used.
4. **Pins not independently re-verified this pass** (verified live 2026-07-03 in the research pass;
   spot-checks matched): shellcheck-py v0.11.0.1, check-jsonschema 0.37.4, markdownlint-cli2 v0.23.0,
   codespell v2.4.2, devcontainer feature versions.
