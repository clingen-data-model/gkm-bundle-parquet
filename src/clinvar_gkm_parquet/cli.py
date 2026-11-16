"""Command line: build, select, rebuild, verify, compile-spec, validate, version (design plan §10)."""

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
        help=f"the bundle's schema: a file, a URL, or a directory holding a schema set ({required}, with "
        "--column-set)",
    )
    p.add_argument(
        "--column-set",
        help=f"the column set that pairs with --bundle-schema: a file or URL (docs/column-set-format.md; "
        f"{required})",
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
    p.add_argument("--memory-limit", help="DuckDB memory limit, e.g. 12GB")
    p.add_argument("--threads", type=int, help="DuckDB threads")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clinvar-gkm-parquet",
        description="Rebuild ClinVar-GKM's Parquet tables from its published JSON bundles.",
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
        help="write a full lossless build (the sections spec) back as its source bundle, byte for byte",
        description="Write the bundle a full build of the sections spec was made from: its uncompressed text "
        "byte for byte (ADR 0005), checked against the sha256s the build recorded. Reads the spec the build wrote "
        "beside its manifest; needs no schema. Refuses a selection, a build with --tables (or, by 1.x, from "
        "deltas) and a directory without its manifest.",
    )
    r.add_argument("dir", type=Path, help="the built directory")
    r.add_argument("-o", "--out", required=True, type=Path, help="the bundle to write (.json, or .json.gz)")
    _engine(r)

    v = sub.add_parser(
        "verify",
        help="check a built directory (the directory check, bundle recompute, round trip)",
        description="Check a built directory against the spec the build wrote beside its manifest, which every "
        "file's key-value metadata names by its sha256. Needs no schema; given --bundle-schema and --column-set, "
        "also checks that they compile to that spec.",
    )
    v.add_argument("dir", type=Path)
    v.add_argument("--bundle-recompute", action="store_true", help="id sets and data against the bundle(s)")
    v.add_argument(
        "--roundtrip",
        action="store_true",
        help="rebuild the bundle text (hashed, not written) and compare it with the recorded sha256s; with "
        "--full, also with that bundle's uncompressed text (a full lossless build only, ADR 0005)",
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


# --- deprecation (ADR 0009 decision 5): the old name, removed with the rename in 2.0.0, a line on stderr -----

NEW_NAME = "gkm-bundle-parquet"
NEW_REPOSITORY = "https://github.com/clingen-data-model/gkm-bundle-parquet"
REMOVED_IN = "2.0.0"
COMMANDS = ("build", "select", "rebuild", "verify", "compile-spec", "validate", "version")


def _deprecated(message: str) -> None:
    print(f"clinvar-gkm-parquet: deprecated: {message}", file=sys.stderr, flush=True)


def _warn_old_name(argv: list[str]) -> None:
    """Every invocation, before the arguments are parsed (so --help and a usage error say it too)."""
    command = next((a for a in argv if not a.startswith("-")), None)
    command = command if command in COMMANDS else None
    run = f"{NEW_NAME} {command}" if command else NEW_NAME
    _deprecated(
        f"this tool is renamed {NEW_NAME} ({NEW_REPOSITORY}), and the name clinvar-gkm-parquet is removed in "
        f"{REMOVED_IN}; from {REMOVED_IN}, install {NEW_NAME} and run `{run}`"
    )


def _version(args: argparse.Namespace) -> int:
    from .compat import version_info

    info = version_info()
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        print(
            f"clinvar-gkm-parquet {info['tool_version']}: table spec {info['table_spec']} "
            f"{info['table_spec_version']}; duckdb {info['duckdb_version']}\n{info['schema_version_rule']}"
        )
    return 0


def _compile_spec(args: argparse.Namespace) -> int:
    from .build import resolve_pairing
    from .compiled import compiled_yaml

    _, source = resolve_pairing(args)
    sys.stdout.write(compiled_yaml(source))
    return 0


def _build(args: argparse.Namespace) -> int:
    from .build import run_build_command

    return run_build_command(args)


def _select(args: argparse.Namespace) -> int:
    from .driver import run_select_command

    return run_select_command(args)


def _rebuild(args: argparse.Namespace) -> int:
    from .roundtrip import run_rebuild_command

    return run_rebuild_command(args)


def _verify(args: argparse.Namespace) -> int:
    from .verify import run_verify_command

    return run_verify_command(args)


def _validate(args: argparse.Namespace) -> int:
    from .validate import run_validate_command

    return run_validate_command(args)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    _warn_old_name(argv)
    args = _parser().parse_args(argv)
    handler = {
        "build": _build,
        "select": _select,
        "rebuild": _rebuild,
        "verify": _verify,
        "compile-spec": _compile_spec,
        "validate": _validate,
        "version": _version,
    }[args.command]
    import duckdb

    try:
        return handler(args)
    except ToolError as e:
        print(f"clinvar-gkm-parquet: error: {e}", file=sys.stderr)
        return 2
    except (
        duckdb.Error
    ) as e:  # e.g. an unreadable or truncated Parquet or staging file: an input error, not a diff
        print(f"clinvar-gkm-parquet: error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
