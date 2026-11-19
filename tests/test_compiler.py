"""The compiler (ADR 0006): on the real 1.1.0 schemas and its column set it reproduces the golden spec and the
build; it refuses a bad column-set entry and any pattern its rules don't cover.

Offline: the schemas are ClinVar-GKM's 1.1.0 set, a test fixture (tests/fixtures/clinvar-gkm). Bundle records are
real (the excerpt, tests/_fixture.py). The small schema sets of the fail-loud tests are hand-written schemas, not
records.
"""

from __future__ import annotations

import copy
import dataclasses
import gzip
import json
import re
from pathlib import Path

import duckdb
import pytest
import yaml
from _fixture import CLINVAR_COLUMNS, CLINVAR_SCHEMA, FIXTURE, GOLDEN_SECTIONS_SPEC, SECTIONS, SPEC, SPEC_SOURCE
from _input_helpers import local_input

from gkm_bundle_parquet import build as build_mod
from gkm_bundle_parquet.columnset import load_column_set, parse_column_set
from gkm_bundle_parquet.compiler import (
    compile_sections,
    generate_default,
    root_parts,
    spec_differences,
    table_spec_sha256,
)
from gkm_bundle_parquet.errors import CompileError
from gkm_bundle_parquet.model import BuildInputs
from gkm_bundle_parquet.roundtrip import rebuild
from gkm_bundle_parquet.schemas import SchemaFile, SchemaSet, load_schema_set
from gkm_bundle_parquet.spec import Spec, Table, check_compiled, parse_spec

SCHEMAS = load_schema_set(str(CLINVAR_SCHEMA))
COLUMN_SET = load_column_set(str(CLINVAR_COLUMNS))
GOLDEN = parse_spec(yaml.safe_load(GOLDEN_SECTIONS_SPEC.read_text(encoding="utf-8")), require_name=True)
COMPILED, SOURCE = SPEC, SPEC_SOURCE


def columns() -> dict:
    return copy.deepcopy(COLUMN_SET.doc)


def checked(raw: dict) -> Spec:
    spec = parse_spec(raw, require_name=True)
    check_compiled(spec, "the test's spec")
    return spec


# --- golden: the 1.1.0 spec the research produced ----------------------------------------------------------------


def _shape(type_str: str) -> object:
    """A DuckDB type with STRUCT field order removed."""

    def norm(t):
        if t.id == "struct":
            return ("struct", tuple(sorted((n, norm(c)) for n, c in t.children)))
        if t.id == "list":
            return ("list", norm(t.children[0][1]))
        return str(t)

    return norm(duckdb.sqltype(type_str))


def _column_facts(t: Table, *, ordered: bool) -> dict:
    facts = {}
    for c in t.columns:
        d = c.derivation
        pointers = dict(zip(d.pointers, d.pointer_targets, strict=True))
        facts[c.name] = (c.type if ordered else _shape(c.type), c.nulls, d.kind, d.path, d.targets, pointers)
    return facts


# The golden spec (tests/fixtures/sections-golden/tables.yaml) is the 1.0 research. The compiler reproduces it,
# beyond column and STRUCT field order (a record's canonical form doesn't depend on it; the golden spec's key_order
# tables take the column set's order, which tool 2.0.0 dropped), with the ADR 0007 rules applied by `_as_adr_0007`.
_ID_PART = ("struct", (("id", "VARCHAR"), ("section", "VARCHAR")))


def _iris(shape: object) -> object:
    """An `iris` list (iri-reference, no pattern) takes the generic IRI rule: one STRUCT(id, section) each."""
    if isinstance(shape, tuple) and shape[0] == "struct":
        return ("struct", tuple((n, ("list", _ID_PART) if n == "iris" else _iris(s)) for n, s in shape[1]))
    if isinstance(shape, tuple) and shape[0] == "list":
        return ("list", _iris(shape[1]))
    return shape


def _as_adr_0007(facts: dict) -> dict:
    """The golden facts under ADR 0007: no ordinal column; `iris` by the generic IRI rule; a list of pointers with
    several targets is one STRUCT(id, section) per element, not parallel `<prop>` and `<prop>_section` lists."""
    out = {}
    for name, (shape, nulls, kind, path, targets, pointers) in facts.items():
        if kind == "ordinal" or kind == "pointer-section" and shape[0] == "list":
            continue
        if kind == "bare-id" and shape[0] == "list" and len(targets) > 1:
            shape, kind, targets = ("list", _ID_PART), "typed-copy", ()
        out[name] = (_iris(shape), nulls, kind, path, targets, pointers)
    return out


@pytest.mark.parametrize("golden", GOLDEN.tables, ids=lambda t: t.name)
def test_compiled_table_equals_the_golden_table(golden):
    t = COMPILED.table(golden.name)
    got, want = _column_facts(t, ordered=False), _as_adr_0007(_column_facts(golden, ordered=False))
    # ADR 0007: a record that carries an `id` its class doesn't require has `id_present` (the golden spec's
    # record_id said it carries one); `json.record_id` now says the class requires it.
    optional_id = golden.json.record_id and not t.json.record_id
    assert got.pop("id_present", None) == (
        ("BOOLEAN", "never", "id-present", "$.id", (), {}) if optional_id else None
    )
    differ = sorted(n for n in set(got) | set(want) if got.get(n) != want.get(n))
    assert differ == [], [(n, got.get(n), want.get(n)) for n in differ]
    names = [c.name for c in t.columns]
    assert "ordinal" not in names and (names[0] == "id" or "id" not in names)
    assert t.sections == golden.sections and t.row_filter is None
    assert t.extension_slots == golden.extension_slots
    assert t.json == dataclasses.replace(golden.json, record_id=t.json.record_id)
    assert t.known_fields is golden.known_fields is True
    assert t.sort_key == golden.sort_key == "id"


