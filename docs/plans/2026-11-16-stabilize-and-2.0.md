# Stabilize, then 2.0.0 — Work Plan

> **Status: agreed with the maintainer, 2026-11-16.** This plan orders the remaining work of
> [ADR 0008](../decisions/0008-end-state-reversible-default-custom-projections-filters.md) and
> [ADR 0009](../decisions/0009-generic-gkm-bundle-tool-renamed.md) (both accepted). The ADRs say what the tool becomes;
> this plan says in what order, what is done, and what each step waits on. Where they differ, the ADRs win.
>
> **Evidence base.** The code inventory was read at `main` @ `ad3a4eb` (2026-11-16); line numbers below are at that
> commit. ClinVar-GKM facts were read from `clingen-data-model/clinvar-gkm` at `1.0` @ `8343986` and `main` @ `3160f08`
> the same day.

## 1. Goal

First a **stable state**: no stale PRs, branches or worktrees; the docs match the accepted ADRs; `main` is what PyPI
serves. Then **2.0.0**, the release that makes the tool generic and renamed, which ClinVar-GKM waits on to publish
Parquet from it. Additive features come after 2.0.0.

## 2. Decisions (maintainer, 2026-11-16)

1. **PR #13 (ADR 0007) is closed as superseded.** It predates ADR 0008: byte-identical round trip, `key_order`, an
   `ordinal` column in every table and required narrowing. A revised ADR 0007 is written from `main` (Phase 1).
2. **ADR 0008 decision 1.2 keeps its wording** on string escapes and number spelling. It is implied by the canonical form
   of decision 1.3, and making them count would need stored text, which 1.3 rules out.
3. **1.1.0 is PR #17**, the last release under the name `clinvar-gkm-parquet`: `validate` (merged in #15, unreleased),
   selection on any pairing, `validate` with no version, and deprecation warnings for everything 2.0.0 removes.
   - It is tagged without a full-size bundle run. The before/after evidence on the real 1.1.0 excerpt and CIViC gives
     126 identical outputs, apart from the `tool_version` key-value entry.
   - The rename warning prints on every run, `--help` included, with no switch to turn it off.
4. **2.0.0 holds the breaking changes and the generator:** the record-identical round trip, the schema-generated
   default, the removals and the rename. Filters both ways and projections follow as 2.x minors.
5. **ClinVar-GKM's `sections` publishing hold** (named in clinvar-gkm PR #217) lifts when the default set is generated
   from the schema and round-trips record-identical, that is, with 2.0.0.

## 3. Phase 0 — stable state

| Item | State |
| --- | --- |
| ADR 0008 accepted | Done, #14 (`2f8f012`) |
| ADR 0009 accepted; links point at the renamed repository | Done, #16 (`3797c36`) |
| GitHub repository renamed `gkm-bundle-parquet` | Done, 2026-11-16 |
| PyPI: old project's trusted publisher names the renamed repository; pending publisher for `gkm-bundle-parquet` | Done by the maintainer, 2026-11-16; the next release's publish step proves it |
| PR #13 closed; merged branches and worktrees removed | Done |
| CI: docs-only PRs skip tests, superseded PR runs cancelled, `pytest -n auto` | Done, #18 (`61bc14c`) |
| Test ids made stable under `-n auto` (gzip `mtime=0`) | Done, #19 (`ad3a4eb`) |
| 1.1.0: rebase #17 on `main`, CI, review, merge, release (`release-tool` skill) | In progress |
| GCS: the two frozen oracle reference Parquet folders (`gs://clinvar-gkm/oracle/1.1.0-2026-10-18/`, `…/1.1.0-2026-10-24/`) | Maintainer to decide: delete or keep. The reports and the key-order measurement stay |
| `transport.py`: closing a stream whose request never opened raises `'_HttpStream' object has no attribute '_resp'` (a warning in the test run) | To do, a small fix |

**Stable point:** 1.1.0 on PyPI, from `main`, with nothing else open.

## 4. Phase 1 — ADR 0007, revised

A new ADR 0007 on a branch from `main`, written on top of ADRs 0008 and 0009:

- keeps #13's generation policy (one table per collection, columns from the schema) and its pointer rule (a `pattern`
  of `^#/<section>/` names the target, ADR 0008 decision 1.5);
- takes ADR 0008's round trip (1.2, 1.3), no `ordinal` (1.1) and optional narrowing (1.4);
- names no producer: ClinVar-GKM and CIViC are examples.

