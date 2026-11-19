---
description: Rust core conventions (the crates, the bindings, the same-output rule for each port step)
paths:
  - "crates/**"
  - "Cargo.toml"
  - "Cargo.lock"
  - "rustfmt.toml"
---

# Rust core conventions

The generic core moves to Rust one step at a time, with Python bindings (ADR 0010). Ported so far: step 1, the
canonical form and the records sha256s (`canonical.py`'s work, now `canonical.rs`). Update this file with each step.

## Layout

- `Cargo.toml` (the workspace) and `crates/`:
  - `gkm-bundle-core`: the core itself, with no Python in it, so that the GKM Toolkit or any Rust user can build on it.
    `canonical.rs` implements docs/round-trip.md.
  - `gkm-bundle-py`: the PyO3 bindings, built by maturin (`pyproject.toml`'s `[tool.maturin]`) into the extension module
    `gkm_bundle_parquet._core`. The Python module that a step replaces keeps its name and interface and names the
    bindings (`canonical.py`), so the rest of the package doesn't change.
- `uv sync` builds the extension, so a development environment needs `cargo` on the PATH (rustup). `[tool.uv]
  cache-keys` makes uv rebuild it when a crate changes.

## Tooling

- Stable Rust, edition 2024. `cargo fmt` (`rustfmt.toml`: lines of 115, as ruff) and `cargo clippy --all-targets -- -D
  warnings`; CI and `release.yml` run both.
- The crates carry no version and aren't published (`publish = false`): the tool version in `pyproject.toml` covers them
  (ADR 0010 decision 6). `Cargo.lock` is committed.
- Dependencies stay minimal: `sha2`, `ryu` and `pyo3`. A crate that defines output is pinned exactly (the `duckdb` crate
  when DuckDB moves, the `jsonschema` crate when `validate` does; ADR 0010 decision 5). Anything else needs a reason in
  the PR.

## Rules

- **No producer** (ADR 0009): `tests/test_any_bundle.py` scans every `.rs` file and `Cargo.toml` under `crates/`.
- **The same output** (ADR 0010 decision 3): a step's PR compares the port with `main`'s Python on real records and on
  generated input aimed at its edge cases, and every output of the same builds byte for byte (ClinVar-GKM excerpts and
  a weekly delta, CIViC, the sub-bundle, `rebuild`, `verify --roundtrip`). It records the comparison in the PR. Any
  difference is a bug in the port.
- **The canonical form has its own reader.** `canonical.rs` parses JSON itself rather than through a JSON library, so
  that each refusal is the one docs/round-trip.md states: a name written twice, NaN and Infinity, a number outside the
  double range, a lone surrogate. Numbers are written with `ryu`: ECMAScript wants the shortest digits that read back
  and, of those, the closest, and std's `{:e}` isn't always the closest.
- **Refusals say why**, in the words the Python used. In Python they raise `CanonicalError`, a `ValueError`; a batch
  marks a refused text instead (`canonical.failure`).
- **The GIL is released** for work over many records (`py.detach`); threads share it (`record_sha256s`).