def test_tables_are_the_bundle_schema_collections_in_order():
    assert tuple(t.name for t in COMPILED.tables) == SECTIONS == tuple(t.name for t in GOLDEN.tables)


def test_compiled_spec_keeps_the_top_level_facts():
    assert COMPILED.lossless and COMPILED.writer == GOLDEN.writer
    # The golden spec is the 1.0 research; tool 2.0.0 dropped key_order, a major change (compiler.RULES_VERSION).
    assert (COMPILED.name, COMPILED.spec_version, GOLDEN.spec_version) == (GOLDEN.name, "2.0", "1.0")
    assert COMPILED.bundle_schema_versions == ("1.1.0",)
    assert SCHEMAS.sha256 in COMPILED.sources[0]
    assert (COMPILED.root.version_property, COMPILED.root.other_values) == ("schema_version", False)


def test_the_source_records_the_pairing():
    assert (SOURCE.name, SOURCE.version, SOURCE.builtin, SOURCE.path) == ("sections", "2.0", True, None)
    assert SOURCE.schema_set_sha256 == SCHEMAS.sha256 and SOURCE.column_set_sha256 == COLUMN_SET.sha256
    assert SOURCE.sha256 == table_spec_sha256(compile_sections(SCHEMAS, COLUMN_SET.doc))


def test_tables_take_schema_order():
    assert [c.name for c in COMPILED.table("location").columns] == [
        "id",
        "id_present",
        "type",
        "digest",
        "sequenceReference",
        "loc_start",
        "loc_start_min",
        "loc_start_max",
        "loc_end",
        "loc_end_min",
        "loc_end_max",
    ]
    doc = columns()
    doc["sections"]["submitter"] = {"id": None, "name": None, "type": None}
    assert [c.name for c in checked(compile_sections(SCHEMAS, doc)).table("submitter").columns] == [
        "id",
        "id_present",
        "type",
        "name",
    ]


def test_output_is_deterministic_and_ignores_entry_order_in_a_sorted_table():
    assert compile_sections(SCHEMAS, COLUMN_SET.doc) == compile_sections(SCHEMAS, COLUMN_SET.doc)
    doc = columns()
    doc["sections"]["location"] = dict(reversed(list(doc["sections"]["location"].items())))
    assert compile_sections(SCHEMAS, doc) == compile_sections(SCHEMAS, COLUMN_SET.doc)


def test_descriptions_come_from_the_schema_and_transforms_from_the_rules():
    t = COMPILED.table("location")
    assert t.description == "VRS SequenceLocation records."
    assert t.column("sequenceReference").description.startswith("A reference to a SequenceReference")
    assert t.column("sequenceReference").derivation.transform == "strip ^#/[^/]+/"
    assert COMPILED.table("scv").column("proposition_section").derivation.transform.startswith("the <section>")


# --- build equivalence: the real excerpt -------------------------------------------------------------------------


def _full(path: Path) -> BuildInputs:
    return BuildInputs(full=local_input(path))


def _rows(directory: Path, table: Table, names: list[str]) -> list[tuple]:
    selected = ", ".join(f'"{n}"' for n in names)
    path = directory / table.file_name
    return duckdb.connect().execute(f"SELECT {selected} FROM read_parquet('{path}') ORDER BY id").fetchall()


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("compiled")
    build_mod.build(_full(FIXTURE), root / "compiled", spec=COMPILED, spec_source=SOURCE)
    # The golden spec predates `root`; it reads the bundle the way the compiled one does.
    build_mod.build(_full(FIXTURE), root / "golden", spec=dataclasses.replace(GOLDEN, root=COMPILED.root))
    return root / "compiled", root / "golden"


def test_build_with_the_compiled_spec_rebuilds_the_bundle_records(built, tmp_path):
    out = tmp_path / "rebuilt.json"
    result = rebuild(built[0], out)
    assert json.loads(out.read_bytes()) == json.loads(gzip.decompress(FIXTURE.read_bytes()))
    assert result.checked and result.records_sha256 == result.expected_records_sha256


@pytest.mark.parametrize("golden", GOLDEN.tables, ids=lambda t: t.name)
def test_rows_equal_those_of_the_golden_spec_by_column_name(built, golden):
    # The columns ADR 0007 left as they were: the others hold the same values in another form (_as_adr_0007).
    got = _column_facts(COMPILED.table(golden.name), ordered=False)
    names = [n for n, f in _column_facts(golden, ordered=False).items() if got.get(n) == f]
    assert "id" in names
    compiled_rows = _rows(built[0], COMPILED.table(golden.name), names)
    assert compiled_rows == _rows(
        built[1], golden, names
    )  # DuckDB returns a STRUCT as a dict: field order is free


def test_a_sorted_table_really_is_written_in_schema_order(built):
    path = built[0] / "sequenceReference.parquet"
    got = [r[0] for r in duckdb.connect().execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()]
    assert got == [c.name for c in COMPILED.table("sequenceReference").columns]
    assert got != [c.name for c in GOLDEN.table("sequenceReference").columns]


# --- the checksum covers what affects the tables -----------------------------------------------------------------


