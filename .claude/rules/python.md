---
description: Python package conventions (layout, dependencies, streaming, fail-loud rules, tests)
paths:
  - "src/**"
  - "tests/**"
  - "pyproject.toml"
---

# Python package conventions

The package, `gkm_bundle_parquet`: `cli.py`, `build.py` (the section pipeline), `model.py` (the data passed between
stages, and the staging format), `sources.py` (one input: a path or URL and its expected sha256, from `--sha256` or a
`<file>.sha256` sidecar), `spec.py` (the compiled spec's format and its checks), `sqltemplate.py` (renders each table's SQL
from the spec, with the macros in `sql/_macros.sql`), `select.py` (the selection engine: pointer walk and mini-bundle),
`driver.py` (wires selection into `build` and `select`), `roundtrip.py` (rows back to bundle records: the build's
round-trip check, `rebuild` and `verify --roundtrip`; ADR 0005), `verify.py`, `validate.py` (`validate`: every record of a
bundle against its JSON Schema set, with the rs engine or the python reference; docs/validate.md) and `compat.py` (the
source of `version --json`). Update this file when the layout changes.

**No producer in the package** (ADR 0009): no module and no SQL file names a producer, its sections or its URLs;
`tests/test_any_bundle.py` scans them all. **The generic core** (ADR 0006) also imports nothing outside itself, because it
is meant to move to the GKM Toolkit:

- `compiler.py` compiles a table spec from a schema set and its column set; `columnset.py` loads a column set
  (`docs/column-set-format.md`); `schemas.py` loads a schema set from a file, URL or directory; `bundle.py` holds the two
  readers behind the sink interface; `transport.py` reads bytes from a path or URL; `model.py` and `errors.py`.

**The tool around it:** `compiled.py` compiles a pairing (`compile_pairing`), writes `table-spec.json` beside a build's
manifest and reads it back; `reader_rules` gives the bundle reader the spec's `root`.

The package ships no schema set and no column set. The suite's ClinVar-GKM pairing is a test fixture,
`tests/fixtures/clinvar-gkm/1.1.0/` (`schema-set/`, written by `scripts/vendor-schemas.py`, and `columns.yaml`).

## Layout and tooling

- `src/` layout, package `gkm_bundle_parquet`, console script `gkm-bundle-parquet`.
- Python `>=3.10`. Manage environments with `uv`; lint and format with `ruff`; test with `pytest`.
- Runtime dependencies stay minimal: `duckdb` (pinned to the exact version that defines the output, see below),
  `PyYAML` (reads column sets) and `jsonschema-rs` (`validate`'s default engine, pinned exactly, because
  its verdicts are proved equal to the reference's at that version). The reference validator, `ga4gh.gkm`, is the optional
  `reference` extra and a dev dependency; its GA4GH dependencies are pre-releases (`[tool.uv] prerelease = "allow"`). The
  build's reader passes records through as bytes and never parses them in Python, so there is no JSON speed-up
  dependency (`validate` parses each record in its workers). Anything else needs a reason in the PR.

## Output determinism

- The same tool version, table spec and input bundle(s) must give byte-identical Parquet files. So:
  - pin the exact DuckDB version a release was tested with;
  - write with `ORDER BY` the table's sort key, fixed compression and fixed row-group size;
  - put no wall-clock timestamps in the Parquet key-value metadata (put them in the dataset manifest the tool
    writes beside the files instead; design plan §7.3).
- Declare every output column type explicitly. Never rely on DuckDB's JSON type inference: it differs from
  the table spec on nested struct field order, integer widths and empty-versus-NULL lists.

## Reading bundles

- Bundles are large (a monthly full is about 4.8 GB gzip). **Stream them**; never `json.load` a whole bundle.
  The assembler writes one `"<key>": <record>` per line; the format is verified in the design plan §4.2.
- Compute the sha256 of the compressed bytes while streaming, and check it against the expected one (`--sha256`, or
  the `<file>.sha256` sidecar) when there is one. Refuse on a mismatch.
- A file in another JSON layout is read whole, by the general reader: never read a large file that way. Layout is chosen per
  file (`bundle.sniff_layout`), not by the pairing.
- A bundle is read only with its pairing; there is no default. The version property, if any, is the column set's (the
  spec's `root`). Nothing in the package lists sections or versions: a bundle's collections come from its schema
  (`compiler.root_parts`).

## Fail loudly

- An unknown bundle section, an unknown extension name, a record that doesn't match the table spec: stop with a clear
  error naming the file, section and key. Never skip or drop silently.

## Network

- Every HTTP request sends `User-Agent: gkm-bundle-parquet/<version>`.
- A schema set's `$ref`s are followed on `https://w3id.org/` only, as JSON data, once per root, into the user cache
  (`$XDG_CACHE_HOME/gkm-bundle-parquet/`); a set on disk is never fetched.
- Respect the CDN limit of 250 requests per 10 s: one stream per large file, retry 429 and 5xx with backoff,
  resume interrupted downloads with HTTP `Range`.
- Unit tests never touch the network, and `tests/conftest.py` enforces it and gives the suite a private cache.
  Network tests are opt-in (`pytest -m network`).

## Tests

- Fixtures are small excerpts of real published bundles, with the source URL, release, schema_version and source
  sha256 recorded beside them (`PROVENANCE.json`, a README). Don't hand-write bundle records.
- Golden expectations come from the source files parsed independently (stdlib json), not from the code under test. `tests/fixtures/sections-golden/` is the hand-researched `sections` spec for 1.1.0, what the compiler
  must reproduce; a build doesn't load it.
- A schema set for a test is a directory given to `--bundle-schema`: the real path, no patching. `tests/fixtures/civic/` and
  `tests/fixtures/clinvar-sub-bundle/` are real bundles of another producer and another layout, with their sources recorded.
  `tests/fixtures/sets-1.1.0/` is a set tool 1.1.0 built, for the 1.x compatibility of `verify` and `rebuild`.
