"""The compatibility data: what this tool release reads and writes. The source of `version --json`.

Keep in sync with the matrix in README.md (ADR 0002, amended by ADR 0006, ADR 0008 and ADR 0009; the release-tool
skill updates both). No producer's schema_version is listed: the one table spec, `sections`, is compiled from
the bundle's schema (its generated default, ADR 0007) or the schema and a column set, so the tool reads any bundle
whose pairing loads and compiles. A release states its table-spec and policy versions (ADR 0007 and ADR 0009, each
on row 0002), the key-value prefix it writes and the prefixes it reads.
"""

from __future__ import annotations

import duckdb

from . import __version__
from .compiler import RULES_VERSION, SPEC_NAME

# The one table spec (ADR 0006): compiled from a bundle schema and its column set, not read from a file.
COMPILED_SPEC = SPEC_NAME
# The prefix of every key-value entry a build writes; a change here changes the files' bytes.
KV_PREFIX = "gkm_bundle_parquet."
# The prefix sets written under the tool's former name carry (1.x): verify and rebuild read it too (ADR 0009).
LEGACY_KV_PREFIX = "clinvar_gkm_parquet."

# What verify and rebuild read: a set's key-value entries under either prefix, the one it writes first.
KV_PREFIXES_READ = (KV_PREFIX, LEGACY_KV_PREFIX)

SCHEMA_VERSION_RULE = (
    "Reads any bundle given its schema (--bundle-schema, required): the tables are the schema's generated "
    "default, or a --column-set that compiles to it or names a projection (ADR 0006, ADR 0007, ADR 0009); no "
    "producer's schema_version is built in. verify and rebuild read the spec a build wrote beside its manifest, "
    "including sections sets written by tool 1.x. apply reads a full lossless set of tool 2.x with a bundle of "
    "the same pairing and version and a JSON list of deleted keys (ADR 0011)."
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
    """`version --json`: the tool's version, the table spec it writes, its version and the default policy's (both
    the compiler's rules version, ADR 0007), the key-value prefix it writes and the ones it reads, DuckDB's version
    and validate's engines."""
    return {
        "tool_version": __version__,
        "table_spec": COMPILED_SPEC,
        "table_spec_version": RULES_VERSION,
        "policy_version": RULES_VERSION,
        "schema_version_rule": SCHEMA_VERSION_RULE,
        "kv_prefix": KV_PREFIX,
        "kv_prefixes_read": list(KV_PREFIXES_READ),
        "duckdb_version": duckdb.__version__,
        "validate_engines": validate_engines(),
    }