def _raw() -> dict:
    return copy.deepcopy(compile_sections(SCHEMAS, COLUMN_SET.doc))


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r["tables"][0].update(description="reworded"),
        lambda r: r["tables"][0]["columns"][1].update(description="reworded"),
        lambda r: r["tables"][1]["columns"][-2]["derivation"].update(transform="reworded"),
        lambda r: r.update(sources=["reworded"]),
        lambda r: r.update(status="draft"),
    ],
)
def test_rewording_leaves_the_checksum_unchanged(change):
    raw = _raw()
    change(raw)
    assert table_spec_sha256(raw) == SOURCE.sha256


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r["tables"][0]["columns"][1].update(type="BIGINT"),
        lambda r: r["tables"][0]["columns"].reverse(),
        lambda r: r["tables"][0]["columns"][1]["derivation"].update(path="$.other"),
        lambda r: r["tables"][1]["columns"][3]["derivation"].update(targets=["gene"]),
        lambda r: r["tables"][0]["json"].update(record_id=True),
        lambda r: r["root"].update(version_property=None),
        lambda r: r["tables"].reverse(),
    ],
)
def test_changing_what_builds_the_tables_changes_the_checksum(change):
    raw = _raw()
    change(raw)
    assert table_spec_sha256(raw) != SOURCE.sha256


# --- column-set strictness ---------------------------------------------------------------------------------------


def _set_path(doc: dict, path: tuple, value: object) -> None:
    node = doc["sections"]
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = value


@pytest.mark.parametrize(
    "change, message",
    [
        # Never used.
        (lambda o: o["sections"].update(notASection={"id": None}), r"sections names \['notASection'\]"),
        (
            lambda o: o["sections"].update(gene=None),
            "section gene: .*the collection has no entry in the column set",
        ),
        (lambda o: o["sections"].pop("gene"), "section gene: .*the collection has no entry in the column set"),
        (lambda o: o.update(collections=["gene"]), "collections names 'gene', but the schema already says"),
        (lambda o: o.update(collections=["schema_version"]), "'schema_version' is the version, not a collection"),
        (lambda o: o.update(collections=["nope"]), "collections names 'nope', not a root property"),
        (lambda o: o.update(version_property="nope"), "version_property 'nope' is not a root property"),
        # The pairing.
        (
            lambda o: o.update(schema="urn:other"),
            "it says it pairs with schema 'urn:other', but the schema's \\$id is",
        ),
        # A position the schema doesn't have.
        (
            lambda o: _set_path(o, ("location", "nonsense"), None),
            r"section location: position \(the record\): names \['nonsense'\].*it has: id, type",
        ),
        (
            lambda o: _set_path(o, ("allele", "state", "bogus"), None),
            r"section allele: position state: names \['bogus'\]",
        ),
        # Contradicting the schema's type.
        (
            lambda o: _set_path(o, ("location", "digest"), "integer"),
            "position digest: contradicts the schema: it says integer, the schema allows string",
        ),
        (
            lambda o: _set_path(o, ("copyNumberCount", "copies"), "string"),
            "position copies: contradicts the schema: it says string, the schema allows integer, a Range",
        ),
        (
            lambda o: _set_path(o, ("copyNumberCount", "copies"), "->location"),
            "position copies: contradicts the schema: it says a pointer, the schema allows integer, a Range",
        ),
        (
            lambda o: _set_path(o, ("gene", "primaryCoding"), "->gene"),
            "position primaryCoding: contradicts the schema: it says a pointer, the schema allows an object",
        ),
        (
            lambda o: _set_path(o, ("location", "digest"), {"a": None}),
            "position digest: contradicts the schema: it says an object, the schema allows string",
        ),
        (
            lambda o: _set_path(o, ("location", "digest"), ["string"]),
            "position digest: contradicts the schema: it says a list, the schema allows string",
        ),
        (lambda o: _set_path(o, ("location", "digest"), "strng"), "position digest: unknown shape 'strng'"),
        (
            lambda o: _set_path(o, ("location", "sequenceReference"), "->nowhere"),
            r"position sequenceReference: a pointer names \['nowhere'\], not collections",
        ),
        (
            lambda o: _set_path(o, ("sequenceReference", "extensions"), {"a": None}),
            "position extensions: an extensions shape is the list of its value shapes",
        ),
        (lambda o: _set_path(o, ("gene", "id as gene_id"), None), "lists 'id' twice"),
    ],
)
def test_a_bad_column_set_entry_stops_the_compile(change, message):
    doc = columns()
    change(doc)
    with pytest.raises(CompileError, match=message):
        compile_sections(SCHEMAS, doc)


def test_a_column_set_with_key_order_is_refused():
    doc = columns()
    doc["key_order"] = {"submitter": None}
    with pytest.raises(CompileError, match="key_order is gone in tool 2.0.0"):
        parse_column_set(yaml.safe_dump(doc).encode(), "test")


def test_the_record_id_cant_be_renamed():
    doc = columns()
    entry = doc["sections"]["submitter"]
    entry.pop("id")
    entry["id as x"] = None
    with pytest.raises(CompileError, match="it can't be renamed"):
        compile_sections(SCHEMAS, doc)


def test_an_open_extension_value_may_name_positions_the_schema_lacks():
    doc = columns()
    doc["sections"]["sequenceReference"]["extensions"] = [{"bogus": "string"}]
    t = parse_spec(compile_sections(SCHEMAS, doc)).table("sequenceReference")
    assert "value_object STRUCT(bogus VARCHAR)" in t.column("extensions").type


