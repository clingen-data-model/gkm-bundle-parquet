# gkm-bundle-parquet

Builds queryable **Parquet** tables from a **GKM JSON bundle**, rebuilds the bundle from them, and validates bundles
against their JSON Schema. It works with any bundle built on the GA4GH GKM classes (VRS, Cat-VRS, VA-Spec), given that
bundle's JSON Schema: the tables are generated from the schema, and a column set is needed only for a narrower
projection. It names no producer and has no default bundle. The tests run on
real bundles of two producers: [ClinVar-GKM](https://github.com/clingen-data-model/clinvar-gkm) and
[CIViC](https://github.com/ga4gh/gkm-starter-kit), which differ in layout, version and root values.

The JSON bundles are the public record. Parquet is a derived convenience: a quick way to explore and query the bundle
content with current database tools such as DuckDB, Polars, pandas and Spark. Every Parquet value can be recomputed from
the bundle it came from ([ADR 0001](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/decisions/0001-parquet-derived-from-bundles.md)).

> **Status:** 2.0.0 (planned) is the first release under the name `gkm-bundle-parquet`. It removes what 1.1.0, the last
> `clinvar-gkm-parquet` release, deprecated: the ClinVar-GKM default bundle, its release model and its fixed table specs
> ([ADR 0008](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/decisions/0008-end-state-reversible-default-custom-projections-filters.md),
> [ADR 0009](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/decisions/0009-generic-gkm-bundle-tool-renamed.md)).
> See [Migrating from 1.x](#migrating-from-1x). On `main` the removals and the rename are in.

## Versioning

This tool is versioned **independently** of any bundle schema, because they release on different cadences
([ADR 0002](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/decisions/0002-independent-tool-versioning.md)).
Every release states the table spec it writes, with its version, and the bundles it reads. From 2.0.0 that is one spec,
`sections`, compiled for **any bundle from its schema** and, optionally, a column set (a *pairing*;
[ADR 0006](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/decisions/0006-sections-spec-compiled-from-schemas.md),
[format](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/column-set-format.md)). With the schema alone the tool builds the schema's **default column set**, which it
generates by a fixed, versioned policy (the *policy version*, `2.0`;
[ADR 0007](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/decisions/0007-default-generated-from-bundle-schema.md), [generate](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/generate.md)). A column set must
compile to that default or declare itself a named projection. A pairing that doesn't load or compile is refused with the
reason. The tool lists no producer's `schema_version`s: the producer publishes its schema, and says which of its versions
it reads.

| Tool version | Reads | Table specs written |
| --- | --- | --- |
| 1.0.0 | any bundle with its pairing; ClinVar-GKM by default (snapshot: 1.1.0); `baseline`, `clinvar-gkm`: ClinVar-GKM 1.1.0 | `sections` 1.0 (default; draft), `baseline` 1.0 (draft), `clinvar-gkm` 1.0 (draft) |
| 1.0.1 | as 1.0.0 | as 1.0.0 |
| 1.1.0 | as 1.0.0; the default bundle, `baseline` and `clinvar-gkm` deprecated | as 1.0.0 |
| 2.0.0 (planned) | any bundle with its schema (required) and, optionally, a column set; no default bundle | `sections` 2.0 (draft; generated default, policy 2.0; no `key_order` or `ordinal`; a record-identical round trip) |

`sections`: one table per bundle collection (20 in ClinVar-GKM 1.1.0), keyed by `id`, compiled from a bundle schema and,
optionally, a column set, with every record property its own typed column (named as the property; STRUCTs at any depth;
pointers as bare ids; extensions in `value_<kind>` slots by the JSON kind of their value) and no `data` column. What the
schema can't settle is kept as JSON text, and `generate` lists where. A property, value type or record shape the spec
doesn't cover stops the build. It is lossless
([ADR 0005](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/decisions/0005-lossless-sections-build.md)): a table also has `id_present` where the record's class allows
an `id` but doesn't require it; a pointer with several target sections, an IRI, a string-or-object and an integer-or-Range
have derived parts (`<prop>_section`, `<prop>_json`, `<prop>_min`/`_max`; [format](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/column-set-format.md#derived-parts));
and root values that aren't collections go in a `root_values` table. There is no `ordinal`: rows are in key order
(ADR 0008 decision 1.1). Pointers whose targets the schema declares (a `pattern` of `^#/<section>/`) are listed as
foreign keys. The build checks every row.
`rebuild` writes back a bundle whose records are identical to the source's: the same canonical form (RFC 8785 with arrays
sorted), whatever the source's layout, key order or array order ([docs/round-trip.md](docs/round-trip.md)). This is
2.0.0's promise (planned; on `main`); 1.x rebuilds a line-layout bundle's text byte for byte and any other layout
JSON-equal.

`version --json` gives the same: `tool_version`, `table_spec`, `table_spec_version`, `schema_version_rule`, the key-value
prefix it writes (`kv_prefix`), the DuckDB version and `validate`'s engines.

Each Parquet file records, in its key-value metadata (prefix `gkm_bundle_parquet.`; 1.x wrote `clinvar_gkm_parquet.`),
the tool version, the spec's name and
version, the policy version, whether the column set is the default or a projection (and its name), the table's foreign
keys, the sha256 of the schema set, the column set and the compiled spec, the source bundle's sha256, for a lossless
build its section's records sha256 and, for a selective build, the selection
([design plan](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/plans/2026-10-20-design.md#72-key-value-metadata-every-file)
§7.2). A set also holds its compiled spec (`table-spec.json`), so `verify` and `rebuild` need neither the schema nor the
column set. They also read sets built by 1.x with `sections`, under either key-value prefix.

## Install

```sh
uvx gkm-bundle-parquet@2.0.0 version --json   # run a pinned release with no install (planned)
pipx install gkm-bundle-parquet==2.0.0        # or install it
```

Python 3.10 or later. DuckDB is pinned exactly, because the output bytes depend on it, so an isolated environment
(`uvx` or `pipx`) avoids a clash with your own DuckDB.

`validate --engine python`, the reference validator, needs the `reference` extra (the GA4GH GKM Toolkit, `ga4gh.gkm`
0.4.0, Python 3.11 or later), whose GA4GH dependencies are pre-releases:
`uv tool install --prerelease=allow 'gkm-bundle-parquet[reference]'` or `pip install --pre 'gkm-bundle-parquet[reference]'`.
The default engine needs nothing extra.

## Usage

Every command that reads a bundle takes one bundle (`--full`, a path or URL) and its schema, `--bundle-schema` (a schema
file, a URL, or a schema-set directory). With no `--column-set` the tool builds the schema's generated default, and
`--version-property` names the root property holding the bundle's version, if any; a `--column-set` (the default written
out, or a named projection) says it instead. `--sha256` gives the bundle's expected sha256; without it, a
`<file>.sha256` sidecar is used when there is one. Which release a file is, and which file to read, is the producer's
business. The examples use ClinVar-GKM's 1.1.0 schema, which ClinVar-GKM publishes with its bundles.

```sh
P="--bundle-schema https://w3id.org/clingen/schema/clinvar-gkm/1.1.0/clinvar-gkm-bundle.schema.json \
   --version-property schema_version"

# The schema's default column set, written out, with a report of what the policy settled (docs/generate.md)
gkm-bundle-parquet generate $P -o clinvar-gkm.columns.yaml
# Whether a column set is the default (exit 0) or how it differs (exit 1)
gkm-bundle-parquet generate --check --bundle-schema clinvar-gkm-1.1.0/schema-set --column-set my.columns.yaml

# One table per bundle collection, a typed column per record property
gkm-bundle-parquet build --full clinvar-gkm_2026-1018.json.gz $P --out out/

# A bundle by URL, with its expected sha256
gkm-bundle-parquet build --full https://example.org/bundle.json.gz --sha256 <hex> $P --out out/

# A selection: one submitter and every record that points to it (its SCVs)
gkm-bundle-parquet build --full clinvar-gkm_2026-1018.json.gz $P --out omim/ \
    --driver submitter --where "name = 'OMIM'" --follow in --max-depth 1

# The same selection's mini-bundle only (byte-identical to the one the build used)
gkm-bundle-parquet select --full clinvar-gkm_2026-1018.json.gz $P --out omim.json.gz \
    --driver submitter --where "name = 'OMIM'" --follow in --max-depth 1

# A CIViC bundle: no version, pretty-printed, empty collections and root values
gkm-bundle-parquet build --full civic-assertion-bundle.json --bundle-schema civic/schema-set --out civic/
# The same with a named projection: the column set narrows the default and says `projection: <name>`
gkm-bundle-parquet build --full civic-assertion-bundle.json \
    --bundle-schema civic/schema-set --column-set civic/civic.columns.yaml --out civic-projection/

# The columns a pairing gives, with no build
gkm-bundle-parquet compile-spec $P > sections.yaml

# Back from Parquet to the bundle: a full build rebuilds the bundle's records (no schema needed)
gkm-bundle-parquet rebuild out/ -o clinvar-gkm_2026-1018.json.gz

# Validate every record against the bundle's JSON Schema set (the default engine, jsonschema-rs; docs/validate.md)
gkm-bundle-parquet validate clinvar-gkm-delta_2026-1031.json.gz --bundle-schema clinvar-gkm-1.1.0/schema-set \
    --version-property schema_version --failures bad.jsonl
# The same with the GKM Toolkit's validator, the reference the default is checked against (much slower)
gkm-bundle-parquet validate clinvar-gkm-delta_2026-1031.json.gz --bundle-schema clinvar-gkm-1.1.0/schema-set \
    --version-property schema_version --engine python

# Checks: the directory on its own; a full build against its bundle; a selection against its mini-bundle;
# the round trip (the rebuilt records against the recorded records sha256s and, with --full, the bundle's own)
gkm-bundle-parquet verify out/
gkm-bundle-parquet verify out/ --bundle-recompute --full clinvar-gkm_2026-1018.json.gz $P
gkm-bundle-parquet verify omim/ --bundle-recompute --full omim.json.gz
gkm-bundle-parquet verify out/ --roundtrip --full clinvar-gkm_2026-1018.json.gz
```

`build`, `select`, `compile-spec`, `generate` and `validate` without `--bundle-schema` stop with exit 2 and say so, and so
does a column set that neither compiles to the default nor declares a projection (its differences are listed). `verify`
needs no schema; given `--bundle-schema` (and a `--column-set`, if the build had one), it also checks that they compile to
the spec that wrote the files (the three hashes every file records), and exits 1 if they don't.

`--memory-limit` (`build`, `select`, `rebuild`, `verify`) sets DuckDB's memory limit, e.g. `12GB`. Without it, the limit is
half the memory the process may use: the machine's physical memory, or its cgroup limit when that is lower (a container).
DuckDB's own default, 80% of physical memory, bounds only its buffers and left too little for the rest of the process on
a 32 GB machine. The limit doesn't change the files: a build that spills to `--work-dir` writes the same bytes.

`validate` checks each record alone, as a one-section bundle, so key patterns are checked too, as ClinVar-GKM's validator
does. `--version-property` names the root property holding the bundle's version, which then goes into every record's
one-section bundle; there is no default name (ADR 0009). Without it, a version at the root is validated as a root value. The
root's own `required` (CIViC's lists every root property) is checked once, on the names the whole bundle holds, not on each
record ([docs/validate.md](docs/validate.md)). It exits 1 if any record fails, naming its section, key and instance paths, or
if the bundle lacks a name that `required` lists, and 2 on anything that isn't a verdict on a record (an unreadable bundle,
an unknown section, a missing version, a schema set that doesn't load). Its two engines give the same verdicts on every
record of the ClinVar-GKM 2026-10-31 delta and a sample of the 2026-10-31 full, with the published 1.1.0 schemas and
ClinVar-GKM's narrowed ones, and on the two CIViC bundles;
[docs/validate.md](docs/validate.md) has the evidence, the known differences and the timings.

From 2.0.0 (planned; on `main`), `rebuild` writes the line layout, gzip when the output name ends in `.gz`. The text
need not be the source's (keys come in column order, spacing is the tool's, and array order is not promised); the
records are, and that is checked: each record's canonical form is hashed, and the records sha256s are compared with each
file's `section_records_sha256` and the manifest's `records_sha256` ([docs/round-trip.md](docs/round-trip.md) defines
them). A set built by tool 1.x recorded text sha256s instead: it rebuilds unchecked, and `verify --roundtrip` needs
`--full <bundle>` for it. `rebuild` refuses anything it can't reproduce:
a selection, a build with `--tables` (or, by 1.x, with deltas), a missing table or manifest, a repeated key, a 1.x set's
missing ordinal, a pointer outside its targets ([ADR 0005](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/decisions/0005-lossless-sections-build.md)).

Selection options (`build` and `select`): `--driver <table>`, `--where` (a DuckDB boolean expression over that table as the
spec builds it), `--ids k1,k2` and `--ids-file` (added to the `--where` keys; with neither, every row of the driver table),
`--follow out|in|both` (default `out`), `--max-depth N` and `--sections a,b,c`. A selection reads the bundle's root by the
pairing: its version property, if it has one, and its sections, so a bundle with no version, empty collections, root values
(kept in the mini-bundle) or another JSON layout (read whole) works too. See
[ADR 0004](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/decisions/0004-driver-based-selection.md)
and the [design plan](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/plans/2026-10-20-design.md)
§4.5 and §10.

## Migrating from 1.x

2.0.0 removes what 1.1.0 deprecated. The values of a `sections` build with a pairing don't change. What does: the
key-value prefix (`gkm_bundle_parquet.` for `clinvar_gkm_parquet.`; `verify` and `rebuild` read both), the
`tool_version` entry, the manifest's `builder` and, where 1.1.0 read a release from an option or a published file name,
the `source_release` entry, now always empty; and, with the record-identical round trip
([docs/round-trip.md](docs/round-trip.md)), the table spec version (`sections` 2.0), the column order of the tables a
column set's `key_order` named (now schema order, as every other table), and the recorded sha256s
(`section_records_sha256` and `records_sha256` for the text sha256s). See the [changelog](CHANGELOG.md) for the manifest
fields.

| 1.x | 2.0.0 |
| --- | --- |
| `clinvar-gkm-parquet` (package `clinvar_gkm_parquet`) | `gkm-bundle-parquet` (package `gkm_bundle_parquet`), the same commands |
| `--release`, `--index`, `--delta`, `--manifest`, `--full-source-release` | resolve the release yourself and pass one bundle with `--full`, plus `--sha256` or a `<file>.sha256` sidecar |
| `build`, `select`, `compile-spec` or `validate` without `--bundle-schema` (the built-in ClinVar-GKM default) | give the bundle's schema with `--bundle-schema` (and `--version-property schema_version` for ClinVar-GKM); the tables are the schema's generated default |
| a column set you wrote | `generate --check` it: a set that isn't the default needs `projection: <name>` |
| the `ordinal` column (the record's position) | gone: rows are in key order, and the round trip doesn't depend on order |
| `compile-spec <schema_version>` | `compile-spec --bundle-schema <schema> --column-set <column set>` |
| `--spec sections`, `--spec clinvar-gkm`, `--spec baseline`, a `tables.yaml` as `--spec` | no `--spec`: the `sections` spec compiled from the pairing, and a column set for other tables |
| `verify --spec` | nothing: `verify` reads the spec the build wrote beside its manifest |
| `verify --against` (the oracle comparison) | `verify --bundle-recompute` and `verify --roundtrip` |
| `validate` with the version property `schema_version` by default | `--version-property schema_version` |
| `key_order` (and `sorted_columns`) in a column set | delete it: key order doesn't count, and a column set with it is refused |
| a byte-identical rebuilt text, checked by `source_text_sha256` and `section_text_sha256` | record-identical records, checked by `records_sha256` and `section_records_sha256` |

`verify` and `rebuild` still read `sections` sets built by 1.x. Such a set records no records sha256, so `rebuild` writes it
back unchecked and `verify --roundtrip` needs `--full <bundle>` for it; build it again to record one. Sets built by 1.x
with `baseline`, `clinvar-gkm`, a custom spec or from deltas are refused: check them with the 1.x tool that wrote them.

## Design records

- [Design plan](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/plans/2026-10-20-design.md): the
  history of the ClinVar-GKM-specific design (table spec, inputs and delta replay, engine, CLI, testing, rollout). ADRs 0008
  and 0009 replace large parts of it.
- [Architecture decision records](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/decisions/README.md):
  [0001 — Parquet is derived from the bundles](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/decisions/0001-parquet-derived-from-bundles.md),
  [0002 — Independent tool versioning](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/decisions/0002-independent-tool-versioning.md),
  [0003 — Pluggable table specs](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/decisions/0003-pluggable-table-specs.md),
  [0004 — Driver-based selection](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/decisions/0004-driver-based-selection.md),
  [0005 — A full sections build is lossless](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/decisions/0005-lossless-sections-build.md),
  [0006 — Tables compiled from a bundle schema and its column set](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/decisions/0006-sections-spec-compiled-from-schemas.md),
  [0007 — The default set is generated from the bundle schema](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/decisions/0007-default-generated-from-bundle-schema.md),
  [0008 — The end state](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/decisions/0008-end-state-reversible-default-custom-projections-filters.md),
  [0009 — A generic GKM bundle tool, renamed gkm-bundle-parquet](https://github.com/clingen-data-model/gkm-bundle-parquet/blob/main/docs/decisions/0009-generic-gkm-bundle-tool-renamed.md).

## Contributing

Work on a branch and open a pull request against `main`. Project conventions are in [`CLAUDE.md`](CLAUDE.md)
and `.claude/rules/`.

## License

[CC0 1.0 Universal](LICENSE): dedicated to the public domain.
