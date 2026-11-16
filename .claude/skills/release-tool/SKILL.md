---
name: release-tool
description: Cut a clinvar-gkm-parquet tool release and update the compatibility matrix. Use when the user asks to release, tag or publish a new version of the tool.
---

# Cut a tool release

The release process below is the one proposed in the design plan (`docs/plans/2026-10-20-design.md`, §9 and
§10). Where the implementation differs, follow the implementation and fix this skill in the same PR.
Every step that publishes (tag push, PyPI, GitHub Release) needs the user's explicit OK.

## 1. Decide the version

- Read `version --json` on `main` and the changelog since the last tag.
- Tool version (semver):
  - **MAJOR**: a breaking CLI change, a manifest or key-value change a reader would notice, or a rules MAJOR;
  - **MINOR**: a rules MINOR or a new feature;
  - **PATCH**: a fix that doesn't change the rules (including a fix that makes output conform to them).
- The rules version (`compiler.RULES_VERSION`, the `sections` table-spec version) changes only with new rules (see
  `.claude/rules/table-spec.md`). A producer's new `schema_version` needs no tool release: it is a new pairing (ADR 0009).

## 2. Verify before tagging

- `uv sync --locked`, `uv run ruff check`, `uv run ruff format --check` and `uv run pytest` pass (what CI and
  `release.yml` run).
- Run `build`, then `verify --bundle-recompute` and `verify --roundtrip`, on a **real published bundle** with its producer's
  pairing (ClinVar-GKM's newest delta, with its `.sha256` sidecar and ClinVar-GKM's vendored pairing), and record the
  commands and results.
  Every difference is fixed or listed and accepted by the user.
- Run the build twice and compare file sha256s: the same inputs must give byte-identical files.
- If the release claims unchanged output, compare the files of the same builds (ClinVar-GKM's pairing, a selection, a
  CIViC pairing) from the previous release and this one: they differ only in the `tool_version` key-value entry (replace
  the old version string with the new one in the old file's bytes and compare).

## 3. Update the compatibility matrix (one PR)

- The version in `pyproject.toml`, and `uv lock` (the lock records it; CI runs `uv sync --locked`).
- The package's compatibility data (`compat.py`, the source of `version --json`).
- The table in `README.md`: one row per tool release, with what it reads and the table-spec versions it writes.
- `CHANGELOG.md`: a `## X.Y.Z (YYYY-MM-DD)` heading (`release.yml` copies the section under it into the GitHub Release),
  the date, the table-spec version, and every change, including what was under `## Unreleased`.
- The design plan's matrix (§9.2) and the README's install pins and status line.
- Merge only with the user's OK.

## 4. Tag and publish (after the user's OK)

- Before the first release from the renamed repository (`clingen-data-model/gkm-bundle-parquet`, ADR 0009), the PyPI
  project's trusted publisher must name that repository and `release.yml`; the maintainer changes it on PyPI.
- Tag the merge commit on `main` as `vX.Y.Z` (`release.yml` checks it against the package version) and push the tag.
- The release workflow builds the sdist and wheel and publishes them (PyPI via trusted publishing, plus a
  GitHub Release with the changelog entry). Check that the published version installs in a clean environment
  and that `clinvar-gkm-parquet version --json` reports the expected matrix.

## 5. Tell the producers

- If ClinVar-GKM should move to the new version, open a request (issue or paired PR) in
  `clingen-data-model/clinvar-gkm` to bump the pinned version in its Parquet release script and to update the
  Ecosystem add-on page and, for a rules change, `docs/parquet/tables.md`. This repo doesn't edit a producer's repo.
- Release announcements go to `clinvar-gkm@broadinstitute.org` and GitHub Discussions, through the maintainer.