def test_a_position_the_schema_says_nothing_about_is_json_text():
    doc = columns()
    doc["sections"]["sequenceReference"]["extensions"] = [{"bogus": None}]
    c = parse_spec(compile_sections(SCHEMAS, doc)).table("sequenceReference").column("extensions")
    assert c.type == 'STRUCT("name" VARCHAR, value_object STRUCT(bogus VARCHAR))[]'
    assert c.derivation.parts["[*].value_object.bogus"] == {"form": "json"}


def test_a_column_set_keys_are_checked_when_it_loads():
    with pytest.raises(CompileError, match=r"unknown key\(s\) \['notes'\]"):
        parse_column_set(b"schema: x\nsections: {a: {b}}\nnotes: y\n", "here")
    with pytest.raises(CompileError, match="`schema` must name the \\$id"):
        parse_column_set(b"sections: {a: {b}}\n", "here")
    with pytest.raises(CompileError, match="`sections` must map"):
        parse_column_set(b"schema: x\n", "here")
    with pytest.raises(CompileError, match=r"can't read it.*\['a'\] written twice"):
        parse_column_set(b"schema: x\nsections:\n  t: {a, b, a}\n", "here")


def test_the_1_1_0_column_set_entries_parse_as_flow_mappings():
    assert COLUMN_SET.doc["sections"]["location"]["id"] is None
    assert COLUMN_SET.doc["sections"]["rcv"] is COLUMN_SET.doc["sections"]["vcv"]


# --- fail-loud rules: small hand-written schema sets -------------------------------------------------------------

ROOT = "urn:example:bundle:1"
W3 = "https://w3id.org/example/json"
STRING = {"type": "string"}


def closed(**properties) -> dict:
    return {"type": "object", "properties": properties, "additionalProperties": False}


def make_set(sections: dict[str, dict], classes: dict[str, dict] | None = None, **bundle_keys) -> SchemaSet:
    """A schema set: sections (name -> record schema) and classes (name -> schema, at W3/<name>)."""
    props = {
        name: {"type": "object", "patternProperties": {".+": record}, "additionalProperties": False}
        for name, record in sections.items()
    }
    bundle = {"$id": ROOT, "properties": props, **bundle_keys}
    files = {ROOT: SchemaFile(ROOT, ROOT, json.dumps(bundle).encode(), bundle)}
    for name, doc in (classes or {}).items():
        sid = f"{W3}/{name}"
        doc = {"$id": sid, **doc}
        files[sid] = SchemaFile(sid, sid, json.dumps(doc).encode(), doc)
    return SchemaSet(ROOT, files)


def ref(name: str) -> dict:
    return {"$ref": f"{W3}/{name}"}


def all_of(schema_set: SchemaSet, **overrides) -> dict:
    """A column set taking every declared property of every collection as the schema states it."""
    sections = {}
    for name, node in schema_set.bundle["properties"].items():
        if "patternProperties" not in node:
            continue
        record = next(iter(node["patternProperties"].values()))
        sections[name] = dict.fromkeys(record.get("properties", {})) or {"x": None}
    return {"schema": schema_set.bundle_id, "sections": sections, **overrides}


def compile_all(schema_set: SchemaSet, **overrides) -> dict:
    return compile_sections(schema_set, all_of(schema_set, **overrides))


RANGE = {
    "type": "array",
    "minItems": 2,
    "maxItems": 2,
    "items": {"oneOf": [{"type": "integer"}, {"type": "null"}]},
}
INT_OR_RANGE = {"oneOf": [{"type": "integer"}, RANGE]}
IRI = {"type": "string", "format": "iri-reference"}


# ADR 0007: generation stops only on a pointer pattern it can't read or naming something that isn't a collection,
# and on a derived name clash. A $ref that resolves to nothing and an `id` that isn't a string still stop it.
@pytest.mark.parametrize(
    "record, message",
    [
        (closed(x=ref("Missing")), r"position x: unknown \$ref.*no schema .*Missing in the set"),
        (closed(x={"$ref": "#/$defs/Missing"}), r"position x: unknown \$ref.*no '\$defs'"),
        (closed(id={"type": "integer"}), "position id: the record's id must be a string"),
        # ADR 0007 decision 1.6: a `^#/` pattern of another form, or naming what isn't a collection.
        (
            closed(x={"type": "string", "pattern": "^#/u"}),
            r"pattern '\^#/u' begins \^#/ but is not \^#/<section>/",
        ),
        (closed(x={"type": "string", "pattern": "^#/[a-z]+/"}), "begins \\^#/ but is not"),
        (
            closed(x={"type": "string", "pattern": "^#/nope/"}),
            r"position x: pattern '\^#/nope/' names \['nope'\], not",
        ),
        (
            closed(x=closed(y={"type": "array", "items": {"type": "string", "pattern": "^#/(u|nope)/"}})),
            r"position x\.y\[\*\]: pattern .* names \['nope'\]",
        ),
        # ADR 0007 decision 1.13: a derived name equal to a property name at the same level.
        (
            closed(x=INT_OR_RANGE, x_min=STRING),
            "the column name 'x_min' of property 'x' is also the name of 'x_min'",
        ),
        (closed(x={"oneOf": [IRI, closed(a=STRING)]}, x_json=STRING), "the column name 'x_json' of property 'x'"),
        (closed(x={"type": "string", "pattern": "^#/(t|u)/"}, x_section=STRING), "column name 'x_section'"),
        (closed(o=closed(x=INT_OR_RANGE, x_max=STRING)), "position o: the derived name 'x_max' of property 'x'"),
        (
            closed(extensions={"type": "array", "items": closed(name=STRING, value=STRING, value_string=STRING)}),
            "the derived name 'value_string' of the extension value is also the name of a field",
        ),
    ],
)
def test_what_stops_generation(record, message):
    schema_set = make_set({"t": record, "u": closed(a=STRING)})
    with pytest.raises(CompileError, match=message):
        generate_default(schema_set, None)


