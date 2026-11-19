# 0010 — A Rust core with Python bindings, ported step by step

- **Status:** Accepted (2026-11-19)
- **Date:** 2026-11-19
- **Plan (if any):** none; the steps are below. Amends [ADR 0006](0006-sections-spec-compiled-from-schemas.md) (where
  the generic core goes) and the GKM Toolkit consequence of [ADR 0009](0009-generic-gkm-bundle-tool-renamed.md).

## Context

ADR 0006 keeps the generic core in Python and has it import nothing outside itself, so that it can move into the GKM
Toolkit (`ga4gh.gkm` in `ga4gh/gkm-starter-kit`) when the toolkit's maintainer asks. The toolkit is Python >=3.11 with
`jsonschema` and `requests`. ADR 0009 left that plan unchanged.

The maintainer asked whether a Rust implementation would make the tool significantly faster. A build was measured on
2026-11-19 to find where the time goes:

- **Input:** a ClinVar-GKM 1.1.0 weekly delta (`clinvar-gkm-delta_2026-0905.json.gz`, a local copy; computed sha256
  `968fe81c63b55e9a5613581908b7ac5027a03f847a986eabed96e48b279c559d`). It was built as an ordinary bundle with the
  suite's 1.1.0 pairing (`tests/fixtures/clinvar-gkm/1.1.0/`): 121,248 records, 80 MB uncompressed, 5.4 MB gzip.
- **Machine:** Apple M2 Pro (10 cores), 16 GB, macOS 15.7.7, Python 3.14.5, DuckDB 1.5.4, tool 2.0.0 at `e50aa9d`.
- **Method:** each pipeline stage wrapped with a wall-clock timer, three runs. The build passed its round-trip check
  each time and took 6.3 to 7.0 s.

The build overlaps a reader thread and a worker thread. The reader stages each section and then waits for the worker, so
the worker's stages set the total:

