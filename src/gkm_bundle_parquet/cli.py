"""Command line: build, select, rebuild, apply, verify, compile-spec, generate, validate, version (design plan
§10)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import ToolError

FULL_HELP = "the bundle: a path or URL (.json or .json.gz)"
SHA256_HELP = "the bundle's expected sha256 (default: its <file>.sha256 sidecar, when there is one)"


def _inputs(p: argparse.ArgumentParser) -> None:
    p.add_argument("--full", required=True, help=FULL_HELP)
    p.add_argument("--sha256", help=SHA256_HELP)


def _pairing(p: argparse.ArgumentParser, *, required: str = "required") -> None:
    p.add_argument(
        "--bundle-schema",
        help=f"the bundle's schema: a file, a URL, or a directory holding a schema set ({required})",
    )
    p.add_argument(
        "--column-set",
        help="a column set that pairs with --bundle-schema: a file or URL (docs/column-set-format.md); it must "
        "compile to the default or declare itself a projection. Default: the generated default (ADR 0007)",
    )
    p.add_argument(
        "--version-property",
        help="the root property holding the bundle's version, for the generated default (default: none, so the "
        "version is a root value); with --column-set, the column set says it",
    )


def _selection(p: argparse.ArgumentParser, *, required: bool) -> None:
    g = p.add_argument_group("selection (ADR 0004)")
    g.add_argument(
        "--driver", required=required, help="a table of the spec: its bundle section holds the starting records"
    )
    g.add_argument(
        "--where",
        help="a DuckDB boolean expression over the driver table as the spec builds it (no ';'), "
        "e.g. \"name = 'x'\"",
    )
    g.add_argument("--ids", help="comma-separated driver keys (added to the --where keys)")
    g.add_argument("--ids-file", help="a file of driver keys, one per line (added to the --where keys)")
    g.add_argument(
        "--follow", choices=("out", "in", "both"), help="follow pointers out of, into, or both ways (default out)"
    )
    g.add_argument("--max-depth", type=int, help="pointer hops from a driver record (default unlimited)")
    g.add_argument("--sections", help="comma-separated bundle sections that may be traversed and written")


def _engine(p: argparse.ArgumentParser) -> None:
    p.add_argument("--work-dir", type=Path, help="staging and DuckDB spill directory")
    p.add_argument(
        "--memory-limit",
        help="DuckDB memory limit, e.g. 12GB (default: half the machine's memory, or of its cgroup limit if "
        "lower)",
    )
    p.add_argument("--threads", type=int, help="DuckDB threads")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gkm-bundle-parquet",
        description="Build Parquet tables from GKM JSON bundles, rebuild the bundle from them, and validate "
        "bundles.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser(
        "build",
        help="build the Parquet tables from a bundle, or from a selection of it",
        description="Build the tables of a spec. With --driver, select the driver records and the records they "
        "reach first (a mini-bundle in the work directory), then build from it.",
    )
    _inputs(b)
    _pairing(b)
    b.add_argument("--out", required=True, type=Path, help="output directory")
    b.add_argument("--tables", help="comma-separated subset of tables (default: every table in the spec)")
    b.add_argument("--keep-work", action="store_true", help="keep the staging files (and the mini-bundle)")
    _engine(b)
    _selection(b, required=False)

    s = sub.add_parser(
        "select",
        help="write only the mini-bundle of a selection (the driver records and the records they reach)",
        description="Write the mini-bundle a selective build would build from: the selected records, verbatim, "
        "in the bundle's line layout. The same inputs and options give the same bytes.",
    )
    _inputs(s)
    _pairing(s)
    s.add_argument("--out", required=True, type=Path, help="the mini-bundle to write (.json.gz; must not exist)")
    _engine(s)
    _selection(s, required=True)

    r = sub.add_parser(
        "rebuild",
        help="write a full lossless build (the sections spec) back as its source bundle, record-identical",
        description="Write the bundle a full build of the sections spec was made from, record-identical "
        "(ADR 0008: every record by key, the same canonical form; key order, array order, string escapes, number "
        "spelling and whitespace don't count; docs/round-trip.md), checked against the records sha256s the build "
        "recorded. Reads the spec the build wrote beside its manifest; needs no schema. A set built by tool 1.x "
        "records none: it is written unchecked. Refuses a selection, a build with --tables (or, by 1.x, from "
        "deltas) and a directory without its manifest.",
    )
    r.add_argument("dir", type=Path, help="the built directory")
    r.add_argument("-o", "--out", required=True, type=Path, help="the bundle to write (.json, or .json.gz)")
    _engine(r)

    a = sub.add_parser(
        "apply",
        help="bring a built set up to date: remove deleted keys, then add or replace the records of a bundle",
        description="Write a new set: the base set's records, less the keys --deletes lists and the keys the "
        "--upserts bundle holds, plus every record of the --upserts bundle (deletes first, then upserts; ADR "
        "0011). It equals a build of the replayed bundle, and records that bundle's records sha256s, so rebuild "
        "and verify --roundtrip check it as they check a build. The base is a full build of this tool (or an "
        "earlier apply), default or projection; --bundle-schema (and --column-set) must compile to its spec, and "
        "the upserts bundle must hold the base's version. The base set is not changed.",
    )
    a.add_argument("base", type=Path, help="the built set to update")
    a.add_argument(
        "--upserts",
        required=True,
        help="a bundle of the base's schema and version holding the added and changed records: a path or URL",
    )
    a.add_argument("--sha256", help="the upserts bundle's expected sha256 (default: its <file>.sha256 sidecar)")
    a.add_argument(
        "--deletes",
        help='the keys to remove: a JSON file (path or URL) {"<section>": ["<key>", ...]}; every key must be in '
        "the base set",
    )
    _pairing(a)
    a.add_argument("--out", required=True, type=Path, help="output directory (new or empty)")
    _engine(a)

    v = sub.add_parser(
        "verify",
        help="check a built directory (the directory check, bundle recompute, round trip)",
        description="Check a built directory against the spec the build wrote beside its manifest, which every "
        "file's key-value metadata names by its sha256. Needs no schema; given --bundle-schema (and --column-set, "
        "or else the schema's default), also checks that they compile to that spec.",
    )
    v.add_argument("dir", type=Path)
    v.add_argument("--bundle-recompute", action="store_true", help="id sets and data against the bundle")
    v.add_argument(
        "--roundtrip",
        action="store_true",
        help="rebuild the bundle's records (hashed, not written) and compare their records sha256s with the "
        "recorded ones; with --full, also with that bundle's own (a full lossless build only; a set built by tool "
        "1.x needs --full)",
    )
    v.add_argument(
        "--full",
        help="the bundle, for --bundle-recompute (or --roundtrip); for a selection, the mini-bundle (its "
        "sha256 must equal the recorded selected_sha256)",
    )
    v.add_argument("--sha256", help=SHA256_HELP)
    _pairing(v, required="optional")
    v.add_argument("--report", type=Path, help="write a Markdown report here (and a .json beside it)")
    _engine(v)

    c = sub.add_parser("compile-spec", help="write the sections spec of a pairing as YAML, with no build")
    _pairing(c)

    g = sub.add_parser(
        "generate",
        help="write a bundle schema's default column set, or check a column set against it",
        description="Write the default column set of a bundle schema (ADR 0007): every collection, every property "
        "its record schema allows. Reports on stderr every IRI position with no pointer pattern and every "
        "JSON-text position, with the reason. With --check, say whether --column-set compiles to the default, "
        "listing each difference (exit 1 if it differs).",
    )
    g.add_argument(
        "--bundle-schema", help="the bundle's schema: a file, a URL, or a schema-set directory (required)"
    )
    g.add_argument("--version-property", help="the root property holding the bundle's version (default: none)")
    g.add_argument("-o", "--out", type=Path, help="write the column set here (default: stdout)")
    g.add_argument("--check", action="store_true", help="check --column-set against the default")
    g.add_argument("--column-set", help="with --check: the column set to check")

    val = sub.add_parser(
        "validate",
        help="validate every record of a bundle against its JSON Schema set",
        description="Validate each record alone, as a one-section mini-bundle with the bundle's version, so key "
        "patterns are checked too. Exits 1 if any record fails, 2 on anything that isn't a verdict on a record "
        "(an unreadable bundle, an unknown section, a missing version, a schema set that doesn't load).",
    )
    val.add_argument("bundle", help="the bundle: a path or URL (.json or .json.gz)")
    val.add_argument(
        "--bundle-schema", help="its schema: a file, a URL, or a directory holding a schema set (required)"
    )
    val.add_argument(
        "--engine",
        choices=("rs", "python"),
        default="rs",
        help="rs: jsonschema-rs (the default); python: the GKM Toolkit's validator, the reference (needs the "
        "reference extra)",
    )
    val.add_argument("--workers", type=int, help="worker processes (default: the number of CPUs)")
    val.add_argument(
        "--version-property",
        help="the root property holding the bundle's version, put in every record's mini-bundle (default: none)",
    )
    val.add_argument("--sha256", help=SHA256_HELP)
    val.add_argument("--max-print", type=int, default=20, help="failing records to print (default 20)")
    val.add_argument("--failures", type=Path, help="write every failing record and its errors here, as JSON lines")
    val.add_argument("--json", type=Path, help="write the counts and timings here, as JSON")

    ver = sub.add_parser("version", help="tool version and compatibility")
    ver.add_argument("--json", action="store_true")
    return p


def _version(args: argparse.Namespace) -> int:
    from .compat import version_info

    info = version_info()
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        also = ", ".join(p for p in info["kv_prefixes_read"] if p != info["kv_prefix"])
        print(
            f"gkm-bundle-parquet {info['tool_version']}: table spec {info['table_spec']} "
            f"{info['table_spec_version']}, policy {info['policy_version']}; duckdb {info['duckdb_version']}\n"
            f"key-value prefix {info['kv_prefix']} (reads also {also})\n{info['schema_version_rule']}"
        )
    return 0


def _compile_spec(args: argparse.Namespace) -> int:
    from .build import resolve_pairing
    from .compiled import compiled_yaml

    _, source = resolve_pairing(args)
    sys.stdout.write(compiled_yaml(source))
    return 0


def _generate(args: argparse.Namespace) -> int:
    from .compiled import run_generate_command

    return run_generate_command(args)


def _build(args: argparse.Namespace) -> int:
    from .build import run_build_command

    return run_build_command(args)


def _select(args: argparse.Namespace) -> int:
    from .driver import run_select_command

    return run_select_command(args)


def _rebuild(args: argparse.Namespace) -> int:
    from .roundtrip import run_rebuild_command

    return run_rebuild_command(args)


def _apply(args: argparse.Namespace) -> int:
    from .apply import run_apply_command

    return run_apply_command(args)


def _verify(args: argparse.Namespace) -> int:
    from .verify import run_verify_command

    return run_verify_command(args)


def _validate(args: argparse.Namespace) -> int:
    from .validate import run_validate_command

    return run_validate_command(args)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = _parser().parse_args(argv)
    handler = {
        "build": _build,
        "select": _select,
        "rebuild": _rebuild,
        "apply": _apply,
        "verify": _verify,
        "compile-spec": _compile_spec,
        "generate": _generate,
        "validate": _validate,
        "version": _version,
    }[args.command]
    import duckdb

    try:
        return handler(args)
    except ToolError as e:
        print(f"gkm-bundle-parquet: error: {e}", file=sys.stderr)
        return 2
    except (
        duckdb.Error
    ) as e:  # e.g. an unreadable or truncated Parquet or staging file: an input error, not a diff
        print(f"gkm-bundle-parquet: error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
