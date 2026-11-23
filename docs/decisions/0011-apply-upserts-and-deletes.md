# 0011 — `apply`: bring a built set up to date from an upserts bundle and a list of deleted keys

- **Status:** Proposed
- **Date:** 2026-11-23
- **Plan (if any):** none. Amends [ADR 0009](0009-generic-gkm-bundle-tool-renamed.md) decision 2; builds on
  [ADR 0005](0005-lossless-sections-build.md) and [ADR 0008](0008-end-state-reversible-default-custom-projections-filters.md)
  decisions 1.2 and 1.3 (the record-identical round trip and the records sha256).

## Context

A consumer holding a Parquet set built from release N wants release N+1. Today the only way is a full build of N+1's
bundle. For ClinVar-GKM that is a 4.9 GB gzip file of about 92 M records: the maintainer's release builds of November 2026
needed about 65 GiB of memory, and tool 1.0.1 took about 17.5 minutes and 29 GB peak on an `e2-standard-8` (ADR 0008,
2026-11-06). A weekly delta builds in seconds on a laptop: on an Apple M2 Pro (16 GB) on 2026-11-23, the 2026-11-15 delta
(5.4 MB, 39,062 records) built in 9.3 s at 1.7 GB peak, and the 2026-11-08 delta (60.8 MB, 1,779,702 records) in 33 s at
2.9 GB peak.

Tool 1.x replayed deltas (`--delta`, `--manifest`): it read ClinVar-GKM's delta manifests, applied their chain rule and
wrote the replayed state. ADR 0009 decision 2 moved that out of the tool, because the delta and manifest formats, the
file names and the chain rule are one producer's release model. So the tool must not learn a producer's delta format
again. What a delta does to a bundle is generic, though: some records are added or replaced, some keys are removed. A
ClinVar-GKM delta bundle is an ordinary bundle of the same schema that holds exactly the added and changed records
(`gkm-bundle-parquet validate` passes every one of the 2026-11-15 delta's 39,062 records against the published 1.2.0
bundle schema, and `build` builds it with that schema; 2026-11-23), and its manifest
lists the deleted keys per section (`.sections.<section>.deleted`).

Two more facts shape the decision:

- **Every table is one row per record.** A compiled `sections` table reads one section and has exactly one key column
  (`spec.check_compiled`); a lossless spec has exactly one table per section with no row filter
  (`roundtrip.lossless_problems`); the build checks that a table's rows are its section's keys. A record's nested
  arrays and objects are STRUCT and LIST values in its row. So there are no child or join tables whose rows have to be
  traced back to a record: replacing a record's row replaces the record whole. The only other table, `root_values`,
  holds the root's non-collection members by name.
- **A set's identity is its records sha256** (ADR 0008 decision 1.3), not a file's sha256. Any bundle with the same
  records, whatever its layout, key order or array order, has the same records sha256, and `rebuild` checks against it.

## Decision

### 1. A command, `apply`, with generic inputs

```sh
gkm-bundle-parquet apply BASE_DIR --upserts BUNDLE [--sha256 HEX] [--deletes DELETES.json] \
    --bundle-schema SCHEMA [--column-set COLUMNS] [--version-property NAME] --out OUT_DIR \
    [--work-dir DIR] [--memory-limit SIZE] [--threads N]
```

- **The base** is a set this tool wrote: a full lossless build by 2.x (the default set or a projection), or an earlier
  `apply`. It is read, never changed. A selection, a build with `--tables`, a set built by tool 1.x (which records no
  records sha256) and a set whose manifest disagrees with its files are refused, as `rebuild` refuses them.
- **The upserts** are an ordinary bundle of the base's schema and version, given as `build --full` takes one (a path or
  URL, `--sha256` or a `<file>.sha256` sidecar). It holds every record to add or replace, and may hold root values.
- **The deletes** are a JSON object mapping a section to the keys to remove: `{"<section>": ["<key>", ...]}`. JSON
  because a key may hold any character, because it is what producers' manifests already are, and because one `jq` line
  derives it from one (ClinVar-GKM: `jq '.sections | map_values(.deleted)' clinvar-gkm-delta_YYYY-MMDD.manifest.json`).
  A section the spec doesn't have, a key that isn't a string and a key listed twice are refused. `--deletes` is
  optional.