| Stage | Wall time, run of 6.3 s | Runs in |
| --- | --- | --- |
| Writing the tables (`build_tables` without the row check) | about 1.8 s | DuckDB (C++) |
| Each written row back to its record text (`record_sql`, the row check's query) | about 1.65 s | DuckDB (C++) |
| Each source record's canonical sha256 (`roundtrip.source_digest`) | about 1.3 s | Python, a 10-process pool |
| Fetching the rebuilt records into Python and hashing them (the rest of `check_rows`) | about 1.2 s | Python, a 10-process pool |
| Imports and compiling the pairing | about 0.5 s | Python |
| The reader's own work: gunzip, the line grammar, staging gzip | about 0.35 s, overlapped | zlib (C) and Python |

The canonical sha256 (`canonical.record_sha256`) of all 121,248 records takes 1.34 s on one core. `orjson`, a Rust JSON
library, does the same parse, key sort, serialisation and hash in 0.24 s (5.6 times faster). It is a stand-in for a Rust
canonical form, which this tool's rules may make somewhat slower. In a build that work is already spread over 10
processes. What remains is mostly moving rows from DuckDB into Python and out to the pool.

So about 2.5 s of the 6.3 s is Python, and about 3.5 s is DuckDB, which would be the same engine called from Rust.
Estimates, from this one delta:

- A Rust canonical form, run in the worker with no process pool, takes the build to about 4.3 s, 1.5 times faster.
- A full rewrite comes to at most about 2 times faster, since DuckDB's share stays.
- A monthly full (about 4.8 GB gzip) does the same work per record, so the shares should hold. It was not measured.

A Rust core can still produce the same files:

- **DuckDB.** The `duckdb` crate encodes DuckDB's version in its own (`1.MAJOR_MINOR_PATCH.x`, per its README).
  `1.10504.0` bundles DuckDB v1.5.4 with source id `08e34c447b`, the build that every file's Parquet footer names today
  (`created_by`: `DuckDB version v1.5.4 (build 08e34c447b)`; checked 2026-11-19).
- **Validation.** The `jsonschema` crate 0.61.1 is the same release as the Python `jsonschema-rs` 0.61.1 that `validate`
  pins.

The maintainer decided on 2026-11-19: drop the plan to keep the core in Python so that it can be merged into the toolkit.
Build the core in Rust instead, and let `ga4gh.gkm` call it through a Python interface. Port it one step at a time.

## Decision

1. **The generic core becomes a Rust library with Python bindings.** It is a Cargo crate in this repository. A PyO3
   binding module, built with maturin, ships inside the `gkm-bundle-parquet` Python distribution. The crate and module
   names are provisional until the first step lands. The Python package, its CLI and its public functions stay the
   interface.
2. **One step at a time, the measured win first.**
   1. The canonical form and the records sha256s (`canonical.py`), called from the build's worker in-process. This
      replaces the hash pool for `source_digest` and `check_rows`.
   2. Later steps (the bundle readers, the round trip, the compiler and the schema-set loader, and in the end the CLI)
      each need their own reason: a measurement, or the toolkit's need for that part in Rust. Moving DuckDB to Rust is
      one step, never part of one: no step uses DuckDB from both languages on the same data.
3. **Each step gives the same output.** The ported build must write byte-identical Parquet files to the Python build of
   the last release, except for `tool_version` in the key-value metadata. Its compiled spec and its manifest must match
   too, except for timestamps and `tool_version`. Inputs:
   - every bundle fixture in the suite (ClinVar-GKM, CIViC, the sub-bundle);
   - one real ClinVar-GKM weekly delta.

   The step's PR records the comparison. Any other difference is a bug in the port, fixed before the step lands; it is
   not a new output version. A step also keeps the same refusals: the suite's tests for wrong input pass unchanged. A
   step that moves `validate` reruns the equivalence evidence in [`docs/validate.md`](../validate.md).
4. **The ported part replaces its Python code in the same PR.** The suite's golden expectations stay independent of the
   code under test (`.claude/rules/python.md`), so the tests check the Rust code as they checked the Python. No second
   copy is kept as a reference.
5. **The rules carry over to Rust.** No producer appears in the Rust code; `tests/test_any_bundle.py` extends its scan to
   it. Fail loudly. Exact pins: the `duckdb` crate at the version that bundles the DuckDB the Python package pins, and the
   `jsonschema` crate at the version `validate`'s evidence names.
6. **Distribution.** The first step's release ships compiled wheels. They target the stable Python ABI from the oldest
   Python the package supports. The platform list is settled in that release's work. An install from the sdist needs a
   Rust toolchain. The crate is not published to crates.io by this ADR, and the tool version covers it (ADR 0002).
7. **The GKM Toolkit.** `ga4gh.gkm` can call the core through the Python bindings instead of taking in the Python
   source. That would give the toolkit a compiled dependency. Whether it takes one is its maintainer's decision, asked
   before anything here is built for it. Until then nothing in this repository depends on the toolkit's answer.

## Earlier ADRs

| ADR | What changes |
| --- | --- |
| 0006 | The generic core moves to a Rust crate with Python bindings, not into `ga4gh.gkm` as Python source. Decision 8 (no producer in the core) stands and covers the Rust code. The dependency note about the move becomes decision 7 here. |
| 0009 | "The GKM Toolkit move is unchanged" is replaced by decision 7 here. |

## Consequences

- **The expected gain is modest:** about 1.5 times faster for a build after step 1, and at most about 2 times faster
  after a full port (estimates). DuckDB bounds it. A full port also removes Python's startup and the hash pool's
  process spawning. Pure Rust would allow a single binary, but this ADR doesn't decide one.
- **Two languages during the port.** Contributors and CI need a Rust toolchain. The release workflow builds wheels for
  several platforms instead of one pure-Python wheel. Step 1 changes `.github/` and the `release-tool` skill and adds a
  `.claude/rules/` file for the Rust code.
- **The record-identical round trip (ADR 0008) and the byte-identical output rule don't change.** Each step proves them
  again (decision 3).
- **CLAUDE.md and `.claude/rules/python.md`** say where the core goes: to Rust, with bindings the toolkit can use. They no
  longer say the core moves into the toolkit as Python.