def _generated(record: dict, classes: dict | None = None) -> tuple[Table, list]:
    g = generate_default(make_set({"t": record, "u": closed(a=STRING)}, classes), None)
    return parse_spec(g.raw).table("t"), [n for n in g.notes if n.section == "t"]


def _facts(t: Table) -> list[tuple]:
    return [(c.name, c.type, c.derivation.kind) for c in t.columns if c.name != "id"]


# ADR 0007 decision 1.12: what the schema can't settle is the value as compact JSON text (a VARCHAR), reported.
@pytest.mark.parametrize(
    "record, classes, reason",
    [
        (closed(x={"type": "object"}), None, "an open object"),
        (closed(x={"type": "object", "properties": {"a": STRING}, "additionalProperties": True}), None, "an open"),
        (closed(x={"type": ["string", "integer"]}), None, "a union of string, integer"),
        (closed(x={"oneOf": [STRING, {"type": "boolean"}]}), None, "a union of string, boolean"),
        (closed(x={"description": "nothing"}), None, "the schema says nothing about its value"),
        (closed(x={"if": {}, "then": {}, "type": "string"}), None, "uses if, which the rules can't read"),
        (closed(x={"type": "object", "additionalProperties": STRING}), None, "a map whose keys are data"),
        (closed(x=ref("Loop")), {"Loop": {"oneOf": [ref("Loop"), STRING]}}, "reaches itself through \\$ref alone"),
    ],
)
def test_what_the_schema_cant_settle_is_json_text(record, classes, reason):
    t, notes = _generated(record, classes)
    assert _facts(t) == [("x", "VARCHAR", "json-text")]
    assert [(n.position, n.kind) for n in notes] == [("x", "json")] and re.search(reason, notes[0].reason)


def test_a_class_that_reaches_itself_is_json_text_where_it_re_enters():
    t, notes = _generated(closed(x=ref("Node")), {"Node": closed(name=STRING, child=ref("Node"))})
    c = t.column("x")
    assert (c.type, c.derivation.parts) == ('STRUCT("name" VARCHAR, child VARCHAR)', {".child": {"form": "json"}})
    assert [(n.position, n.kind) for n in notes] == [("x.child", "json")] and "reaches itself" in notes[0].reason
    t, _ = _generated(closed(x=ref("A")), {"A": closed(b=ref("B")), "B": closed(a=ref("A"))})
    assert t.column("x").type == "STRUCT(b STRUCT(a VARCHAR))"


def test_a_range_is_known_by_its_shape_at_any_depth():
    t, _ = _generated(
        closed(n=INT_OR_RANGE, r=RANGE, x=closed(n=INT_OR_RANGE), l={"type": "array", "items": RANGE})
    )
    assert _facts(t) == [
        ("n", "BIGINT", "range-exact"),
        ("n_min", "BIGINT", "range-min"),
        ("n_max", "BIGINT", "range-max"),
        ("r", "BIGINT", "range-exact"),
        ("r_min", "BIGINT", "range-min"),
        ("r_max", "BIGINT", "range-max"),
        ("x", "STRUCT(n BIGINT, n_min BIGINT, n_max BIGINT)", "typed-copy"),
        ("l", 'STRUCT("integer" BIGINT, min BIGINT, max BIGINT)[]', "typed-copy"),
    ]
    assert t.column("x").derivation.parts == {".n": {"form": "range"}}
    assert t.column("l").derivation.parts == {"[*]": {"form": "range"}}


def test_a_string_or_object_is_the_string_and_the_object_as_json():
    t, notes = _generated(closed(x={"oneOf": [IRI, closed(a=STRING)]}, y={"oneOf": [STRING, closed(a=STRING)]}))
    assert _facts(t) == [
        ("x", "VARCHAR", "part"),
        ("x_section", "VARCHAR", "part"),
        ("x_json", "VARCHAR", "part"),
        ("y", "VARCHAR", "part"),
        ("y_json", "VARCHAR", "part"),
    ]
    assert [c.derivation.part for c in t.columns[1:]] == ["id", "section", "json", "string", "json"]
    assert {(n.position, n.kind) for n in notes} == {("x", "iri"), ("x", "json"), ("y", "json")}


def test_an_iri_reference_with_no_pattern_takes_the_generic_rule_and_is_not_a_foreign_key():
    t, notes = _generated(closed(x=IRI, l={"type": "array", "items": IRI}))
    assert _facts(t) == [("x", "VARCHAR", "part"), ("x_section", "VARCHAR", "part"), ("l", _ID_LIST, "typed-copy")]
    assert t.column("x").derivation.encoding == {"form": "iri", "targets": ["t", "u"]}
    assert t.foreign_keys == () and [(n.position, n.kind) for n in notes] == [("x", "iri"), ("l[*]", "iri")]


_ID_LIST = "STRUCT(id VARCHAR, section VARCHAR)[]"