- **The pairing** (`--bundle-schema`, and `--column-set` or `--version-property`) is required, as for `build`, and must
  compile to the base set's spec: its `table_spec_sha256`, `schema_set_sha256` and `column_set_sha256` must equal the
  ones the base records, or `apply` stops naming the differences. A projection is applied with its own column set.
- **The version must match.** The upserts bundle's version (the spec's version property, read before anything is built)
  must equal the base's `bundle_schema_version`, or `apply` stops. This is the line-change trap: ClinVar-GKM's first 1.2
  delta starts from the same baseline release date as the last 1.1 one, but a 1.2 delta is not a delta of a 1.1 set.
  Across lines, build the new line's full.

### 2. What it writes: the set of the replayed bundle

The output is the set a full build of the **replayed bundle** writes, where the replayed bundle is:

- per section, the base's records, less the deleted keys and the keys the upserts bundle holds, plus every record of
  the upserts bundle: deletes first, then upserts, so a key in both ends as the upserts bundle's record;
- the base's root values, each replaced by the upserts bundle's value of the same name, plus the upserts bundle's new
  ones;
- the root members in the base's order, then those the upserts bundle adds. A section emptied by the deletes stays a
  member, as `{}`.

Every deleted key must be in the base, or `apply` stops (naming up to five): a delete that misses is the sign of a base
that isn't the release the deletes were listed against. Upserted keys need no such check (an absent one is an add).

Each table is written as `ORDER BY` its sort key with the spec's writer settings, as `build` writes it, so the same
inputs give byte-identical files. A table neither input touches is copied row for row. `root_values` is written from
the merged values, and `table-spec.json` is the base's compiled spec.

### 3. Provenance

- **Each file's key-value metadata** is a build's, with three differences: `source_bundle_sha256` is empty (no one file
  holds the state); `section_records_sha256` is the replayed bundle's section records sha256; and a new key, `applied`,
  holds the chain of applications as compact JSON, one step per `apply`:
  `{"base_records_sha256", "base_source_bundle_sha256", "upserts_sha256", "upserts_records_sha256", "deletes_sha256",
  "records_sha256"}`. A step's base source sha256 is the bundle the base was built from, or empty when the base was itself
  applied, so the first step names the original bundle and each later step's base is the previous step's result.
- **The manifest** is a build's, with `source_bundle_sha256` empty, `records_sha256` and `source_root` of the replayed
  bundle, the same `applied` chain, and `inputs` naming the base set (its directory, its manifest's sha256, its records
  sha256, its source bundle sha256 and its files with their sha256s), the upserts bundle (as a build records its input,
  plus its records sha256 and section counts) and the deletes file (its source, sha256, size and keys per section).

### 4. How the records sha256s are derived, and checked

The section records sha256 is a sha256 of every record's line, so it can't be updated in place: every row of a touched
table is hashed once, as `rebuild` hashes it. For each table the deletes or the upserts touch:

1. the merged table is written (base rows less the removed keys, plus the upserts rows);
2. every row of it is written back as its record and hashed; with the removed base rows hashed too, three digests come
   out: the rows from the base plus the removed rows must give the base file's recorded `section_records_sha256` (so the
   base rows really are the base bundle's records), the rows from the upserts must give the upserts bundle's own records
   sha256 for the section (computed by `build` from that bundle's text), and all the rows give the result's;
3. the number of removed base rows must be the deletes plus the upserted keys the base held, and a key twice stops it.

Untouched tables keep their recorded `section_records_sha256` and are copied unhashed: `apply` trusts the base for them,
as `verify` does without `--roundtrip`, and `verify --roundtrip` on the output checks every table. The manifest's
`records_sha256` comes from the section sha256s and the merged root, and the base's own is first recomputed from its files
and root and compared with its manifest.

### 5. `verify` and `rebuild` on an applied set

- **`rebuild`** treats an applied set as any full lossless build: it writes the replayed bundle and checks it against the
  recorded records sha256s. So the round trip holds for an applied set, and its Parquet still derives from a bundle, the
  replayed one, which `rebuild` reproduces record-identical.
- **`verify --roundtrip --full B`** compares an applied set with `B` by records only: since no one file holds the state,
  there is no file sha256 to match. Any bundle holding the same records passes: a JSON replay, the rebuilt bundle, or the
  producer's own full of release N+1, when the producer's delta is exact.
- **`verify`** (the directory check), **`verify --bundle-recompute`** and **`verify --roundtrip`** without `--full` are
  unchanged.

This is the tradeoff. Tool 1.x refused a replayed set as "a state no one bundle holds", and `verify --roundtrip --full`
checked the bundle's file sha256. Keeping that refusal would make an applied set unverifiable, and inventing a file
sha256 for the replayed bundle would name a file that doesn't exist. The records sha256 already is the round trip's
identity (ADR 0008 decision 1.3), so an applied set is identified by it, and "Parquet derives from the bundle" stays
provable: the set rebuilds to the replayed bundle, and that bundle can be checked against the producer's full of the
next release.

### 6. ADR 0009 decision 2, amended

"The tool reads one bundle" now holds for `build` and `select`. `apply` reads a set the tool wrote, one bundle and a list
of keys, all generic. The release model stays the producer's: which delta follows which release, the manifest format, the
chain rule, and which files to read. A producer's docs show how to derive the deletes file from its manifest and in what
order to apply its deltas; ClinVar-GKM's would point at `jq '.sections | map_values(.deleted)'`.

## Consequences

- **A laptop can follow the weekly releases** from one full build: ClinVar-GKM's published Parquet set (`v1/1.2/parquet/`,
  built by tool 2.0.0 from the 2026-11-15 weekly with the w3id-served 1.2.0 schema set, schema set sha256 `3b39201…`) is a
  valid base for the next delta with that schema set.
- **Equivalence on real data** (2026-11-23, Apple M2 Pro): the base built from ClinVar-GKM's 2026-11-08 delta (1,779,702
  records, treated as a bundle), with the 2026-11-15 delta applied and the 11-15 manifest's deletes that are in that base
  (2,160 of 8,296 keys), equals a build of the JSON replay: all 20 tables have the same rows (1,815,952 in all; `EXCEPT ALL`
  both ways is empty), the records sha256s are equal (`f59fae5f…`), and `verify --roundtrip --full <replay>` and `rebuild`
  pass. `apply` took 33.5 s at 3.0 GB peak there, as long as building the replayed bundle, because the delta touched 14
  of 20 sections, the large ones among them. The suite proves the same on an excerpt of those two deltas
  (`tests/fixtures/real-1.2.0-apply/`) and on CIViC's two bundles, a chain of two applications included.
- **Cost on a full set: estimated, not measured.** `apply` hashes every row of every touched table, in DuckDB and the
  canonical hasher's worker processes, with memory bounded by `--memory-limit`; it never parses the full bundle's JSON.
  On a ClinVar-GKM full set (about 92 M rows, most in sections a weekly delta touches) that is roughly 50 times the
  measurement above: tens of minutes on a laptop, but within its memory. A per-record sha256 column in the tables would
  avoid the rehash; that is a rules change (a new column), left for later.
- **The `applied` chain grows** by one step (about 530 bytes) per application in every file's key-value metadata.
- **No new table-spec rules**: the tables, columns and rules version are unchanged. The key-value metadata gains an
  optional key, `applied`, and a set `apply` wrote has an empty `source_bundle_sha256`; a reader that took that value as
  a file's sha256 must handle the empty string. `apply` is a new feature, so a tool MINOR (2.1.0).
- **A delta that isn't exact gives a set that isn't the next release.** `apply` proves the output is the replay of its
  inputs, not that the inputs are the producer's whole change. `verify --roundtrip --full <the producer's full of N+1>`
  checks that, when someone holds that full.
- **ClinVar-GKM needs a paired docs change**: how to derive the deletes file, the order to apply deltas, and that a delta
  of a new line doesn't apply to a set of the previous line.
