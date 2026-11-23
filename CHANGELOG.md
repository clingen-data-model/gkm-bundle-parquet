# Changelog

All notable changes to this tool. Each release states the table-spec versions it writes and what it reads: through
1.x, the bundle `schema_version`s; from 2.0.0, any bundle with its schema, and the policy version and key-value prefixes
too (ADR 0002, ADR 0006, ADR 0009). Tags are `vX.Y.Z`.

## Unreleased

Planned as 2.1.0 (a new feature; no table-spec change: `sections` 2.0, policy 2.0).

**New**

- **`apply`: bring a built set up to date from a small delta** (ADR 0011, proposed). `apply BASE --upserts BUNDLE
  [--deletes DELETES.json] --bundle-schema S [--column-set C | --version-property V] --out DIR` writes the set a build of
  the replayed bundle gives: per section, the base's records less the deleted keys and the upserted keys, plus every
  record of the upserts bundle (deletes first, then upserts), root values replaced by name. The inputs are generic: a
  full lossless set of tool 2.x (default or projection, or an earlier `apply`), an ordinary bundle of the same pairing and
  version, and a JSON object `{"<section>": ["<key>", ...]}`; the release model stays the producer's (ADR 0009). It
  refuses a pairing that doesn't compile to the base's spec, an upserts bundle of another version, a deleted key the base
  doesn't hold, a selection, and a set built by 1.x. Each touched table is rehashed and checked against the base's and the
  upserts bundle's recorded records sha256s; untouched tables are copied. Evidence: on ClinVar-GKM's 2026-11-08 delta as
  the base, with the 2026-11-15 delta and its deletes applied, all 20 tables equal a build of the JSON replay (1,815,952
  rows; equal records sha256s), in 33.5 s at 3.0 GB peak on an M2 Pro; the suite proves it on an excerpt of those deltas
  (`tests/fixtures/real-1.2.0-apply/`, with ClinVar-GKM's published 1.2.0 schema set as a new fixture) and on CIViC's
  bundles, a chain of two applications included.
- **Key-value metadata and manifest of an applied set:** `source_bundle_sha256` is empty (no one file holds the state),
  every file carries the new key `applied` (the chain of applications: each step's base records sha256, base source
  bundle sha256, upserts bundle sha256 and records sha256, deletes file sha256 and resulting records sha256), and the
  manifest repeats it with `inputs.base` (directory, manifest sha256, records sha256, files), `inputs.upserts` and
  `inputs.deletes`. A build's files are unchanged.

**Changed**

- **`verify --roundtrip --full B` on an applied set** compares the records only, since no file holds the state: any bundle
  with the same records passes. For a build it still checks `B`'s sha256 against `source_bundle_sha256`. `rebuild` and the
  other checks treat an applied set as a build.
- `version --json`: `schema_version_rule` mentions `apply`.
- `scripts/excerpt-apply-fixture.py` (new) cuts a small, consistent base, upserts bundle and deletes file from real
  bundles, for tests.

## 2.0.0 (2026-11-19)

The first release under the name `gkm-bundle-parquet` (ADR 0009), and the one that implements ADR 0007 and ADR 0008's
round trip. It removes everything 1.1.0 deprecated (plan Phase 3). It names no producer and has no default
bundle: every command that reads a bundle to build, select, compile or validate takes one bundle and its schema
(`--bundle-schema`), and the tables are the schema's **generated default** unless a column set says otherwise.

What it reads and writes (`version --json`): any bundle given its schema, and, with a column set, only a column set that
compiles to the default or names a projection; no producer's `schema_version` is listed. It writes table spec
`sections` **2.0** (`released`), whose rules are the default policy **2.0**, under the key-value prefix
`gkm_bundle_parquet.`; `verify` and `rebuild` also read the sets tool 1.x wrote with `sections`, under
`clinvar_gkm_parquet.` or the new prefix. Pins DuckDB 1.5.4.

The removals alone don't change the output otherwise. With the ClinVar-GKM 1.1.0 pairing, on the real excerpt, the CIViC
fixture, a selection and the 2026-10-31 delta (the newest published), `table-spec.json`, the mini-bundles, `compile-spec`
and the rebuilt bundles were byte-identical to 1.1.0's, and so was every Parquet file but for the `tool_version` entry
and, for a file whose name 1.1.0 read a release from, `source_release` (below). The record-identical round trip then
changes the recorded sha256s, the rebuilt text, the table spec version and the column order of the tables 1.1.0's
column set listed under `key_order`; the values don't change. The generated default then makes the column set optional
and changes the tables' columns: no `ordinal`, `id_present`, the generic IRI rule, and JSON text where the schema can't
settle a position. The rename then changes every key-value key, so every file's bytes.

**Breaking changes**

- **Renamed `gkm-bundle-parquet`** (ADR 0009 decision 3, #30). Every name the tool carries changes:

  | | 1.x | 2.0.0 |
  | --- | --- | --- |
  | PyPI project, console script | `clinvar-gkm-parquet` | `gkm-bundle-parquet` |
  | Python package | `clinvar_gkm_parquet` | `gkm_bundle_parquet` |
  | Key-value prefix (every entry of every file) | `clinvar_gkm_parquet.` | `gkm_bundle_parquet.` |
  | Cache directory | `$XDG_CACHE_HOME/clinvar-gkm-parquet/` | `$XDG_CACHE_HOME/gkm-bundle-parquet/` |
  | User-Agent | `clinvar-gkm-parquet/<version>` | `gkm-bundle-parquet/<version>` |
  | Manifest `builder` | `clinvar-gkm-parquet <version>` | `gkm-bundle-parquet <version>` |
  | Selection mini-bundle | `clinvar-gkm_selection.json.gz` | `gkm-bundle-parquet_selection.json.gz` |

  The new prefix changes every file's bytes. `verify` and `rebuild` read either prefix (below), so sets written by 1.x
  stay checkable and rebuildable; a file with entries under both is refused. The commands and their options keep their
  names.
- **Every command that reads a bundle needs its schema** (ADR 0009 decision 1, #24). `build`, `select`, `compile-spec`,
  `generate` and `validate` stop with exit 2 without `--bundle-schema`, saying there is no default bundle; `verify` and
  `rebuild` need none (they read the spec a build wrote beside its manifest), and `verify` takes one optionally (below).
  `--bundle-schema` is a schema file, a URL (such as a w3id `$id`) or a schema-set directory (README, "Schema sources").
  Removed: the default producer (`default_pairing.py`, `spec/sections/default-bundle.yaml`), ClinVar-GKM's 1.1.0 schema
  snapshot (`schemas/1.1.0/`) and column set (`spec/sections/column-sets/1.1.0.columns.yaml`), which are now test
  fixtures (`tests/fixtures/clinvar-gkm/1.1.0/`), and `compile-spec <schema_version>`.
- **The default column set is generated from the bundle schema** (ADR 0007, #28; [docs/generate.md](docs/generate.md),
  [docs/column-set-format.md](docs/column-set-format.md)). With `--bundle-schema` and no `--column-set`, `build`,
  `select`, `compile-spec` and `verify` take the schema's default, generated by a fixed policy (policy version `2.0`);
  `--version-property` names the root's version property for it. The table spec is `sections` 2.0.
  - **`generate`** (new): `generate --bundle-schema S [--version-property V] [-o FILE]` writes the default column set
    and reports, on stderr, every IRI position with no pointer pattern and every JSON-text position with its reason;
    `generate --check --bundle-schema S --column-set C` exits 0 if `C` compiles to the default (an equal
    `table_spec_sha256`) and 1 listing the differences.
  - **A column set must be the default or a named projection** (ADR 0007 decision 3, ADR 0008 section 3): any other
    set must say `projection: <name>`, or it is refused (exit 2) with its differences. The manifest's `table_spec` and
    each file's key-value metadata record `policy_version`, `column_set_kind` (`default` or `projection`) and
    `projection`. A projection is compiled and checked as before; its projected-bundle round trip is later work.
  - **No `ordinal` column** (ADR 0008 decision 1.1): rows are written in key order, and `rebuild` writes records in
    key order (a 1.x set keeps its ordinals). `rebuild` refuses a table with a repeated key.
  - **`id_present`** (BOOLEAN, after `id`) where the record's class allows `id` but doesn't require it, so a record
    without its id round-trips. Where the class requires it, it must equal the key, or the build stops.
  - **Every allowed property is a column or STRUCT field at any depth**; a property set to `false` has none.
  - **The generic IRI rule**: `format: iri-reference` with no pattern is `<prop>` (the bare id of a
    `#/<collection>/<rest>` value, or the string as written) and `<prop>_section`. Not a foreign key. ClinVar-GKM 1.1.0's
    `Coding.iris` become `STRUCT(id, section)[]` (from `VARCHAR[]`).
  - **A list of multi-target pointers** is one `STRUCT(id, section)` per element, not the parallel `<prop>` and
    `<prop>_section` lists (ClinVar-GKM 1.1.0: `variation.members`, `conditionSet.concepts`,
    `evidenceLine.hasEvidenceItems`). In general an element with derived parts is one STRUCT of them (`id`, `string`,
    `json`, `section`, `integer`, `min`, `max`).
  - **What stopped the compile now has a form**: a string-or-object is `<prop>` plus `<prop>_json`; an integer-or-Range
    is known by its shape at any depth (`<prop>`, `_min`, `_max`); a multi-target pointer may sit inside a value;
    extensions are known by shape (a closed `{name, value, ...}`), with a `value_json` slot for an open value and the
    element's other fields kept; anything else the schema can't settle (an open object, a map, a union of scalars, a
    keyword the rules can't read, a class that reaches itself) is JSON text. A derived name equal to a property name at
    the same level stops the compile. Column-set shapes `json` (at any depth), `iri`, `range` and
    `<string shape>+json` (`->a|b+json`, `iri+json`, `string+json`) state these where the schema leaves them open.
  - Measured on the 2026-10-31 delta (495,547 records): the published 1.1.0 schema's default is 20 tables, 339 columns
    (43 of them JSON text), 0 foreign keys; the narrowed profiles' (`tests/fixtures/schema-sets/`) is 20 tables, 163
    columns (none JSON text), 26 foreign keys. Both round-trip to the same records sha256 as the source, pass
    `verify --bundle-recompute`, and build byte-identical twice. Build time, two runs each on a loaded shared machine
    (load average 17 to 34), so rough: 36 s and 32 s from the published schema, 33 s and 30 s from the narrowed one,
    against 30 s and 25 s for `main` with the 1.1.0 column set (about 1.2x to 1.3x).
- **The round trip is record-identical, not byte-identical** (ADR 0008 decisions 1.2 and 1.3, #26;
  [docs/round-trip.md](docs/round-trip.md)). A rebuilt record must have the same canonical form as its source record:
  RFC 8785 (JCS) with every array sorted by its elements' canonical UTF-8 bytes (a multiset), numbers by their IEEE-754
  double value, strings by their decoded text (no Unicode normalization). Key order, array order, whitespace, escapes
  and number spelling no longer count, for any source layout; a changed value still does. The build's row check
  compares each row's record sha256 with its source record's; `rebuild` and `verify --roundtrip` compare records
  sha256s, not text sha256s.
  - Removed: the text sha256s, `section_text_sha256` (each file) and `source_text_sha256` and `source_text_size` (the
    manifest), replaced by the records sha256s (Additions). The manifest's `round_trip.promise` is `record-identical`
    for every layout.
  - `rebuild` writes the line layout with keys in column order and records in key order; it prints the records sha256
    instead of a text sha256.
  - `key_order` and `sorted_columns` are gone: a column set with `key_order` is refused, naming the fix; columns are in
    schema order in every table, so a bundle the general reader normalizes compiles to the same spec as one in the
    line layout. The `sections` spec is now **2.0** (a major change: the 7 tables that had `key_order: columns` in the
    1.1.0 pairing reorder their columns, so a reader by column position sees a new table; by name, nothing changes).
    The other columns, types and values are unchanged. The 1.1.0 column set (`tests/fixtures/clinvar-gkm/1.1.0/`) loses
    its `key_order` block, so its sha256 (`column_set_sha256`) changes.
  - **Sets built by tool 1.x** (they record `section_text_sha256` and no `section_records_sha256`): still read. Their
    `table-spec.json` keeps `key_order`, which is ignored. `rebuild` writes them back and says the result is not checked
    (nothing is recorded to check it against; the text sha256s are not compared, since the text is laid out by 2.0.0's
    rules); `verify --roundtrip` refuses them unless `--full <bundle>` is given, which compares their records with the
    bundle's. Rebuild such a set with 2.0.0 to record its records sha256.
  - Measured 2026-11-16 on the 2026-10-31 delta (495,547 records, 17 sections), on a shared machine (load average
    about 4), so the timings are rough: build 19 s against 12 s for the removals alone (about 1.6x: the row check
    canonicalizes every record and row in Python), `rebuild` 11 s against 10 s, `verify --roundtrip --full` 14 s. The
    records sha256 matched an independent recomputation (stdlib json and hashlib) of the source, the rebuilt
    bundle and the manifest, and two builds gave byte-identical files. On a full release (2026-11-18, the 2026-10-31
    full relabelled to ClinVar-GKM schema 1.2.0, 91,854,927 records, 20 tables, 4.3 GB of Parquet; an n2-highmem-16
    VM, 128 GiB, the default memory limit 64,411 MiB): build 38 min 23 s at a peak RSS of 65.0 GiB,
    `verify --bundle-recompute` 9 min 24 s (0 differ), `verify --roundtrip` 29 min 43 s, `rebuild` 20 min 21 s (its
    records sha256 equal to the manifest's), `validate --engine rs` 10 min 48 s, all passing.
- **The release model is removed** (ADR 0009 decision 2, #24): `--release`, `--index`, `--delta`, `--manifest` and
  `--full-source-release` (on `build`, `select`, `verify` and `validate`), and the line index, aliases, pools, frozen
  manifests, delta replay (`replay.py`), `ChainError` and the delta-manifest code behind them. Pass one bundle with
  `--full <path|url>`, with `--sha256 <hex>` or a `<file>.sha256` sidecar. Which release a file is, and which file to
  read, is the producer's business.
- **The fixed specs and the oracle are removed** (ADR 0008 decision 2, #24): `--spec` (and with it `--spec baseline`,
  `--spec clinvar-gkm`, `clinvar-gkm-1.0`, `clinvar-gkm@1.0`, `--spec sections` and a hand-written `tables.yaml` as
  `--spec`), the `baseline` and `clinvar-gkm` specs with their 22 SQL files, `verify --against` and the BigQuery-oracle
  comparison code. `verify` and `rebuild` refuse a set 1.x built with one of them (or from deltas), naming the spec:
  check it with the 1.x tool that wrote it.
- **`validate` has no default version property** (ADR 0009 decision 4, #24): 1.x used `schema_version` unless the given
  schema's root didn't declare it. Pass `--version-property schema_version` for ClinVar-GKM bundles to validate each
  record with the bundle's version, as before. Without it, a version at the root is validated as a root value.
- **The deprecation warnings go with what they named**, the old name's with the rename.
- **Manifest** (`manifest.json`):
  - `builder` is `gkm-bundle-parquet <version>` (above);
  - `note` names the bundle (one), not "the ClinVar-GKM JSON bundle(s)";
  - `source_release` and `release` are always `null` (the tool doesn't know which release a file is; a producer that
    publishes the manifest fills them);
  - `source_deltas` and `inputs.deltas` are removed;
  - `inputs.full` loses `release` and `source_release`;
  - `records_sha256` replaces `source_text_sha256` and `source_text_size` (above);
  - `table_spec` gains `policy_version`, `column_set_kind` and `projection`.
- **Key-value metadata:** every key under `gkm_bundle_parquet.` (above); `section_records_sha256`, `policy_version`,
  `column_set_kind`, `projection` and `foreign_keys` are new, `section_text_sha256` is gone. `source_deltas` is always
  `[]` and `source_release` always empty, kept so the entries stay the same. 1.1.0 filled `source_release` from
  `--release`, `--full-source-release` or a published ClinVar-GKM file name (`clinvar-gkm-delta_2026-1031.json.gz` gave
  `2026-10-31`), so such a file's bytes change; the data don't.
- **`version --json`** is the 2.0.0 compatibility matrix: `tool_version`; `table_spec` (the one spec, `sections`),
  `table_spec_version` and `policy_version` (both `2.0`); `schema_version_rule` (what it reads); `kv_prefix` (the
  prefix it writes) and `kv_prefixes_read` (`gkm_bundle_parquet.` and `clinvar_gkm_parquet.`); `duckdb_version`; and
  `validate_engines`. Removed: `table_specs`, `default_table_spec`, `snapshotted_schema_versions`, `tables` and
  `table_spec_status`, which described the built-in specs. `version` prints the spec and policy versions, the prefixes
  and the rule.

**Additions**

- **`generate`**, the generated default and named projections (above).
- **The records sha256** (ADR 0008 decision 1.3, #26; [docs/round-trip.md](docs/round-trip.md)): each file's
  `gkm_bundle_parquet.section_records_sha256` (the sha256 of the sorted lines `<key> TAB <record sha256>`) and the
  manifest's `records_sha256` (the sha256 of the sorted lines `<section> TAB <section records sha256>` plus
  `$ TAB <root sha256>`). It depends only on the records, so two layouts of one bundle have the same one, and anyone can
  recompute it with a JSON parser and sha256.
- **Foreign keys from the schema's `pattern`** (ADR 0007, ADR 0008 decision 1.5, #28): a string whose `pattern` is
  `^#/<section>/` or `^#/(a|b)/` is a foreign key at any depth (the bare id, plus `<prop>_section` with several
  targets). Another `^#/` pattern, or one naming something that isn't a collection, stops the compile. Foreign keys
  (from patterns and from a column set's `->` shapes) are listed per table in `table-spec.json` (`foreign_keys`:
  `position`, `targets` and, with several targets, `section`) and in each file's key-value metadata (`foreign_keys`,
  JSON).
- **`table-spec.json` is `sections` 2.0**: the compiled spec beside the manifest names the policy version, the column
  set's kind and projection, and each table's foreign keys, and has no `key_order`, `sorted_columns` or `ordinal`.
  `verify` and `rebuild` read it, so they need neither the schema nor the column set.
- **Reading the old key-value prefix** (ADR 0009 decision 3, #30): `verify` (the directory check,
  `--bundle-recompute` and `--roundtrip`) and `rebuild` read a file's entries under `gkm_bundle_parquet.` or
  `clinvar_gkm_parquet.`, so `sections` sets written by 1.x stay checkable and rebuildable. A build writes only the new
  prefix. `version --json` lists both (`kv_prefixes_read`).
- **A default DuckDB memory limit** (plan §5, #24): without `--memory-limit`, every `build`, `select`, `verify` and
  `rebuild` connection sets DuckDB's `memory_limit` to half the memory the process may use, the machine's physical
  memory or its cgroup limit (v2 `memory.max`, else v1) when that is lower. DuckDB's own default, 80% of physical
  memory, bounds only its buffers and left too little for the rest of the process on a 32 GB machine (the 2026-11-06
  oracle run). When no size is known, DuckDB keeps its own default. The limit doesn't change the files' bytes.
- **`verify` checks a pairing** (#24): given `--bundle-schema` (with `--column-set` if the build had one, else the
  generated default), it checks that they compile to the spec that wrote the files: the three hashes every file
  records. A difference is a problem (exit 1); `--column-set` without `--bundle-schema` is a usage error (exit 2).

**Fixes**

- **`validate` no longer fails every record of a bundle whose root schema `required`s its collections** (#27; CIViC's
  requires all 19 root properties). Each record is still validated alone, as a one-section mini-bundle, but against the
  root schema without its own `required`, every other keyword kept, in both engines; that `required` is then checked
  once, on the names the bundle holds, and each missing name is one failure under that name with no key. Both CIViC
  fixtures now pass (each of their 30 and 23 items failed before), with both engines. ClinVar-GKM's roots have no
  `required`, so its verdicts don't change; the engine-equivalence evidence was rerun ([docs/validate.md](docs/validate.md)).
- **Closing an HTTP stream whose first request failed** (an HTTP error, or giving up after the retries) no longer
  raises `'_HttpStream' object has no attribute '_resp'` (#23); it showed as an ignored exception when the stream was
  garbage-collected (`transport.py`).
- **`verify --roundtrip --work-dir <dir>`** creates the directory when it doesn't exist (1.1.0 failed; #24).

**Other changes**

- **`sections` 2.0 is released, not a draft**: a compiled spec's `status` (in `table-spec.json` and the manifest's
  `table_spec_status`) is `released`. `table_spec_sha256` leaves `status` out, so no hash and no Parquet file changes;
  a set built from `main` before the release says `draft`.
- The test fixtures' column sets (ClinVar-GKM 1.1.0 and CIViC) narrow the generated default, so they now declare
  themselves projections (`clinvar-gkm-producer`, `civic-fixture`); their `column_set_sha256`s change. The golden
  `sections` spec test compares with ADR 0007's changes applied (no `ordinal`, `id_present`, the generic IRI rule,
  one STRUCT per multi-target pointer element).
- The producer-freedom check (`tests/test_any_bundle.py`) covers the whole package, every module and `sql/_macros.sql`:
  no module names a producer, its sections or its URLs; the one exception is `compat.py`'s old key-value prefix. Three
  macros only the removed specs' SQL used are removed. The package description no longer names ClinVar-GKM.
- The opt-in network test of the 1.1.0 schema set checks what w3id serves: the fixture's files but the root, whose `$id`
  is the original `.../1.1.0/json/ClinvarGkmBundle` (the fixture carries the corrected one; the maintainer leaves the
  served set as it is, 2026-11-16). Paired with the 1.1.0 column set, which names the corrected `$id`, the served set is
  refused; pass the fixture's set (`tests/fixtures/clinvar-gkm/1.1.0/README.md`, and the README's "Schema sources").
- Scripts: `bench-tables.py`, `capture-spec.py`, `make-baseline-spec.py`, `render-spec-readme.py` and
  `extract-fixtures.py` (ClinVar-GKM-specific; the fixtures' provenance names it, so run it from the 1.1.0 source) are
  removed. `sample-bundle.py` takes `--bundle-schema` and `--version-property`; `validate-equivalence.py` takes
  `--version-property` instead of `--index`; `bench-scale.py` has no `--deltas`.

**Migration (1.x → 2.0.0)**

Command by command (`S` is the bundle's schema, `F` the bundle, `V` its version property, if it has one, such as
ClinVar-GKM's `schema_version`):

| Command | 1.x | 2.0.0 |
| --- | --- | --- |
| install, run | `uvx clinvar-gkm-parquet@1.1.0 <command>`; `import clinvar_gkm_parquet` | `uvx gkm-bundle-parquet@2.0.0 <command>`; `import gkm_bundle_parquet` |
| `build` | `build --full F --out O` (the built-in ClinVar-GKM pairing) | `build --full F --bundle-schema S [--version-property V] --out O` (the generated default) |
| `build`, a release | `build --release R` or `--index I` | resolve the release yourself: `build --full <url> --sha256 <hex>` (or its `.sha256` sidecar) `--bundle-schema S ...` |
| `build`, deltas | `build --full F --delta D --manifest M` (replay) | build from the one bundle you want to read; the tool reads one |
| `build`, a fixed spec | `build --spec baseline`, `--spec clinvar-gkm`, `--spec my-tables.yaml` | no `--spec`: the `sections` spec of the pairing; a column set with `projection: <name>` for other tables |
| `build`, a release label | `--full-source-release R` | nothing: the manifest's `source_release` and `release` are `null` for the producer to fill |
| `select` | `select --full F --driver D ...` | `select --full F --bundle-schema S [--version-property V] --driver D ...` |
| `compile-spec` | `compile-spec 1.1.0` | `compile-spec --bundle-schema S [--version-property V] [--column-set C]` |
| `generate` | (none) | `generate --bundle-schema S [--version-property V] -o C`; `generate --check --bundle-schema S --column-set C` |
| `verify` | `verify DIR [--spec ...]` | `verify DIR`: it reads the spec the build wrote; optionally `--bundle-schema S [--column-set C]` to check a pairing |
| `verify`, the oracle | `verify DIR --against REF` | `verify DIR --bundle-recompute --full F` and `verify DIR --roundtrip [--full F]` |
| `verify`, a release | `verify DIR --bundle-recompute --release R` / `--index I` / `--delta D` | `verify DIR --bundle-recompute --full F [--sha256 <hex>]` |
| `rebuild` | `rebuild DIR -o F`: byte-identical text, checked by text sha256s | the same command: record-identical records, checked by records sha256s; a 1.x set rebuilds unchecked |
| `validate` | `validate F` (the built-in ClinVar-GKM schema; `schema_version` by default) | `validate F --bundle-schema S [--version-property V]` |
| `validate`, a release | `validate F --index I` | `validate F --sha256 <hex> --bundle-schema S ...` |
| `version --json` | `table_specs`, `default_table_spec`, `snapshotted_schema_versions`, `tables`, `table_spec_status` | `table_spec`, `table_spec_version`, `policy_version`, `kv_prefix`, `kv_prefixes_read` |

Column sets, tables and files:

| 1.x | 2.0.0 |
| --- | --- |
| a column set you wrote (required) | optional: `generate --check` it; if it isn't the default, add `projection: <name>` |
| `key_order` (and `sorted_columns`) in a column set | delete it: key order doesn't count, and a column set with it is refused |
| the `ordinal` column | none: rows are in key order |
| `<prop>` and `<prop>_section` lists of multi-target pointers | one `STRUCT(id, section)` per element in `<prop>` |
| `Coding.iris` (ClinVar-GKM) as `VARCHAR[]` | `STRUCT(id, section)[]`: the generic IRI rule |
| `source_text_sha256`, `section_text_sha256` (byte-identical text) | `records_sha256`, `section_records_sha256` (record-identical records; docs/round-trip.md) |
| key-value prefix `clinvar_gkm_parquet.` | `gkm_bundle_parquet.` (both read by `verify` and `rebuild`) |
| the ClinVar-GKM 1.1.0 schema snapshot and column set in the package | ClinVar-GKM's own published schema (or `tests/fixtures/clinvar-gkm/1.1.0/` in a source tree) |
| a `sections` set built by 1.x | still read by `verify` and `rebuild`; build it again to record its records sha256 |
| a `baseline`, `clinvar-gkm`, custom-spec or delta-replay set built by 1.x | refused: check it with the 1.x tool that wrote it |

## 1.1.0 (2026-11-16)

The last release under the name `clinvar-gkm-parquet` (ADR 0009 decision 3): it deprecates what 2.0.0 removes. Reads and
writes the same as 1.0.1: bundle `schema_version` 1.1.0 snapshotted, table specs `sections` 1.0 (the default), `baseline`
1.0 and `clinvar-gkm` 1.0, all `draft`, and DuckDB 1.5.4. The table specs don't change, and neither does the output: on
the real 1.1.0 excerpt and the CIViC fixtures, every file of a default `sections` build (and of `baseline`, `clinvar-gkm`,
selective and CIViC builds) is byte-identical to 1.0.1's but for the `tool_version` key-value entry, and the
mini-bundles and a rebuilt bundle are byte-identical.

- **Deprecations** (ADR 0009 decision 5, ADR 0008 decision 2), removed in 2.0.0. Each use still works as before and prints
  one line on stderr, `clinvar-gkm-parquet: deprecated: ...`, naming the replacement; output files, exit codes and stdout
  don't change, and there is no switch to silence them. See the README's Deprecations section.
  - the name `clinvar-gkm-parquet`, said on every run (including `--help` and a usage error): from 2.0.0 the tool is
    `gkm-bundle-parquet` (https://github.com/clingen-data-model/gkm-bundle-parquet), with the same commands;
  - `--release`, `--index`, `--delta`, `--manifest` and `--full-source-release` (on `build`, `select`, `verify` and
    `validate`): resolve the release yourself and pass one bundle with `--full` and `--sha256`, or a `<file>.sha256`
    sidecar;
  - `build`, `select` and `validate` without `--bundle-schema`, which read the built-in default bundle: give the bundle's
    schema (and, for `build` and `select`, its column set);
  - `--spec clinvar-gkm` (and `clinvar-gkm-1.0`, `clinvar-gkm@1.0`), `--spec baseline` and a `tables.yaml` as `--spec`:
    the default `sections` spec with a pairing, and a column set for other tables;
  - `verify --against`: `verify --bundle-recompute` and `verify --roundtrip`;
  - `compile-spec <schema_version>`: `compile-spec --bundle-schema ... --column-set ...`.
- **`version --json`** marks each built-in spec `deprecated` (`baseline` and `clinvar-gkm`), and `schema_version_rule`
  says what is deprecated; `version` prints `deprecated` beside them.
- **Selection on any pairing** (ADR 0008 decision 4.1, ADR 0009 decision 5): `select` takes `--bundle-schema` and
  `--column-set`, and `build --driver` now accepts them (it refused them). With a pairing, the selection reads the root by
  the pairing's rules instead of a fixed `schema_version` line and the default bundle's sections: the version property is
  the column set's (none, or optional), and the mini-bundle writes it only when the source has one; an empty collection
  (`"x": {}`) is read; a root value that isn't a collection is kept in the mini-bundle, verbatim, after its sections; and a
  bundle in another JSON layout is read whole, as the canonical line layout (its records key-sorted), with the file's
  sha256. A pairing with `--delta` is refused. Without a pairing, the mini-bundles are byte-identical and a bundle not in
  the line layout is still refused; the selection's reader now reads an empty collection line, as the build's already did,
  and names a root value it can't place as an unknown section. Selection follows `#/<section>/<key>` pointers as before, so a pointer
  into part of a record (CIViC's `#/variant/<key>/protein/<id>`) is reported as dangling.
- **`validate` with no version** (ADR 0009 decision 5): when `--bundle-schema` is given, `--version-property` isn't, and the
  schema's root declares no `schema_version`, the bundle is validated with no version instead of stopping (exit 2, "no
  schema_version"); `version: (none)` is printed. Without `--bundle-schema`, nothing changes. On the CIViC fixtures it now
  runs, and every record fails on exactly one thing: CIViC's root `required` lists all 19 of its root properties, and a
  record is checked alone, as a one-section bundle (docs/validate.md).
- **`validate`:** checks every record of a bundle against its JSON Schema set, each as a one-section bundle with the
  bundle's version, as ClinVar-GKM's `validate-bundle.py` does. Engine `rs` (the default) runs on jsonschema-rs 0.61.1, a
  new pinned dependency; engine `python` is the GA4GH GKM Toolkit's `validate_bundle_schema` (`ga4gh.gkm` 0.4.0, the new
  `reference` extra). The two agree on every record of the 2026-10-31 delta and a sample of the 2026-10-31 full, with the
  published 1.1.0 schemas and ClinVar-GKM's narrowed ones, and on every generated negative case
  ([docs/validate.md](docs/validate.md)). `version --json` lists the engines (`validate_engines`). Table specs, accepted
  `schema_version`s and output bytes don't change.

## 1.0.1 (2026-11-03)

Reads and writes the same as 1.0.0: bundle `schema_version` 1.1.0 snapshotted, table specs `sections` 1.0 (the default),
`baseline` 1.0 and `clinvar-gkm` 1.0, all `draft`, and DuckDB 1.5.4. Like 1.0.0, it comes before the oracle and any build on
a full monthly bundle.

- **The 1.1.0 column set and schema snapshot use the corrected bundle schema `$id`**,
  `https://w3id.org/clingen/schema/clinvar-gkm/1.1.0/clinvar-gkm-bundle.schema.json`, the one on ClinVar-GKM's `main` and in
  the GKM Toolkit's copy, which now pair with the shipped column set. The original `$id` is no longer accepted with it, so
  pairing the file w3id serves for 1.1.0 (it still declares the original) with that column set is refused. A default
  1.1.0 `sections` build has the same tables, rows and `table_spec_sha256`; `schema_set_sha256` and `column_set_sha256`
  change, and with them the files' bytes. Sets built by 1.0.0 still `verify` and `rebuild`.

## 1.0.0 (2026-11-03)

First release. Snapshots bundle `schema_version` 1.1.0, and `baseline` and `clinvar-gkm` read that version; `sections` reads
any bundle with its schema and column set. Writes the table specs `sections` 1.0 (the default), `baseline` 1.0 and
`clinvar-gkm` 1.0. Pins DuckDB 1.5.4.

**Released before the oracle, by the maintainer's decision (2026-11-03).** No build has run on a full monthly bundle, and
`clinvar-gkm` hasn't been compared with ClinVar-GKM's BigQuery export. All three specs stay `draft`, so their tables can
still change, and ClinVar-GKM's published Parquet set is still its BigQuery export. The largest real input so far is the
2026-09-23 weekly delta (4,530,457 records), which rebuilt byte-identical.

- First tool code (design plan §12, step 1): package scaffold, CLI (`build`, `verify`, `version --json`),
  table spec 1.0 (draft), streaming bundle reader, delta replay, DuckDB table build, `verify`.
- Reads bundle `schema_version` 1.1.0; writes table spec 1.0 (draft until captured from the 1.1.0 export).
- Streaming build engine (design plan §6.2): the full is read once, section by section, and each section's
  tables are built in a worker thread while the next section is read. There is no state database: the deltas
  are replayed in the stream from an in-memory change map, and only the current sections are staged.
  `verify --bundle-recompute` streams the same way. The Parquet files are byte-identical to the first engine's.
  New refusals: a full whose sections are out of the assembler's order. A full with no published sha256 is
  hashed in a first pass (its tables' metadata names it).
- Table specs on the CLI (ADR 0003): `build --spec <name|path>` and `verify --spec`. **Behaviour change:** with
  no `--spec`, `build` now writes the built-in `baseline` spec (20 tables: `id`, `refs`, `data`); the 22-table
  layout of the published set is `--spec clinvar-gkm-1.0` (also `clinvar-gkm`, `clinvar-gkm@1.0`). Every file
  gains the key-value entry `clinvar_gkm_parquet.table_spec` (plus `table_spec_builtin` and `table_spec_sha256`
  for a custom spec), and the manifest a `table_spec` object. That entry is the only byte change to a
  `clinvar-gkm` build: on the real excerpt, with and without deltas, every table's rows equal the previous
  build's, and the old key set gives byte-identical files. `verify` reads the spec from the files; a custom spec
  needs `--spec <file>` with the recorded sha256. A spec may now read a subset of the bundle sections; the other
  sections are still read, checked and counted. `version` and `version --json` list the built-in specs
  (`table_specs`, `default_table_spec`).
- Selective builds (ADR 0004): `build --driver <table>` with `--where`, `--ids`, `--ids-file`, `--follow`,
  `--max-depth` and `--sections`, and a new `select` command that writes only the mini-bundle. `--where` is a
  DuckDB expression evaluated on the driver table as the chosen spec builds it, on the replayed state when
  deltas are given. Every file and the manifest of a selective build carry `clinvar_gkm_parquet.selection`;
  the source keys still name the published bundles. `verify --bundle-recompute` checks a selection against its
  mini-bundle (`--full`, whose sha256 must equal `selected_sha256`), and `verify --against` refuses a selection
  or a set not built with `clinvar-gkm`. A build with no driver never runs the selection engine.
- Python API: `build.build()` called without `spec` now writes the default spec, as the CLI does (it wrote
  `clinvar-gkm`). Pass the `clinvar-gkm` spec for the published layout.
- Faster table SQL (design plan §6.2): each record is parsed once and each row checked by a node census before
  the parsed values are trusted, with the original expressions as the fallback. The output and every fail-loud
  message are unchanged. Synthetic benchmark (`scripts/bench-tables.py`): all 22 tables 3.8× faster, `variation`
  4.6×, `scv` 3.9×; for a full-size build, an estimated 31 → 8 minutes of table SQL.
- Custom typed tables (ADR 0003): a table with no shipped `sql/<table>.sql` is built by a generic renderer from
  its derivation kinds alone (`key`, `record`, `refs`, `scalar-path`, `bare-id`, `pointer-section`,
  `typed-copy`, `extension-slot`), so a user's `tables.yaml` can map any section to typed tables. Paths take
  `[*]` list steps (`$.contributions[*].contributor`) and are checked when the spec loads; a `computed` column
  or a row filter still needs a shipped SQL file, and is refused at load otherwise. The spec format gains two
  optional keys: a typed copy's `pointers` (pointer positions stripped to bare ids) and a table's
  `known_fields` (every top-level field must be read). `clinvar-gkm` 1.0 (draft) now states both; its output is
  unchanged, and the generic SQL of its 14 generic tables is byte-identical to the shipped files'.
- **Behaviour change: the default spec is now `sections`** (ADR 0003, amended 2026-10-29): with no `--spec`,
  `build` (and `build.build()`) writes 20 tables, one per bundle section, keyed by `id`, with every record
  property its own typed column and no `data` column. `--spec baseline` writes the previous default. The
  built-in `sections` 1.0 spec is a draft (a file then; compiled since ADR 0006, entry below).
- Spec format additions: derivation kinds `range-exact`, `range-min` and `range-max` (a VRS integer-or-Range: the
  integer, else the Range's bounds; any other value stops the build); extension slots by JSON type
  (`extension_slots: {<column>: json-type}`: each extension fills the `value_<type>` slot of its value's JSON
  type, at every depth; a type with no slot, a null value or a mixed-type array stops the build); a table may
  have no `record` column (at most one). A shipped `sql/<table>.sql` now renders only the `clinvar-gkm` table it
  implements; another spec's table of the same name takes the generic renderer.
- Every `known_fields` table now also checks that the record's own `id`, when present, equals its bundle key, and
  stops the build when it doesn't. `clinvar-gkm` output is unchanged (byte-identical on the real excerpt).
- **Lossless round trip** (ADR 0005): a full build of the `sections` spec from one bundle is a lossless encoding
  of the bundle's uncompressed text. New command `clinvar-gkm-parquet rebuild <dir> -o <bundle.json[.gz]>` writes
  the text back byte for byte and checks it against the hashes the build recorded; `verify --roundtrip` does the
  same without writing (with `--full`, also against that bundle's text). Both refuse a selection, a build with
  deltas or `--tables`, a missing table or manifest, ordinals that are not `0..n-1`, a pointer section outside its column's
  targets, and any spec that is not lossless; a differing hash leaves no output. Measured 2026-10-30 on the
  2026-09-23 weekly delta (4,530,457 records, 2.68 GB of text): rebuilt text identical (sha256 `fb247dc2…`),
  build 41.2 s / 3.5 GB peak RSS (29.9 s / 2.8 GB before the round-trip check), rebuild 40.0 s / 2.0 GB.
- **`sections` 1.0 (draft) changed in place** (it is unreleased): every table gains `ordinal` (BIGINT, last: the
  record's position in its bundle section); each bare-id column with several targets gains a `<column>_section`
  column right after it (`variation.members_section`, `conditionSet.concepts_section`, `object_section` in the
  four proposition tables, `propositionSomaticClinicalImpact.conditionQualifier_section`,
  `evidenceLine.hasEvidenceItems_section`, `proposition_section` in `scv`, `vcv` and `rcv`); an absent list inside
  a typed value is now NULL (was `[]`), and an extension's unused list slots NULL (were `[]`), so `[]` is always
  an empty list. The spec states its round trip: top-level `lossless: true`, a `json` block per table, `targets`
  on every bare-id column, and typed-copy `pointers` as a mapping from position to target section.
- The `sections` build now writes each row back as JSON and stops unless it equals the bundle line, key text
  included: a JSON `null`, a float, an open Range `[null, null]`, a record without its `id`, a key order or escape
  the spec doesn't state now stop a `sections` build (none occurs in the excerpt or the delta). A lossless build
  also writes `clinvar_gkm_parquet.section_text_sha256` in every file and `source_text_sha256` and
  `source_text_size` in the manifest. `baseline` and `clinvar-gkm` output is unchanged (byte-identical on the real
  excerpt).
- Spec format additions: derivation kind `ordinal`; `targets` on bare-id and pointer-section columns; `pointers`
  may map each position to its target sections; a table's `json` block (`key_order: columns | sorted`,
  `record_id`, `sorted_columns`); a spec's `lossless` flag. Staging: the build's chunk lines start with the
  record's ordinal, and `bundle_<section>` gains `ordinal` and `key_json` (the key as written).
- Tests only: the provisional fixture `tests/fixtures/examples-1.1.0/` and `scripts/make-example-fixtures.py`
  are removed. The real 1.1.0 excerpt `tests/fixtures/real-1.1.0/` is the suite's only bundle fixture;
  `CLINVAR_GKM_FIXTURE=<folder>` still runs the suite on another excerpt (the `provisional` and `real` values
  are gone), and the skips and strict xfails for the provisional fixture's shapes are removed. No tool
  behaviour changes.
- **The compile-and-build path is generic: any bundle plus its schema and column set** (ADR 0006; unreleased). `build
  --bundle-schema <path|url|dir> --column-set <path|url> --full <bundle>` compiles a table spec for any producer's bundle,
  and `compile-spec` shows the result. A column set ([`docs/column-set-format.md`](docs/column-set-format.md)) is required
  and authoritative: it states the columns, and a schema's `$id` pairs with it. No ClinVar-GKM name or URL is in that path
  (a test enforces it). Proved on two real CIViC bundles, whose schema has a URN `$id`, with no version, pretty-printed,
  with empty collections and root values: rebuilt JSON equals the source, `metadata` included.
- **The default bundle is a data file**, `spec/sections/default-bundle.yaml`. With no flags, a build reads the bundle's
  `schema_version` from its first lines and takes that version's pairing: the shipped snapshot `schemas/1.1.0/` with
  `spec/sections/column-sets/1.1.0.columns.yaml`, else the published URLs, fetched once and cached. On 1.1.0 the compiled
  tables equal the previous file's (names, types, nulls, derivations) with no listed difference, except column and field
  order in the 13 key-sorted tables, which is now schema order; rows are equal per column by name, and `rebuild` gives
  the same text (2026-09-23 weekly delta: sha256 `fb247dc2…`). `spec/sections/1.0/tables.yaml` moved to
  `tests/fixtures/sections-golden/`, the golden the compiler must reproduce; a build doesn't load it.
- **Behaviour change: no fixed list of bundle versions for `sections`.** A version is accepted when its pairing loads and
  compiles, and refused with the reason otherwise. `baseline`, `clinvar-gkm` and a custom spec keep the versions they list;
  a custom spec's sections are checked against those versions' schema sets, so one listing an unsnapshotted version reads or
  fetches its schemas when it loads. A bundle's collections and their order come from its schema in the reader, replay, the
  selection engine and `rebuild`. A delta manifest's sections are checked when its delta is read, because a manifest states
  no version. Error text changes: an unknown version's message gives the reason.
- **Two readers, chosen per file by its first lines.** The line layout is streamed by the strict grammar, as before, and now
  also takes `"<collection>": {}` and root values on one line, and a file with no version line under a pairing. Any other
  JSON is read whole (in memory), every record's keys sorted, a duplicate key or a non-JSON number refused. A plain `.json`
  file works as well as gzip. `clinvar-gkm`, `baseline` and selections read only the line layout.
- **The round trip states its promise.** The manifest gains `round_trip` (`promise`: `byte-identical` | `json-equal`,
  `source_layout`) and `source_root` (the root's members, in order). `rebuild` and `verify --roundtrip` state which holds, and
  a file in another layout is written back in the canonical line layout.
- **Root values** (`metadata`, say) are kept in a `root_values` table (`name`, `value` as JSON text), which exists only when a
  bundle has any and which `rebuild` writes back. An empty collection is a zero-row table, written back as `{}`.
- **New value kinds in a compiled spec:** a float is a DOUBLE column or `value_number` slot, its text confirmed by the row
  check; `json` keeps a record property as JSON text (derivation kind `json-text`), for a map whose keys are data or a
  position that is a pointer in one record and an object in another; `prop as name` renames a column, which replaces the
  tool's built-in `loc_` rename. Two `allOf` declarations of one property now both hold: a profile narrows its base.
- **New key-value entries on a compiled build:** `table_spec_sha256` (of what affects the tables: not `description`,
  `transform`, `sources` or `status`), `schema_set_sha256` and `column_set_sha256`, also in the manifest's `table_spec`, and
  its compiled spec beside the manifest as `table-spec.json`. `verify` and `rebuild` read that file and refuse a hash that
  differs, naming the tool that wrote the set; they don't recompile or need the schemas. So a `sections` set built before
  this change must be rebuilt (it was an unreleased draft). `clinvar-gkm` and `baseline` output is byte-identical on the
  real excerpt.
- New command `compile-spec <schema_version>` or `compile-spec --bundle-schema ... --column-set ...`: writes the compiled spec
  as YAML, with the hashes in a comment. `scripts/vendor-schemas.py <schema> <directory>` writes a schema set a build can read
  with no network.
- **`version --json` changes:** `accepted_schema_versions` is removed, because it can no longer be a closed list.
  `schema_version_rule` states the rule and `snapshotted_schema_versions` lists what works offline. Each `table_specs`
  entry gains `compiled`, and the compiled entry has `tables_by_schema_version` instead of `tables`. The other keys keep
  their meaning. `spec.resolve_spec("sections")` needs the bundle's version; `build.resolve_spec_for` takes it from the
  inputs.
