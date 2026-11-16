# clinvar-gkm/1.1.0 (ClinVar-GKM's 1.1.0 pairing)

The pairing the suite builds ClinVar-GKM bundles with: the bundle schema set and the column set for ClinVar-GKM bundle
`schema_version` 1.1.0. Until 1.1.0 the tool shipped them as its default bundle (`schemas/1.1.0/` and
`spec/sections/column-sets/1.1.0.columns.yaml`); from 2.0.0 the tool has no default bundle and ships no producer's
pairing (ADR 0009), so they are test fixtures here, moved unchanged. ClinVar-GKM publishes and vendors its own.

- `schema-set/`: the published 1.1.0 schema set, written by `scripts/vendor-schemas.py` from
  `https://w3id.org/clingen/schema/clinvar-gkm/1.1.0/clinvar-gkm-bundle.schema.json` (`MANIFEST.json` lists every file,
  its `$id` and sha256). Schema set sha256 `741a29fcd86f07003f0d27d6c94ba0b414ec8f69fae479cb76cc67c4de47d798`.
- `columns.yaml`: the 1.1.0 column set (docs/column-set-format.md), `version_property: schema_version`.

`tests/_fixture.py` names them (`CLINVAR_SCHEMA`, `CLINVAR_COLUMNS`, `PAIRING`).
