"""A compiled spec, as the tool's stages use it: compiled from a pairing, checked, written into a built directory,
and read back from one (ADR 0006).

A pairing is a bundle schema and its column set, each a path or URL. A bundle is read when its pairing loads and
compiles; there is no default pairing (ADR 0009). With no column set, the schema's default is generated (ADR 0007,
compiler.generate_default). A column set given is the default when it compiles to the default's tables (an equal
table_spec_sha256), else it must declare itself a named projection (`projection: <name>`), or it is refused.

A build writes the spec it compiled as COMPILED_SPEC_FILE beside its manifest, so `verify` and `rebuild` need no
schema: they load that file and hold its hash against the one every file records.
"""

from __future__ import annotations

import hashlib
import json
from functools import cache
from pathlib import Path

import yaml

from .bundle import BundleShape, RootRules
from .columnset import load_column_set
from .compiler import RULES_VERSION, compile_sections, generate_default, spec_differences, table_spec_sha256
from .errors import CompileError, SpecError
from .schemas import load_schema_set
from .spec import Spec, SpecSource, check_compiled, parse_spec

COMPILED_SPEC_FILE = "table-spec.json"
DEFAULT, PROJECTION = "default", "projection"  # what a column set is (ADR 0007 §3)


def _from_raw(
    raw: dict, schema_set_sha256: str, column_set_sha256: str, kind: str, projection: str | None = None
) -> tuple[Spec, SpecSource]:
    where = f"sections compiled from schema set {schema_set_sha256}"
    raw = {**raw, "column_set": {"kind": kind, "projection": projection, "policy_version": RULES_VERSION}}
    spec = parse_spec(raw, where=where, require_name=True)
    check_compiled(spec, where)
    source = SpecSource(
        name=spec.name,
        version=spec.spec_version,
        builtin=True,
        path=None,
        sha256=table_spec_sha256(raw),
        schema_set_sha256=schema_set_sha256,
        column_set_sha256=column_set_sha256,
        column_set_kind=kind,
        projection=projection,
        raw=raw,
    )
    return spec, source


@cache
def generated(schema_source: str, version_property: str | None = None):
    """The schema's default (compiler.Generated) and its schema set."""
    schema_set = load_schema_set(schema_source)
    return generate_default(schema_set, version_property), schema_set


@cache
def compile_default(schema_source: str, version_property: str | None = None) -> tuple[Spec, SpecSource]:
    """The default set of a bundle schema, generated (ADR 0007 2.2), checked, and its source; the column set
    sha256 is that of the generated column set's text."""
    gen, schema_set = generated(schema_source, version_property)
    return _from_raw(gen.raw, schema_set.sha256, hashlib.sha256(gen.text.encode("utf-8")).hexdigest(), DEFAULT)


def compliance(schema_source: str, column_set_source: str) -> tuple[dict, dict, list[str]]:
    """(the default's compiled spec, the column set's, their differences): empty when the column set complies, that
    is, compiles to the default's tables (ADR 0007 decision 3.1)."""
    schema_set = load_schema_set(schema_source)
    column_set = load_column_set(column_set_source)
    raw = compile_sections(schema_set, column_set.doc)
    gen, _ = generated(schema_source, column_set.doc.get("version_property"))
    if table_spec_sha256(raw) == table_spec_sha256(gen.raw):
        return gen.raw, raw, []
    return gen.raw, raw, spec_differences(gen.raw, raw) or ["(the specs differ, but not in a table or column)"]


@cache
def compile_pairing(schema_source: str, column_set_source: str) -> tuple[Spec, SpecSource]:
    """The spec of a pairing, checked, and its source: the default when the column set complies, a named projection
    when it declares one, refused otherwise (ADR 0007 §3). Raises SchemaSetError, CompileError or SpecError."""
    schema_set = load_schema_set(schema_source)
    column_set = load_column_set(column_set_source)
    _, raw, differences = compliance(schema_source, column_set_source)
    if not differences:
        return _from_raw(raw, schema_set.sha256, column_set.sha256, DEFAULT)
    name = column_set.doc.get("projection")
    if name is None:
        shown = "\n  ".join(differences[:20]) + (
            f"\n  ... and {len(differences) - 20} more" if len(differences) > 20 else ""
        )
        raise CompileError(
            f"column set {column_set_source}: it doesn't compile to the default of schema {schema_set.bundle_id} "
            f"(policy {RULES_VERSION}), and doesn't declare itself a projection (`projection: <name>`; ADR 0007 "
            f"decision 3.2). It differs in:\n  {shown}"
        )
    return _from_raw(raw, schema_set.sha256, column_set.sha256, PROJECTION, name)


