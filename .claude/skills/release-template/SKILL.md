---
name: release-template
description: Cut a tagged vX.Y.Z release of this Copier template so consumers get the latest. Use when tagging or releasing the template after merging a consumer-visible change (anything under template/ or a changed copier.yml question) to main.
---

# Release the template

`copier copy gh:allada-homelab/python-template` resolves the **latest git tag**, so changes only
reach consumers once tagged. Repo-only changes (docs, `.claude/`, root files) need no release.

## Steps

1. **Be on `main`, up to date, clean.** `git checkout main && git pull --ff-only && git status`.
2. **Matrix green.** Run the `render-test` skill's full matrix locally (or confirm the latest
   `render-test` CI run on `main` is green) and `bash .github/render-test/no-dead-vars.sh`.
3. **Pick the SemVer bump** by blast radius vs the latest tag (`git tag --sort=-v:refname | head`):
   - patch: fixes / internal-only template changes;
   - minor: new toggles/features, additive;
   - major: breaking renames or removed/renamed questions (also add a `_migrations` entry in
     `copier.yml` so `copier update` fixes existing projects).
4. **Tag and push:**
   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z — <summary>"
   git push origin vX.Y.Z
   ```
5. **Verify** the published path resolves the new version:
   ```bash
   copier copy --trust --defaults gh:allada-homelab/python-template /tmp/rel-check --data project_name="Rel"
   # first line should read: Copying from template version X.Y.Z
   ```

## Notes

- Never reuse/move a published tag. If a release is wrong, ship the next patch.
- A stale higher tag hijacks `copier copy` (this repo once inherited a `1.1.3` tag that shadowed
  everything until deleted) — keep the tag line clean and monotonic.
