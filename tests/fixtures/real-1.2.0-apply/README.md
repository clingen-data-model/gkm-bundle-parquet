# real-1.2.0-apply (real bundle excerpts, for `apply`)

A small base, upserts bundle and deletes file cut from two consecutive ClinVar-GKM 1.2.0 weekly deltas, so that
`tests/test_apply.py` can prove `apply` on real records (ADR 0011): building `base.json.gz` and applying
`upserts.json.gz` and `deletes.json` must give the tables a build of the replayed bundle gives.

| File here | Cut from | Release | Source sha256 (equals the published one) |
| --- | --- | --- | --- |
| `base.json.gz` | `https://data.clinvar-gkm.org/v1/1.2/deltas/clinvar-gkm-delta_2026-1108.json.gz` (an ordinary bundle, read as the base) | 2026-11-08 | `c98b5b0c0b583fa311c6efc3be4eb9281e922afeab77bc1e0c0421b4c54cb605` |
| `upserts.json.gz` | `https://data.clinvar-gkm.org/v1/1.2/deltas/clinvar-gkm-delta_2026-1115.json.gz` | 2026-11-15 | `b370d3857ffd709bf80ab6b9e81d8ef6f4e2bbbab8303df0fdb49547d3a353e3` |
| `deletes.json` | the `deleted` lists of `https://data.clinvar-gkm.org/v1/1.2/deltas/clinvar-gkm-delta_2026-1115.manifest.json`, `jq '.sections \| map_values(.deleted)'` | 2026-11-15 | manifest `83717ab0043950e7714a78c50dd20a03901b3e786ca0e8b5fb731beb76dbd321` (read 2026-11-23) |

Every record is copied byte for byte from its source line. The schema is ClinVar-GKM's published 1.2.0 set,
`tests/fixtures/clinvar-gkm/1.2.0/schema-set/`. Per section, up to two keys of each kind, in key order: kept (in the base
only), replaced (in both deltas; every one of them changed), deleted (in the base and listed as deleted on 2026-11-15) and
added (in the upserts only). `condition` is left out of the base, so the upserts bundle adds a root member, and
`conditionSet` holds only its deleted record, so `apply` empties a section; `copyNumberCount` is in the base only. 44 base
records, 33 upserts and 11 deletes, in 15 sections; `PROVENANCE.json` lists the counts, the keys picked and every file's
sha256.

Made with (the sources read locally, the deletes derived with `jq` as above):

```sh
jq '.sections | map_values(.deleted)' clinvar-gkm-delta_2026-1115.manifest.json > deletes-1115.json
uv run python scripts/excerpt-apply-fixture.py clinvar-gkm-delta_2026-1108.json.gz \
    clinvar-gkm-delta_2026-1115.json.gz deletes-1115.json tests/fixtures/real-1.2.0-apply \
    --per-kind 2 --omit-from-base condition --take conditionSet=0,0,1,0
```

then `PROVENANCE.json` was written by hand from the sources and the script's `selection.json`.