def reader_rules(spec: Spec) -> RootRules:
    """How to read a bundle's root for `spec`, as its `root` says (a compiled spec always has one)."""
    if spec.root is None:
        raise SpecError(f"table spec {spec.label} has no root: it doesn't say how a bundle's root is read")
    shape = BundleShape(tuple(s for t in spec.tables for s in t.sections), spec.root.other_values)
    return RootRules(spec.root.version_property, lambda version: shape)


def write_compiled(source: SpecSource, out_dir: Path) -> Path:
    path = Path(out_dir) / COMPILED_SPEC_FILE
    path.write_text(json.dumps(source.raw, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def is_recorded(kv: dict[str, str]) -> bool:
    """A file's key-value metadata (keys without the prefix) says a compiled spec wrote it."""
    return "schema_set_sha256" in kv


def recorded_spec(out_dir: Path, kv: dict[str, str]) -> Spec:
    """The compiled spec that wrote a set, from the file beside its manifest, checked against the hash the files
    record. A difference is refused, saying which hash and which tool version wrote the set."""
    path = Path(out_dir) / COMPILED_SPEC_FILE
    tool = kv.get("tool_version", "?")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise SpecError(
            f"{path}: the files name a compiled spec (written by tool {tool}), and it is missing: {e}"
        ) from e
    except ValueError as e:
        raise SpecError(f"{path}: not a compiled spec: {e}") from e
    want = kv.get("table_spec_sha256")
    got = table_spec_sha256(raw) if isinstance(raw, dict) and "tables" in raw else None
    if got != want:
        raise SpecError(
            f"{path}: the compiled spec differs from what the files record: its table_spec_sha256 is {got}, the "
            f"files (written by tool {tool}) record {want}"
        )
    spec = parse_spec(raw, where=str(path), require_name=True)
    check_compiled(spec, str(path))
    return spec


def compiled_yaml(source: SpecSource) -> str:
    """A compiled spec as YAML (`compile-spec`), with its hashes in a comment."""
    what = f"the projection {source.projection}" if source.projection else "the default"
    header = (
        f"# Table spec {source.name} {source.version}, compiled (ADR 0006): {what} (policy {source.version}).\n"
        f"# schema_set_sha256: {source.schema_set_sha256}\n"
        f"# column_set_sha256: {source.column_set_sha256}\n"
        f"# table_spec_sha256: {source.sha256} (of what affects the tables, compiler.table_spec_sha256)\n"
    )
    return header + yaml.safe_dump(source.raw, sort_keys=False, allow_unicode=True, width=100_000)


# --- generate (ADR 0007 decision 2.1, 3.1) ----------------------------------------------------------------------


def generate_report(notes) -> str:
    """What `generate` reports: IRI positions without a pointer pattern, the JSON-text positions with the reason,
    and the open records."""
    by_kind: dict[str, list] = {"iri": [], "json": [], "open": []}
    for n in notes:
        by_kind[n.kind].append(n)
    lines = [
        f"{len(by_kind['iri'])} IRI position(s) with no pointer pattern (the generic IRI rule: a bare id and a "
        "per-row section, no declared foreign key; ADR 0008 decision 1.5):"
    ]
    lines += [f"  {n.section}: {n.position}" for n in by_kind["iri"]]
    lines.append(f"{len(by_kind['json'])} JSON-text position(s) (VARCHAR holding compact JSON text):")
    lines += [f"  {n.section}: {n.position}: {n.reason}" for n in by_kind["json"]]
    if by_kind["open"]:
        lines.append(
            f"{len(by_kind['open'])} open record class(es) (a property the class doesn't declare stops the build):"
        )
        lines += [f"  {n.section}" for n in by_kind["open"]]
    return "\n".join(lines) + "\n"


def run_generate_command(args) -> int:
    """`generate`: write a schema's default column set and report what the policy settled; with --check, say
    whether a column set complies, listing the differences. Returns the exit code."""
    import sys

    from .errors import InputError

    if args.bundle_schema is None:
        raise InputError(
            "--bundle-schema is required: the bundle schema whose default is generated (a file, a URL or a "
            "schema-set directory); the tool has no default bundle (ADR 0009)"
        )
    if args.check:
        if args.column_set is None:
            raise InputError("--check needs --column-set: the column set to check against the default")
        _, _, differences = compliance(str(args.bundle_schema), str(args.column_set))
        if not differences:
            print(f"{args.column_set}: complies: it compiles to the default (policy {RULES_VERSION})")
            return 0
        print(
            f"{args.column_set}: differs from the default (policy {RULES_VERSION}) in {len(differences)} place(s):"
        )
        for d in differences:
            print(f"  {d}")
        return 1
    if args.column_set is not None:
        raise InputError("--column-set is read only with --check")
    gen, _ = generated(str(args.bundle_schema), args.version_property)
    if args.out is not None:
        Path(args.out).write_text(gen.text, encoding="utf-8")
    else:
        sys.stdout.write(gen.text)
    sys.stderr.write(generate_report(gen.notes))
    return 0
