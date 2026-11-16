# clinvar-gkm/1.1.0 (ClinVar-GKM's 1.1.0 pairing)

The pairing the suite builds ClinVar-GKM bundles with: the bundle schema set and the column set for ClinVar-GKM bundle
`schema_version` 1.1.0. Up to tool 1.1.0 the tool shipped them as its default bundle (`schemas/1.1.0/` and
`spec/sections/column-sets/1.1.0.columns.yaml`); from 2.0.0 the tool has no default bundle and ships no producer's
pairing (ADR 0009), so they are test fixtures here, moved unchanged. ClinVar-GKM publishes and vendors its own.

- `schema-set/`: the 1.1.0 schema set, written by `scripts/vendor-schemas.py` (`MANIFEST.json` lists every file, its
  `$id`, where it was read from and its sha256). Schema set sha256
  `741a29fcd86f07003f0d27d6c94ba0b414ec8f69fae479cb76cc67c4de47d798`.
- `columns.yaml`: the 1.1.0 column set (docs/column-set-format.md), `version_property: schema_version`. Its `schema` is
  `https://w3id.org/clingen/schema/clinvar-gkm/1.1.0/clinvar-gkm-bundle.schema.json`.

`tests/_fixture.py` names them (`CLINVAR_SCHEMA`, `CLINVAR_COLUMNS`, `PAIRING`).

## The root `$id`: this fixture against what w3id serves

The fixture's root schema carries the **corrected** `$id`,
`https://w3id.org/clingen/schema/clinvar-gkm/1.1.0/clinvar-gkm-bundle.schema.json` (tool PR #11), read from
`clingen-data-model/clinvar-gkm` at `4c8ecf3` (the `url` in `MANIFEST.json`). w3id serves the **original** root, whose
`$id` is `https://w3id.org/clingen/schema/clinvar-gkm/1.1.0/json/ClinvarGkmBundle`. That one line is the only difference:
the other 94 files are byte-identical, and every `$ref` in the root is absolute (checked 2026-11-16).

| | Root sha256 | Schema set sha256 |
| --- | --- | --- |
| This fixture | `80ebd72a3e2aa1153572d8e29a8d8eeea4748574cb07d96ab8573378f829a2cd` | `741a29fcd86f07003f0d27d6c94ba0b414ec8f69fae479cb76cc67c4de47d798` |
| Served by w3id | `48369c69648459b1e138a1f59c1a349742f5e0b93a93c68ccc9df77b8b73ede2` | `03409226627844411de48d31b4fd873e2f2c42baa4256441a86c7b2d8ae55ef6` |

The maintainer chose to leave the served set as it is (2026-11-16). The opt-in network test
(`tests/test_schemas.py::test_fetching_1_1_0_by_url_gives_the_fixture_set_but_the_root_id`, `pytest -m network`) checks
exactly this. Because `columns.yaml` names the corrected `$id`, the served set paired with this column set is refused
by the pairing check; pass this fixture's `schema-set/` with it instead.
