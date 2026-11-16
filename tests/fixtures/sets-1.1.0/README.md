# sets-1.1.0 (Parquet sets written by tool 1.1.0)

Parquet sets as tool 1.1.0, still named `clinvar-gkm-parquet`, wrote them: every file's key-value metadata has the old
prefix `clinvar_gkm_parquet.`. From 2.0.0 the tool writes `gkm_bundle_parquet.`, and `verify` and `rebuild` still read
both (ADR 0009 decision 3). `tests/test_legacy_sets.py` checks that they do. The files are as written; don't regenerate
them with a newer tool.

## clinvar-gkm-excerpt

A full `sections` build of `tests/fixtures/real-1.1.0/clinvar-gkm_excerpt.json.gz` (ClinVar-GKM 1.1.0), with the 1.1.0
schema set and column set now in `tests/fixtures/clinvar-gkm/1.1.0/`. Built on 2026-11-16 by tool 1.1.0 (commit
`0bf179a`, branch `feat/1.1.0-deprecations`) from a `git archive` of that commit, in its root:

```sh
clinvar-gkm-parquet build --full tests/fixtures/real-1.1.0/clinvar-gkm_excerpt.json.gz \
    --bundle-schema schemas/1.1.0 --column-set spec/sections/column-sets/1.1.0.columns.yaml \
    --out <dir>/clinvar-gkm-excerpt
```

20 tables (331 rows), `table-spec.json` and `manifest.json` (`builder` `clinvar-gkm-parquet 1.1.0`).