**Done:** [ADR 0007](../decisions/0007-default-generated-from-bundle-schema.md) accepted 2026-11-16 (#22), before the generator is
built (Phase 3, step 2).

## 5. Phase 2 — 1.1.0, the last 1.x

PR #17. After it ships, 1.x gets fixes only. Two things it doesn't include:

- **A default DuckDB memory limit** (the out-of-memory gap of the 2026-11-06 oracle run; `build.py:149-150` sets one only
  with `--memory-limit`). It applies to any producer, so it goes into 2.0.0.
- **`--full` builds ignoring the default line index** (the other oracle gap, `build.py:1248-1257`). It is in the release
  model that 2.0.0 removes, so it isn't fixed.

## 6. Phase 3 — 2.0.0, `gkm-bundle-parquet`

In this order. Steps 1 and 3 can run in parallel; the rename goes last, because it touches every file and would conflict
with every open branch.

1. **Record-identical round trip** (ADR 0008 decisions 1.2, 1.3). Today the check compares bytes (`roundtrip.py:291-302`)
   and text sha256s (`roundtrip.py:11-16, 492-560, 739`).
   - Each record's canonical form: RFC 8785, with arrays put in canonical form and sorted. A per-record sha256, and a
     records sha256 for the bundle in the key-value metadata and the manifest.
   - The build's row check, `rebuild` and `verify --roundtrip` compare canonical forms and the records sha256.
   - `key_order` goes (`columnset.py:20`, `spec.py`, `compiler.py:574-627`, `roundtrip.py:171`), and so do the text sha256s.
2. **The default generated from the schema** (ADR 0007 revised, ADR 0008 decisions 1.1, 1.4, 1.5).
   - A `generate` command; a build with `--bundle-schema` needs no column set (today `build.py:1272` refuses that).
   - No `ordinal` (`compiler.py:591`).
   - The compiler handles what it stops on today (`compiler.py:21-23`): string-or-object positions, a Range at any depth.
   - The generic `#/` rule: a bare id plus `<prop>_section`.
   - Foreign keys by schema `pattern`, written to `table-spec.json` and each file's key-value metadata; `generate`
     lists pointer positions without a pattern.
   - This is table spec `sections` 2.0.
3. **Removals** (ADR 0008 decision 2, ADR 0009 decisions 2 and 3).
   - The release model: `sources.py` (line index, aliases, pools, frozen manifests, `resolve_release`), `replay.py`,
     `ChainError`, the release parts of `model.py`, `build.py` and `verify.py`, and the options `--release`, `--index`,
     `--delta`, `--manifest` and `--full-source-release`. One bundle with `--sha256` or a `.sha256` sidecar stays
     (`sources.py:177-203, 243-250`).
   - The default producer: `default_pairing.py`, `spec/sections/default-bundle.yaml`; `schemas/1.1.0/` and the 1.1.0
     column set move to `tests/fixtures/`. Every command requires `--bundle-schema`.
   - The `clinvar-gkm` spec and its 22 SQL files, `baseline`, a hand-written `tables.yaml` as input, `verify --against`
     and the oracle code. The compatibility matrix in `compat.py` and `version --json` is reworked.
4. **Rename** (ADR 0009 decision 3): package, console script, cache directory, User-Agent, key-value prefix, mini-bundle
   name and manifest `builder`. About 330 occurrences in some 60 files. `verify` and `rebuild` read the old
   `clinvar_gkm_parquet.` prefix as well as the new one.
5. **Docs, changelog and matrix**, then the release, through the pending publisher.

**To fix before 2.0.0** (found by #17, not blocking 1.1.0):

- **w3id serves a 1.1.0 schema set whose sha256 differs from the shipped snapshot** (`0340922…` against `741a29f…`; the
  network test fails on 1.0.1's code too). 2.0.0 drops the snapshot, and ClinVar-GKM plans to pass w3id URLs, so the
  served set must be the right one. Look into it right after 1.1.0.
- **`validate` fails every CIViC record.** It checks each record as a one-section bundle, and CIViC's root schema
  requires all 19 root properties. The fix changes validation, so it reruns the engine-equivalence evidence
  ([validate.md](../validate.md)).

## 7. Phase 4 — ClinVar-GKM switches (its repository)

This repository doesn't write to ClinVar-GKM's; these are its steps, listed so the order is clear.

- **clinvar-gkm PR #217** (draft) builds Parquet with `gkm-bundle-parquet` 2.0.0 in a new release stage 6. It comes out
  of draft when 2.0.0 is on PyPI. With the generated default it needs no `--column-set`.
- The hold (section 2, decision 5) lifts, and releases stop passing `--skip-parquet`.
- **#216** repoints its docs links to the new name; **#205** writes the Ecosystem → Add-ons page that summarises and links
  here.
- **#213** (sorted keys and canonical alias order, for byte-exact replays) is no longer needed for the tool's round trip,
  which ignores key and array order. **#204** (a column set published per schema tag) is only needed for custom column
  sets once the default is generated.
- **#215** (narrowed profiles with pointer patterns) is optional for the round trip. It gives declared foreign keys and
  fewer columns, and arrives as a new schema version.

## 8. Phase 5 — 2.x

- Filters both ways (ADR 0008 decision 4): `--follow none`, a filtered build reversible to its mini-bundle, filter
  options on `rebuild`.
- Projections (ADR 0008 decision 3).
- The move of the generic core to the GKM Toolkit, when its maintainer asks (ADR 0006).
