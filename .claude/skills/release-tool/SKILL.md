---
name: release-tool
description: Cut a clinvar-gkm-parquet tool release and update the compatibility matrix. Use when the user asks to release, tag or publish a new version of the tool, or to add support for a new bundle schema_version.
---

# Cut a tool release

The release process below is the one proposed in the design plan (`docs/plans/2026-10-20-design.md`, §9 and
§10). Where the implementation differs, follow the implementation and fix this skill in the same PR.
Every step that publishes (tag push, PyPI, GitHub Release) needs the user's explicit OK.

## 1. Decide the version

- Read `version --json` on `main` and the changelog since the last tag.
- Tool version (semver):
  - **MAJOR**: a breaking CLI change, a table-spec MAJOR, or dropping a `schema_version` a fixed spec lists or a
    snapshot;
  - **MINOR**: a fixed spec listing a new `schema_version`, a new snapshot, a table-spec MINOR, or a new feature
    (`sections` reads a new `schema_version` with a published column set and no release, ADR 0006);
  - **PATCH**: a fix that doesn't change the table spec (including a fix that makes output conform to it).
- Table-spec version: changes only if the release ships a new `spec/<version>/` or new `sections` rules (see
  `.claude/rules/table-spec.md`).
- Snapshot the schema sets the release was tested with: `scripts/vendor-schemas.py <schema URL> schemas/<version>`,
  and their column sets in `spec/sections/column-sets/<version>.columns.yaml`.

## 2. Verify before tagging

- `uv sync --locked`, `uv run ruff check`, `uv run ruff format --check` and `uv run pytest` pass (what CI and
  `release.yml` run).
- For every `schema_version` the release snapshots or a fixed spec lists, run `build` then `verify` on a **real published bundle**
  of that version (a monthly full, plus a delta chain if replay changed), and record the commands and results.
- If the table spec or the build logic changed: run `verify --bundle-recompute` and `verify --roundtrip` on a real
  bundle (the BigQuery oracle gates nothing since ADR 0008). Every difference is fixed or listed and accepted by the user.
- Run the build twice and compare file sha256s: the same inputs must give byte-identical files.
- If the release claims unchanged output, compare the files of the same builds (default `sections`, the fixed specs, a
  selection, a CIViC pairing) from the previous release and this one: they differ only in the `tool_version` key-value
  entry (replace the old version string with the new one in the old file's bytes and compare).

## 3. Update the compatibility matrix (one PR)

- The version in `pyproject.toml`, and `uv lock` (the lock records it; CI runs `uv sync --locked`).
- The package's compatibility data (`compat.py`, the source of `version --json`).
- The table in `README.md`: one row per tool release, with the snapshotted `schema_version`s, what the fixed specs
  list, and the table-spec versions it writes.
- `CHANGELOG.md`: a `## X.Y.Z (YYYY-MM-DD)` heading (`release.yml` copies the section under it into the GitHub Release),
  the date, accepted `schema_version`s, table-spec version, and every change, including what was under `## Unreleased`.
- The design plan's matrix (§9.2) and the README's install pins and status line.
- Merge only with the user's OK.

## 4. Tag and publish (after the user's OK)

- Before the first release from the renamed repository (`clingen-data-model/gkm-bundle-parquet`, ADR 0009), the PyPI
  project's trusted publisher must name that repository and `release.yml`; the maintainer changes it on PyPI.
- Tag the merge commit on `main` as `vX.Y.Z` (`release.yml` checks it against the package version) and push the tag.
- The release workflow builds the sdist and wheel and publishes them (PyPI via trusted publishing, plus a
  GitHub Release with the changelog entry). Check that the published version installs in a clean environment
  and that `clinvar-gkm-parquet version --json` reports the expected matrix.

## 5. Tell ClinVar-GKM

- If ClinVar-GKM should move to the new version, open a request (issue or paired PR) in
  `clingen-data-model/clinvar-gkm` to bump the pinned version in its Parquet release script and to update the
  Ecosystem add-on page and, for a spec change, `docs/parquet/tables.md`. This repo doesn't edit ClinVar-GKM.
- Release announcements go to `clinvar-gkm@broadinstitute.org` and GitHub Discussions, through the maintainer.
