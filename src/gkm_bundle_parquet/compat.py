"""The compatibility data: what this tool release reads and writes. The source of `version --json`.

Keep in sync with the matrix in README.md (ADR 0002, amended by ADR 0003, ADR 0006 and ADR 0009; the release-tool
skill updates both). No producer's schema_version is listed: the one table spec, `sections`, is compiled from the
bundle's schema and its column set, so the tool reads any bundle whose schema and column set load and compile
(ADR 0009, row 0002).
"""

from __future__ import annotations

import duckdb

from . import __version__
from .compiler import RULES_VERSION, SPEC_NAME

# The one table spec (ADR 0006): compiled from a bundle schema and its column set, not read from a file.
COMPILED_SPEC = SPEC_NAME
# The prefix of every key-value entry a build writes; a change here changes the files' bytes.
KV_PREFIX = "gkm_bundle_parquet."
# The prefix of sets written by 1.x, under the tool's former name: `verify` and `rebuild` read it too (ADR 0009).
LEGACY_KV_PREFIX = "clinvar_gkm_parquet."
KV_PREFIXES = (KV_PREFIX, LEGACY_KV_PREFIX)

SCHEMA_VERSION_RULE = (
    "Reads any bundle whose schema (--bundle-schema) and column set (--column-set) load and compile into the "
    "sections spec (ADR 0006, ADR 0009); no producer's schema_version is built in. verify and rebuild read the "
    "spec a build wrote beside its manifest, and sets written by 1.x (key-value prefix clinvar_gkm_parquet.) "
    "with the sections spec."
)


def validate_engines() -> dict:
    """`validate`'s engines: the library each runs on and its installed version (None when not installed)."""
    from importlib.metadata import PackageNotFoundError, version

    def installed(dist: str) -> str | None:
        try:
            return version(dist)
        except PackageNotFoundError:
            return None

    return {
        "rs": {"library": "jsonschema-rs", "version": installed("jsonschema-rs"), "default": True},
        "python": {
            "library": "ga4gh.gkm",
            "version": installed("ga4gh.gkm"),
            "default": False,
            "extra": "reference",
        },
    }


def version_info() -> dict:
    """`version --json`: the tool's version, the table spec it writes and the version of the compiler's rules
    (the spec's version), the key-value prefixes it writes and reads, DuckDB's version and validate's engines."""
    return {
        "tool_version": __version__,
        "table_spec": COMPILED_SPEC,
        "table_spec_version": RULES_VERSION,
        "schema_version_rule": SCHEMA_VERSION_RULE,
        "kv_prefix": KV_PREFIX,
        "reads_kv_prefixes": list(KV_PREFIXES),
        "duckdb_version": duckdb.__version__,
        "validate_engines": validate_engines(),
    }
