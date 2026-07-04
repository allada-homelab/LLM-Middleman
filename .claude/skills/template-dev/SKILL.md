---
name: template-dev
description: Add to or change this Copier template — adding a copier question/toggle, editing files under template/, the .jinja-vs-verbatim rule, gating with _exclude, GitHub-expression escaping, wiring dependencies, and the frozen-variable-contract + tag workflow. Use when editing copier.yml or any file under template/, or adding an include_* toggle.
---

# Developing the Copier template

Read `CLAUDE.md` for the full gotcha list. This skill is the procedure for changing the template.
Always pair changes with the `render-test` skill to prove them green before pushing.

## Where files go

- `copier.yml` — the schema (questions, gating). Treat declared variable names as a **frozen
  contract**; renaming one is a breaking change for `copier update` consumers.
- `template/<path>` — what gets rendered. **Name a file `*.jinja` only if its content needs a
  copier variable** (it's rendered, suffix stripped). Everything else is copied **verbatim** —
  verbatim files may safely contain literal `{{ }}` (CI/Just syntax) because they are not rendered.
- `repos/` — read-only prior-art reference; never edit; never renders.

## Adding a new `include_*` toggle (the multi-file checklist)

A toggle is dead unless every step is done in the same change (CI's `no-dead-vars.sh` enforces it):

1. **Question** in `copier.yml` (`type: bool`, `default: true` unless opt-in). Archetype-specific?
   add `when: "{{ project_type == 'service' }}"` so it isn't asked otherwise.
2. **The file(s)** under `template/` it controls.
3. **Gate it** — pick one:
   - whole-file drop: add to `_exclude` using the **rendered destination path** (e.g.
     `{% if not include_x %}path/to/file.yml{% endif %}` — NOT `file.yml.jinja`);
   - in-file: wrap a stanza in `{%- if include_x %}…{%- endif %}` inside an always-present file.
4. **Wire dependencies** (if any) into `template/pyproject.toml.jinja` under the same `{% if %}`
   (runtime deps, a `[project.optional-dependencies]` extra, or a dependency-group). Add coverage
   `omit` for example/entrypoint modules.
5. **Run `render-test`** (all cells + `no-dead-vars.sh`). Consider an answer cell that exercises
   the new toggle OFF.

## Editing rendered GitHub workflows (`template/.github/workflows/*.jinja`)

Wrap **every** GitHub `${{ … }}` in `{% raw %}…{% endraw %}`. To inject a copier var into a GH
expression, use the escape:

```jinja
runs-on: ${{ '{{' }} vars.CI_RUNNER || '{{ ci_runner_default }}' {{ '}}' }}
```

## Computed/hidden variables

Use a normal name + `when: false` (e.g. `package_name`). **Never** a leading-underscore name — those
are parsed as copier settings, not questions, and render empty.

## Toolchain pins / TOML

`template/pyproject.toml.jinja` isn't Python, so `ruff format` won't touch it — keep the rendered
TOML valid by hand (mind `{% if %}` whitespace). Tool versions and the ruff ruleset live here.

## Commit & ship

Branch (never commit to `main`) → `render-test` green → PR → CI green → merge. If the change is
**consumer-visible** (anything under `template/` or a new/changed question), cut a release with the
`release-template` skill so `copier copy gh:…` resolves it. Repo-only changes (docs, `.claude/`,
root files) need no tag.