def test_a_pointer_pattern_is_a_foreign_key_at_any_depth():
    one, several = {"type": "string", "pattern": "^#/u/"}, {"type": "string", "pattern": "^#/(t|u)/"}
    record = closed(a=one, b=several, c=closed(d=one, e=several), f={"type": "array", "items": several})
    t, notes = _generated(record)
    assert _facts(t) == [
        ("a", "VARCHAR", "bare-id"),
        ("b", "VARCHAR", "bare-id"),
        ("b_section", "VARCHAR", "pointer-section"),
        ("c", "STRUCT(d VARCHAR, e VARCHAR, e_section VARCHAR)", "typed-copy"),
        ("f", _ID_LIST, "typed-copy"),
    ]
    assert t.foreign_keys == (
        {"position": "a", "targets": ["u"], "section": None},
        {"position": "b", "targets": ["t", "u"], "section": "b_section"},
        {"position": "c.d", "targets": ["u"], "section": None},
        {"position": "c.e", "targets": ["t", "u"], "section": "c.e_section"},
        {"position": "f[*].id", "targets": ["t", "u"], "section": "f[*].section"},
    )
    assert notes == []


def test_prop_false_has_no_column_and_every_allowed_property_has_one_at_any_depth():
    t, _ = _generated(closed(a=STRING, b=False, c=closed(d=STRING, e=False, f={"type": "boolean"})))
    assert _facts(t) == [("a", "VARCHAR", "scalar-path"), ("c", "STRUCT(d VARCHAR, f BOOLEAN)", "typed-copy")]


def test_an_array_with_no_items_holds_json_text():
    t, notes = _generated(closed(x={"type": "array"}))
    assert _facts(t) == [("x", "VARCHAR[]", "typed-copy")] and t.column("x").derivation.parts == {
        "[*]": {"form": "json"}
    }
    assert [(n.position, n.kind) for n in notes] == [("x[*]", "json")]


@pytest.mark.parametrize(
    "items, element",
    [
        # Extensions are known by shape, not name: a closed {name, value}, other fields kept before the slots.
        (
            closed(name=STRING, value=STRING, extra=STRING),
            'STRUCT("name" VARCHAR, extra VARCHAR, value_string VARCHAR)',
        ),
        (closed(name=STRING, value={"type": "object"}), 'STRUCT("name" VARCHAR, value_json VARCHAR)'),
        (closed(name=STRING, value={"type": "array"}), 'STRUCT("name" VARCHAR, value_json VARCHAR)'),
        # Not the shape: an open element is JSON text.
        ({"type": "object"}, "VARCHAR"),
    ],
)
def test_extensions_are_read_by_shape(items, element):
    t, _ = _generated(closed(extensions={"type": "array", "items": items}))
    assert t.column("extensions").type == f"{element}[]"


def test_an_extensions_property_that_is_not_a_list_is_a_plain_column():
    t, _ = _generated(closed(extensions=STRING))
    assert _facts(t) == [("extensions", "VARCHAR", "scalar-path")]


def test_an_optional_id_has_id_present_and_a_required_one_is_the_key():
    t, _ = _generated(closed(id=STRING, a=STRING))
    assert [c.name for c in t.columns] == ["id", "id_present", "a"] and not t.json.record_id
    t, _ = _generated({**closed(id=STRING, a=STRING), "required": ["id"]})
    assert [c.name for c in t.columns] == ["id", "a"] and t.json.record_id
    t, _ = _generated(closed(a=STRING))
    assert [c.name for c in t.columns] == ["id", "a"] and not t.json.record_id


def test_an_open_record_class_is_noted():
    _, notes = _generated({"type": "object", "properties": {"a": STRING}})
    assert [(n.position, n.kind) for n in notes] == [("(the record)", "open")]


def test_generation_is_deterministic_and_the_column_set_text_reparses_to_the_same_spec():
    schema_set = make_set({"t": closed(a=IRI, b=INT_OR_RANGE), "u": closed(a=STRING)})
    g = generate_default(schema_set, None)
    assert generate_default(schema_set, None).text == g.text
    doc = parse_column_set(g.text.encode(), "generated").doc
    assert table_spec_sha256(compile_sections(schema_set, doc)) == table_spec_sha256(g.raw)


def test_a_column_set_differs_from_the_default_by_what_it_narrows():
    schema_set = make_set({"t": closed(a=STRING, b=STRING), "u": closed(a=STRING)})
    default = generate_default(schema_set, None).raw
    narrowed = compile_sections(schema_set, {"schema": ROOT, "sections": {"t": {"a": None}, "u": {"a": None}}})
    assert spec_differences(default, default) == []
    assert any("column b: only in the default" in d for d in spec_differences(default, narrowed))


def test_a_multi_target_pointer_inside_a_value_is_a_struct_of_id_and_section():
    schema_set = make_set({"t": closed(x=closed(p=STRING)), "u": closed(a=STRING)})
    doc = all_of(schema_set)
    doc["sections"]["t"] = {"x": {"p": "->t|u"}}
    t = parse_spec(compile_sections(schema_set, doc)).table("t")
    assert t.column("x").type == "STRUCT(p VARCHAR, p_section VARCHAR)"
    assert t.foreign_keys == ({"position": "x.p", "targets": ["t", "u"], "section": "x.p_section"},)


def test_a_value_shape_with_parts_cant_be_an_extension_slot():
    extensions = {"type": "array", "items": closed(name=STRING, value=STRING)}
    schema_set = make_set({"t": closed(extensions=extensions), "u": closed(a=STRING)})
    doc = all_of(schema_set)
    doc["sections"]["t"] = {"extensions": ["->t|u"]}
    with pytest.raises(CompileError, match="a value shape '->t|u' has parts, which an extension slot can't hold"):
        compile_sections(schema_set, doc)


def test_a_bundle_level_allof_the_rules_cant_read_stops_the_compile():
    with pytest.raises(CompileError, match=r"allOf\[0\]: the rules read only patternProperties"):
        compile_all(make_set({"t": closed(x=STRING)}, allOf=[{"required": ["t"]}]))


def test_a_bad_section_pattern_in_a_bundle_allof_stops_the_compile():
    allof = [{"patternProperties": {"(": {"additionalProperties": closed()}}}]
    with pytest.raises(CompileError, match=r"allOf\[0\]: bad pattern '\('"):
        compile_all(make_set({"t": closed(x=STRING)}, allOf=allof))


def test_a_schema_of_the_wrong_json_shape_stops_the_compile():
    schema_set = make_set({"t": closed(x=STRING)})
    record = schema_set.bundle["properties"]["t"]["patternProperties"][".+"]
    record["allOf"] = [5]
    with pytest.raises(CompileError, match="section t: malformed"):
        compile_all(schema_set)


def test_a_bundle_with_no_collections_stops_the_compile():
    with pytest.raises(CompileError, match="it has no collections"):
        compile_sections(make_set({}), {"schema": ROOT, "sections": {"a": {"b": None}}})


def test_a_collection_cant_take_the_root_values_tables_name():
    schema_set = make_set({"root_values": closed(x=STRING)})
    with pytest.raises(CompileError, match="can't be named root_values"):
        compile_all(schema_set)


def test_an_integer_or_range_at_the_top_level_is_three_columns_and_may_be_renamed():
    schema_set = make_set({"t": closed(id=STRING, n=INT_OR_RANGE)})
    t = parse_spec(compile_all(schema_set)).table("t")
    assert [(c.name, c.type, c.derivation.kind, c.derivation.path) for c in t.columns[1:]] == [
        ("id_present", "BOOLEAN", "id-present", "$.id"),
        ("n", "BIGINT", "range-exact", "$.n"),
        ("n_min", "BIGINT", "range-min", "$.n"),
        ("n_max", "BIGINT", "range-max", "$.n"),
    ]
    assert t.json.record_id is False
    doc = all_of(schema_set)
    doc["sections"]["t"] = {"id": None, "n as m": None}
    t = parse_spec(compile_sections(schema_set, doc)).table("t")
    assert [c.name for c in t.columns] == ["id", "id_present", "m", "m_min", "m_max"]
    assert {c.derivation.path for c in t.columns[2:]} == {"$.n"}


def test_a_section_with_no_id_property_has_no_record_id():
    assert parse_spec(compile_all(make_set({"t": closed(x=STRING)}))).table("t").json.record_id is False


def test_a_union_merges_first_branch_then_new_fields_and_allof_narrows():
    base = closed(a=STRING, b={"type": "integer"})
    # The profile narrows `a`'s value to a const of the same type; `minimum` says nothing about the columns.
    narrowing = {"properties": {"a": {"const": "x"}, "b": {"minimum": 1}}}
    profile = {"allOf": [ref("Base"), narrowing], "type": "object", "unevaluatedProperties": False}
    record = closed(x={"anyOf": [ref("Profile"), closed(c=STRING, a=STRING)]})
    spec = parse_spec(compile_all(make_set({"t": record}, {"Base": base, "Profile": profile})))
    assert spec.table("t").column("x").type == "STRUCT(a VARCHAR, b BIGINT, c VARCHAR)"


def test_an_allof_narrowing_keeps_what_its_base_declares():
    """A profile's {properties: {...}} on a property its base declares leaves the base's other properties there."""
    base = closed(s={"oneOf": [closed(type=STRING, code=STRING), IRI]})
    profile = {"allOf": [ref("Base"), {"properties": {"s": {"properties": {"code": {"const": "x"}}}}}]}
    schema_set = make_set({"t": profile}, {"Base": base})
    doc = {"schema": ROOT, "sections": {"t": {"s": {"type": None, "code": None}}}}
    assert parse_spec(compile_sections(schema_set, doc)).table("t").column("s").type == (
        'STRUCT("type" VARCHAR, code VARCHAR)'
    )


def test_a_pointer_shape_is_a_bare_id_in_the_column_with_its_section_when_it_has_several():
    record = closed(a=IRI, b={"type": "array", "items": IRI}, c=closed(d=IRI), e=IRI)
    schema_set = make_set({"t": record, "u": closed(x=STRING)})
    doc = all_of(schema_set)
    doc["sections"]["t"] = {"a": "->u", "b": "->t|u", "c": {"d": "->u"}, "e": None}
    t = parse_spec(compile_sections(schema_set, doc)).table("t")
    assert (t.column("a").derivation.kind, t.column("a").derivation.targets) == ("bare-id", ("u",))
    assert t.column("b").type == "STRUCT(id VARCHAR, section VARCHAR)[]"
    assert t.column("b").derivation.parts == {"[*]": {"form": "pointer", "targets": ["t", "u"]}}
    assert dict(zip(t.column("c").derivation.pointers, t.column("c").derivation.pointer_targets, strict=True)) == {
        ".d": ("u",)
    }
    # An IRI with no pattern and no shape takes the generic IRI rule (ADR 0007 decision 1.7).
    assert [(c.derivation.kind, c.derivation.part) for c in (t.column("e"), t.column("e_section"))] == [
        ("part", "id"),
        ("part", "section"),
    ]


def test_closed_extensions_are_read_from_the_schema_with_slots_in_order():
    item = closed(name=STRING, value={"type": "array", "items": closed(g=STRING)})
    extensions = {
        "type": "array",
        "items": {
            "oneOf": [
                item,
                closed(name=STRING, value={"type": "boolean"}),
                closed(name=STRING, value={"type": "array", "items": STRING}),
                closed(name=STRING, value=STRING),
                closed(name=STRING, value={"type": "number"}),
            ]
        },
    }
    t = parse_spec(compile_all(make_set({"t": closed(extensions=extensions)}))).table("t")
    c = t.column("extensions")
    assert c.type == (
        'STRUCT("name" VARCHAR, value_string VARCHAR, value_boolean BOOLEAN, value_number DOUBLE, '
        "value_string_array VARCHAR[], value_object_array STRUCT(g VARCHAR)[])[]"
    )
    assert c.derivation.kind == "extension-slot"
    assert t.extension_slots == {"extensions": "json-type"}


def test_nested_extensions_mark_the_column_for_json_type_slots():
    extensions = {"type": "array", "items": closed(name=STRING, value=STRING)}
    t = parse_spec(compile_all(make_set({"t": closed(x=closed(extensions=extensions))}))).table("t")
    assert t.column("x").derivation.kind == "typed-copy" and t.extension_slots == {"x": "json-type"}


def test_a_number_position_is_a_double_and_integer_narrows_it():
    schema_set = make_set({"t": closed(x={"type": "number"}, y={"type": "number"})})
    doc = all_of(schema_set)
    doc["sections"]["t"] = {"x": None, "y": "integer"}
    t = parse_spec(compile_sections(schema_set, doc)).table("t")
    assert (t.column("x").type, t.column("y").type) == ("DOUBLE", "BIGINT")


def test_json_keeps_a_map_or_a_pointer_or_object_position_as_text():
    record = closed(m={"type": "object", "additionalProperties": STRING}, p={"oneOf": [IRI, closed(a=STRING)]})
    schema_set = make_set({"t": record})
    doc = all_of(schema_set)
    doc["sections"]["t"] = {"m": "json", "p": "json"}
    t = parse_spec(compile_sections(schema_set, doc)).table("t")
    assert {c.derivation.kind for c in t.columns[1:]} == {"json-text"}
    assert {c.type for c in t.columns[1:]} == {"VARCHAR"}
    doc["sections"]["t"] = {"m": {"a": "string"}, "p": "json"}
    with pytest.raises(CompileError, match="makes this a map whose keys are data"):
        compile_sections(schema_set, doc)


# --- the root ----------------------------------------------------------------------------------------------------


def test_the_schema_says_which_root_properties_are_collections():
    bundle = {
        "$id": ROOT,
        "$defs": {"M": {"properties": {"a": STRING}}, "C": {"patternProperties": {".": closed(x=STRING)}}},
        "properties": {
            "version": {"const": "1"},
            "by_pattern": {"patternProperties": {".": closed(x=STRING)}},
            "by_ref": {"$ref": "#/$defs/C"},
            "by_additional": {"type": "object", "additionalProperties": closed(x=STRING)},
            "metadata": {"$ref": "#/$defs/M"},
            "listed": {"type": "array", "items": STRING},
            "scalar": STRING,
            "silent": {"type": "object"},
        },
    }
    schema_set = SchemaSet(ROOT, {ROOT: SchemaFile(ROOT, ROOT, b"{}", bundle)})
    parts = root_parts(schema_set, "version")
    assert parts.collections == ("by_pattern", "by_ref", "by_additional") and parts.other_values
    assert root_parts(schema_set, "version", ["silent"]).collections[-1] == "silent"
    with pytest.raises(CompileError, match="says it is not a collection"):
        root_parts(schema_set, "version", ["listed"])


def test_a_root_closed_to_other_properties_holding_only_collections_has_no_other_values():
    schema_set = make_set({"t": closed(x=STRING)}, additionalProperties=False)
    assert root_parts(schema_set).other_values is False
    assert root_parts(make_set({"t": closed(x=STRING)})).other_values is True


def test_the_format_docs_small_example_compiles():
    text = (Path(__file__).resolve().parents[1] / "docs" / "column-set-format.md").read_text()
    doc = yaml.safe_load(re.search(r"```yaml\n(.*?)```", text, re.S).group(1))
    extensions = {"type": "array", "items": {"$ref": f"{W3}/Extension"}}
    open_value = {"type": ["number", "string", "boolean", "object", "array", "null"]}
    coding = closed(code=STRING, system=STRING, extra=STRING)
    mapping = closed(coding=coding, relation=STRING)
    gene = closed(id=STRING, type=STRING, name=STRING, mappings={"type": "array", "items": mapping}, other=STRING)
    variant = closed(
        id=STRING, type=STRING, name=STRING, gene=IRI, start=INT_OR_RANGE, end=INT_OR_RANGE, extensions=extensions
    )
    schema_set = make_set({"gene": gene, "variant": variant}, {"Extension": closed(name=STRING, value=open_value)})
    bundle_id = doc["schema"]
    bundle = {**schema_set.bundle, "$id": bundle_id}
    files = {k: v for k, v in schema_set.files.items() if k != ROOT}
    files[bundle_id] = SchemaFile(bundle_id, bundle_id, b"{}", bundle)
    t = checked(compile_sections(SchemaSet(bundle_id, files), doc)).table("variant")
    assert [c.name for c in t.columns] == [
        "id",
        "id_present",
        "type",
        "name",
        "gene",
        "start",
        "start_min",
        "start_max",
        "stop_pos",
        "stop_pos_min",
        "stop_pos_max",
        "extensions",
    ]
    assert doc["projection"] == "example-narrow"
    assert t.column("extensions").type == 'STRUCT("name" VARCHAR, value_string VARCHAR, value_boolean BOOLEAN)[]'
