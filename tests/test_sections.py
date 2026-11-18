"""The sections spec (ADR 0003, ADR 0006), compiled from the ClinVar-GKM 1.1.0 pairing, and the engine features it
brings: extension slots by JSON type (`json-type`), the integer-or-Range kinds (range-exact, range-min, range-max)
and the record-id check of known_fields tables.

Expectations come from the fixture records parsed here with stdlib json, checked by the spec's rules as written in
spec.py (not by the code under test). Failures take one real record of the fixture and change it
minimally, as each test says; nothing here is a hand-written record.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import re

import duckdb
import pytest
from _fixture import SECTIONS, SPEC, SPEC_SOURCE
from _tables_helpers import connect, read_fixture, roundtrip_check, rows

from gkm_bundle_parquet import build, sqltemplate
from gkm_bundle_parquet.errors import RoundTripError, SpecError
from gkm_bundle_parquet.spec import RANGE_KINDS, check_compiled, parse_spec
from gkm_bundle_parquet.sqltemplate import canonical_type, render_generic

SOURCE = SPEC_SOURCE
BASE = read_fixture()
POINTER = re.compile(r"^#/[^/]+/.")


def _records(section: str, sections=None) -> dict[str, dict]:
    return {k: json.loads(r) for k, r in (sections or BASE)[section]}


def _table(con, table, fast=True) -> list[dict]:
    name = f"out_{table.name}_{'fast' if fast else 'slow'}"
    con.execute(
        f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM ({render_generic(table, fast=fast)}) ORDER BY id'
    )
    return rows(con, name)


# --- the spec --------------------------------------------------------------------------------------------------


def test_the_spec_is_compiled():
    assert (SOURCE.name, SOURCE.version, SOURCE.builtin, SOURCE.path) == ("sections", "2.0", True, None)
    assert SOURCE.schema_set_sha256 and SOURCE.column_set_sha256 and SPEC.root is not None


def _property_columns(t) -> list:
    """The columns that read a property: all but the key and `id_present`."""
    return [c for c in t.columns if c.derivation.kind not in ("key", "id-present")]


def test_spec_shape():
    """One table per section, named as it; `id` (the key) first, then `id_present` when the class doesn't require
    an id; no record column and no ordinal (ADR 0008 decision 1.1); known_fields; every other column reads one
    top-level property; the generic renderer builds every table."""
    assert [t.name for t in SPEC.tables] == list(SECTIONS)
    for t in SPEC.tables:
        assert t.sections == (t.name,) and t.row_filter is None and t.sort_key == "id" and t.known_fields
        assert (t.columns[0].name, t.columns[0].type, t.columns[0].derivation.kind) == ("id", "VARCHAR", "key")
        assert not t.columns_of("record")
        assert not t.columns_of("ordinal")
        for c in _property_columns(t):
            assert re.fullmatch(r"\$\.\w+", c.derivation.path), (t.name, c.name)
            assert c.type == canonical_type(c.type), (t.name, c.name)  # as DESCRIBE prints it
            if c.derivation.kind == "pointer-section":  # a multi-target pointer's section, named after it
                assert c.name == c.derivation.path[2:] + "_section", (t.name, c.name)
            elif c.derivation.kind in RANGE_KINDS:
                prop = c.derivation.path[2:]
                base = {"start": "loc_start", "end": "loc_end"}.get(prop, prop) if t.name == "location" else prop
                suffix = {"range-exact": "", "range-min": "_min", "range-max": "_max"}[c.derivation.kind]
                assert c.name == base + suffix, (t.name, c.name)
            else:
                assert c.name == c.derivation.path[2:], (t.name, c.name)  # named exactly as the property
        assert not sqltemplate.generic_problems(t)


@pytest.mark.parametrize("name", [t.name for t in SPEC.tables])
def test_fast_writes_the_same_parquet_bytes(name, tmp_path):
    """render_generic(fast=True) against the slow reference: the same bytes (json-type columns take the slow
    expression on the record's one parse)."""
    t = SPEC.table(name)
    con = connect(BASE)
    digests = []
    for fast in (True, False):
        path = tmp_path / f"{fast}.parquet"
        con.execute(
            f"COPY (SELECT * FROM ({render_generic(t, fast=fast)}) ORDER BY id) TO '{path}' "
            f"(FORMAT parquet, COMPRESSION {SPEC.writer['compression']}, "
            f"ROW_GROUP_SIZE {SPEC.writer['row_group_size']})"
        )
        digests.append(hashlib.sha256(path.read_bytes()).hexdigest())
    assert digests[0] == digests[1]


def test_build_tables_self_checks_pass(tmp_path):
    """build.build_tables writes every table and its self-checks pass (DESCRIBE equals the spec, the keys equal the
    section's keys: with no record column, the keys alone)."""
    con = connect(BASE)
    kv = build.kv_metadata(
        table_spec_version="1.0",
        bundle_schema_version="1.1.0",
        source_bundle_sha256="0" * 64,
        table_spec={"table_spec": "sections", "table_spec_version": "1.0"},
    )
    results = build.build_tables(con, SPEC, [t.name for t in SPEC.tables], tmp_path, kv)
    assert {r.table: r.rows for r in results} == {s: len(BASE[s]) for s in SECTIONS}
    for t in SPEC.tables:
        cols = [c for c, _ in build.describe_file(con, tmp_path / t.file_name)]
        assert cols[0] == "id" and "data" not in cols


# --- every value, by the spec's rules --------------------------------------------------------------------------


def _kind(v) -> str:
    """The JSON type of an extension value, as json-type slots name it (spec.py)."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, dict):
        return "object"
    kinds = {_kind(e) for e in v}
    return "array" if not kinds else f"{kinds.pop()}_array" if len(kinds) == 1 else "mixed_array"


class _Checker:
    """Checks a column value against the bundle value by the spec's rules, and records which slots it saw."""

    def __init__(self):
        self.slots: set[tuple[str, int]] = set()  # (slot kind, extension depth)

    def check(self, v, got, t, where: str, depth: int = 0) -> None:
        if t.id == "list":
            if v is None:
                assert got is None, where  # an absent list inside a value is NULL (ADR 0005)
                return
            assert isinstance(v, list) and len(got) == len(v), where
            for i, (e, g) in enumerate(zip(v, got, strict=True)):
                self.check(e, g, t.children[0][1], f"{where}[{i}]", depth)
        elif t.id == "struct" and isinstance(v, str):
            # A list element with derived parts (ADR 0007 decision 1.10): a pointer or an IRI is its bare id and
            # section; any other string stays as written.
            fields = dict(t.children)
            assert set(fields) == {"id", "section"}, where
            m = POINTER.match(v)
            assert (got["id"], got["section"]) == ((v.split("/", 2)[2], v.split("/")[1]) if m else (v, None)), (
                where
            )
        elif t.id == "struct":
            if v is None:
                assert got is None, where
                return
            fields = dict(t.children)
            slots = [f for f in fields if f.startswith("value_")]
            assert isinstance(v, dict), where
            if "name" in fields and slots:  # an extension: the one slot of its value's JSON type
                kind = _kind(v.get("value"))
                assert set(v) <= (set(fields) - set(slots)) | {"value"}, where
                assert f"value_{kind}" in slots or (kind == "array" and any(s.endswith("_array") for s in slots))
                self.slots.add((kind, depth + 1))
                for f, ft in fields.items():
                    if f == f"value_{kind}":
                        self.check(v["value"], got[f], ft, f"{where}.{f}", depth + 1)
                    elif f in slots:
                        assert got[f] is None, f"{where}.{f}"  # every other slot, list slots too
                    else:
                        self.check(v.get(f), got[f], ft, f"{where}.{f}", depth + 1)
            else:
                assert set(v) <= set(fields), (where, sorted(set(v) - set(fields)))  # no field dropped
                for f, ft in fields.items():
                    self.check(v.get(f), got[f], ft, f"{where}.{f}", depth)
        elif v is None:
            assert got is None, where
        elif isinstance(v, str) and POINTER.match(v):
            assert got == v.split("/", 2)[2], where  # every pointer is stripped to the bare id
        elif t.id == "date":
            assert got.isoformat() == v, where
        elif t.id == "double":
            assert got == float(v), where
        else:
            assert type(got) is type(v) and got == v, where


def test_every_column_holds_the_bundle_value():
    """Each row is a record: `id` is its key, each property column holds the property as its kind says, every
    pointer is stripped, and each extension fills only the slot of its value's JSON type, at every depth."""
    con = connect(BASE)
    checker = _Checker()
    for t in SPEC.tables:
        records = _records(t.name)
        got = _table(con, t)
        assert [r["id"] for r in got] == sorted(records)
        covered = {"id"} | {c.derivation.path[2:] for c in _property_columns(t)}
        for row in got:
            rec = records[row["id"]]
            assert set(rec) <= covered and rec.get("id", row["id"]) == row["id"]
            assert row.get("id_present", "id" in rec) == ("id" in rec)
            for c in _property_columns(t):
                kind, prop, where = c.derivation.kind, c.derivation.path[2:], f"{t.name} {row['id']} {c.name}"
                v, value = rec.get(prop), row[c.name]
                if kind in RANGE_KINDS:
                    continue  # test_range_columns_equal_clinvar_gkm
                if v is None:
                    assert value == ([] if c.nulls == "empty-list" else None), where
                elif kind == "pointer-section":
                    want = [p.split("/")[1] for p in v] if isinstance(v, list) else v.split("/")[1]
                    assert value == want, where
                elif kind == "bare-id":
                    want = [p.split("/", 2)[2] for p in v] if isinstance(v, list) else v.split("/", 2)[2]
                    assert value == want, where
                else:
                    checker.check(v, value, duckdb.sqltype(c.type), where)
    # the excerpt has every kind its extensions use, at the top level and nested
    seen = {k for k, _ in checker.slots}
    assert {"string", "boolean", "object", "string_array", "object_array"} <= seen
    assert max(d for _, d in checker.slots) >= 2  # an extension inside an extension's value


def test_json_type_slots_on_known_records():
    """A few values read directly: a top-level string extension, a string and a string-array extension inside a
    struct, booleans inside a list of structs, and extensions inside an extension's object-array value."""
    con = connect(BASE)
    seq = {r["id"]: r for r in _table(con, SPEC.table("sequenceReference"))}
    for k, rec in _records("sequenceReference").items():
        assert [(e["name"], e["value_string"]) for e in seq[k]["extensions"]] == [
            (e["name"], e["value"]) for e in rec["extensions"]
        ]
    props = {r["id"]: r for r in _table(con, SPEC.table("propositionPathogenicity"))}
    for k, rec in _records("propositionPathogenicity").items():
        want = rec.get("geneContextQualifier", {}).get("extensions", [])
        for e, g in zip(want, (props[k]["geneContextQualifier"] or {}).get("extensions", []), strict=True):
            if isinstance(e["value"], list):
                assert (g["value_string"], g["value_string_array"]) == (None, e["value"])
            else:
                assert (g["value_string"], g["value_string_array"]) == (e["value"], None)
    variation = {r["id"]: r for r in _table(con, SPEC.table("variation"))}
    nested = 0
    for k, rec in _records("variation").items():
        for e, g in zip(rec["extensions"], variation[k]["extensions"], strict=True):
            if not isinstance(e["value"], list):
                continue
            for item, gi in zip(e["value"], g["value_object_array"], strict=True):
                want = item.get("nucleotideExpression", {}).get("extensions", [])
                for x, gx in zip(want, (gi["nucleotideExpression"] or {}).get("extensions") or [], strict=True):
                    slot = "value_boolean" if isinstance(x["value"], bool) else "value_string"
                    other = "value_string" if slot == "value_boolean" else "value_boolean"
                    assert (gx["name"], gx[slot], gx[other]) == (x["name"], x["value"], None)
                    nested += 1
    assert nested


# --- integer-or-Range columns ----------------------------------------------------------------------------------

RANGE_COLUMNS = ["loc_start", "loc_start_min", "loc_start_max", "loc_end", "loc_end_min", "loc_end_max"]


def _range(value, kind):
    """spec.py's rule for an integer-or-Range: the integer (NULL for a Range), else the Range's ends."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return {"range-exact": None, "range-min": value[0], "range-max": value[1]}[kind]


def test_range_columns_hold_the_bundle_values():
    """location.start and .end: each an integer or a Range [min, max] in the bundle; the three columns of each, as
    spec.py states them, computed here from the records."""
    con = connect(BASE)
    cols = ", ".join(RANGE_COLUMNS)
    con.execute(f"CREATE TABLE s AS SELECT * FROM ({render_generic(SPEC.table('location'))})")
    got = con.execute(f"SELECT id, {cols} FROM s ORDER BY id").fetchall()
    kinds = ("range-exact", "range-min", "range-max")
    want = sorted(
        (k, *(_range(r.get(p), kind) for p in ("start", "end") for kind in kinds))
        for k, r in _records("location").items()
    )
    assert got == want and len(got) == len(BASE["location"]) > 0
    ranges = sum(
        isinstance(r.get("start"), list) or isinstance(r.get("end"), list) for r in _records("location").values()
    )
    assert con.execute("SELECT count(*) FROM s WHERE loc_start IS NULL OR loc_end IS NULL").fetchone()[0] == ranges
    assert ranges == 7  # with open ends on both sides ([null, n] starts, [n, null] ends)
    open_start = "SELECT count(*) FROM s WHERE loc_start_min IS NULL AND loc_start_max IS NOT NULL"
    assert con.execute(open_start).fetchone()[0]
    # copyNumberCount.copies: the producer writes an integer only, so the spec has no range columns for it.
    con.execute(f"CREATE TABLE sc AS SELECT * FROM ({render_generic(SPEC.table('copyNumberCount'))})")
    got = con.execute("SELECT id, copies FROM sc ORDER BY id").fetchall()
    assert got == sorted((k, r["copies"]) for k, r in _records("copyNumberCount").items())


# --- fail loud -------------------------------------------------------------------------------------------------


def _mutated(section, pick, change, sections=None):
    """A copy of the fixture with the first record of `section` for which pick(key, rec) holds changed in place by
    change(rec). Returns (sections, key)."""
    sections = copy.deepcopy(sections or BASE)
    for i, (k, text) in enumerate(sections[section]):
        rec = json.loads(text)
        if pick(k, rec):
            change(rec)
            sections[section][i] = (k, json.dumps(rec, separators=(",", ":")))
            return sections, k
    raise AssertionError(f"no {section} record to mutate")


def _fails(sections, table, *needles, render=render_generic):
    """The table stops with a message holding every needle; the slow reference stops with the same message."""
    msgs = []
    for fast in (True, False):
        con = connect(sections)
        with pytest.raises(duckdb.InvalidInputException) as exc:
            con.execute(f"SELECT * FROM ({render(table, fast=fast)}) ORDER BY 1").fetchall()
        msgs.append(str(exc.value))
    for n in needles:
        assert n in msgs[0], msgs[0]
    assert msgs[1] == msgs[0]
    return msgs[0]


def _with_type(table_name: str, column: str, type_: str):
    """The sections table with one column's type replaced (so a test doesn't depend on the spec's slot set)."""
    t = SPEC.table(table_name)
    cols = tuple(dataclasses.replace(c, type=type_) if c.name == column else c for c in t.columns)
    return dataclasses.replace(t, columns=cols)


SEQ_EXT = 'STRUCT("name" VARCHAR, value_string VARCHAR)[]'


def _set_first_extension_value(value):
    return lambda rec: rec["extensions"][0].__setitem__("value", value)


@pytest.mark.parametrize(
    "value, needle",
    [
        (7, "no slot for an extension value of JSON type integer at [] (the type has value_string)"),
        (1.5, "no slot for an extension value of JSON type number at []"),
        (True, "no slot for an extension value of JSON type boolean at []"),
        ({"a": "b"}, "no slot for an extension value of JSON type object at []"),
        (["a", "b"], "no slot for an extension value of JSON type string_array at []"),
        ([], "no slot for an extension value of JSON type array at []"),
        (None, "extension value at [] is null or absent"),
        (["a", 1], 'extension value at [] is an array of mixed JSON types: ["a",1]'),
    ],
)
def test_json_type_value_with_no_slot_stops_the_build(value, needle):
    sections, key = _mutated(
        "sequenceReference", lambda k, r: r.get("extensions"), _set_first_extension_value(value)
    )
    table = _with_type("sequenceReference", "extensions", SEQ_EXT)
    _fails(sections, table, f"table sequenceReference: column extensions: key {key}: {needle}")


def test_json_type_absent_value_stops_the_build():
    sections, key = _mutated(
        "sequenceReference", lambda k, r: r.get("extensions"), lambda rec: rec["extensions"][0].pop("value")
    )
    _fails(sections, _with_type("sequenceReference", "extensions", SEQ_EXT), "is null or absent", key)


def test_json_type_empty_array_fills_no_slot():
    """An empty array has no element type: where the struct has an array slot it fills none (all NULL), and the
    round trip writes it back as []."""
    sections, key = _mutated("sequenceReference", lambda k, r: r.get("extensions"), _set_first_extension_value([]))
    t = _with_type(
        "sequenceReference",
        "extensions",
        'STRUCT("name" VARCHAR, value_string VARCHAR, value_string_array VARCHAR[])[]',
    )
    got = {r["id"]: r for r in _table(connect(sections), t)}[key]["extensions"][0]
    assert (got["value_string"], got["value_string_array"]) == (None, None)
    sections, key = _mutated(
        "sequenceReference", lambda k, r: r.get("extensions"), _set_first_extension_value(["x"])
    )
    got = {r["id"]: r for r in _table(connect(sections), t)}[key]["extensions"][0]
    assert (got["value_string"], got["value_string_array"]) == (None, ["x"])


def _gene_symbols(rec):
    ext = rec.get("geneContextQualifier", {}).get("extensions", [])
    return next((e for e in ext if isinstance(e.get("value"), list)), None)


def test_json_type_mixed_array_nested_stops_the_build():
    """Inside a struct column (geneContextQualifier.extensions[]): an array of a string and an integer."""
    for section in ("propositionPathogenicity", "propositionOncogenicity", "propositionSomaticClinicalImpact"):
        if any(_gene_symbols(r) for r in _records(section).values()):
            break
    else:
        pytest.fail("no proposition with a list-valued gene-context extension")
    sections, key = _mutated(
        section, lambda k, r: _gene_symbols(r), lambda rec: _gene_symbols(rec)["value"].append(1)
    )
    _fails(
        sections,
        SPEC.table(section),
        f"table {section}: column geneContextQualifier: key {key}",
        "extension value at .extensions[] is an array of mixed JSON types",
    )


def test_json_type_no_slot_nested_in_a_list_stops_the_build():
    """scv.reportedIn[].extensions[]: a number where the struct has no value_number slot."""

    def pick(k, rec):
        return any(d.get("extensions") for d in rec.get("reportedIn", []))

    def change(rec):
        next(d for d in rec["reportedIn"] if d.get("extensions"))["extensions"][0]["value"] = 0.5

    sections, key = _mutated("scv", pick, change)
    t = SPEC.table("scv")
    assert "value_number" not in t.column("reportedIn").type
    _fails(sections, t, f"table scv: column reportedIn: key {key}", "JSON type number at [].extensions[]")


def test_record_id_not_the_key_stops_the_build():
    """known_fields tables: the record's own id must be its key."""
    sections, key = _mutated("variation", lambda k, r: "id" in r, lambda rec: rec.__setitem__("id", "clinvar:0"))
    needle = f'table variation: record: key {key}: the record\'s id "clinvar:0" is not its bundle key'
    _fails(sections, SPEC.table("variation"), needle)


def test_record_id_absent(tmp_path):
    """ADR 0007: a class that allows an `id` but doesn't require it has `id_present`, so a record without its id
    is written back without it."""
    sections, key = _mutated("variation", lambda k, r: "id" in r, lambda rec: rec.pop("id"))
    got = {r["id"]: r for r in _table(connect(sections), SPEC.table("variation"))}
    assert got[key]["id_present"] is False and all(r["id_present"] for k, r in got.items() if k != key)
    roundtrip_check(connect(sections), SPEC.table("variation"), tmp_path)


def test_unknown_top_level_property_stops_the_build():
    sections, key = _mutated("allele", lambda k, r: True, lambda rec: rec.__setitem__("newProperty", 1))
    _fails(sections, SPEC.table("allele"), f"table allele: record: key {key}", "no column covers: [newProperty]")


@pytest.mark.parametrize(
    "value",
    [[1, 2, 3], [1], "12", 1.5, [1.5, 2], [[1], 2], {"min": 1}, True],
    ids=["three", "one", "string", "number", "number end", "nested", "object", "boolean"],
)
def test_range_of_another_shape_stops_the_build(value):
    sections, key = _mutated("location", lambda k, r: "start" in r, lambda rec: rec.__setitem__("start", value))
    msg = _fails(sections, SPEC.table("location"), "table location: column loc_start", f"key {key}")
    assert "expected an integer or a Range [min, max] of integers or nulls, got " in msg


def test_range_null_ends_and_absent(tmp_path):
    """An absent coordinate is NULL in all three columns. [null, null], a Range of two open ends, reads the same
    (all NULL), so it can't be written back: the round-trip check stops the build (the producer writes none)."""
    sections, key = _mutated("location", lambda k, r: "start" in r, lambda rec: rec.pop("start"))
    got = {r["id"]: r for r in _table(connect(sections), SPEC.table("location"))}[key]
    assert (got["loc_start"], got["loc_start_min"], got["loc_start_max"]) == (None, None, None)
    assert got["loc_end"] is not None or got["loc_end_min"] is not None
    sections, key = _mutated(
        "location", lambda k, r: "start" in r, lambda rec: rec.__setitem__("start", [None, None])
    )
    with pytest.raises(RoundTripError, match=f"table location: round trip: key {key}: .* differs from the bundle"):
        roundtrip_check(connect(sections), SPEC.table("location"), tmp_path)


# --- fail loud: when the spec loads ----------------------------------------------------------------------------


def _one_table(**col) -> dict:
    """A one-table spec mapping over section location (id, then the given column)."""
    base = {"name": "x", "nulls": "when-absent", "description": "test"}
    return {
        "name": "t",
        "spec_version": "0.1",
        "status": "draft",
        "bundle_schema_versions": ["1.1.0"],
        "sources": [],
        "writer": {"compression": "snappy", "row_group_size": 10},
        "tables": [
            {
                "name": "loc",
                "sections": ["location"],
                "row_filter": None,
                "sort_key": "id",
                "description": "test",
                "columns": [
                    {
                        "name": "id",
                        "type": "VARCHAR",
                        "nulls": "never",
                        "derivation": {"kind": "key", "path": None, "transform": None},
                        "description": "test",
                    },
                    {**base, **col},
                ],
            }
        ],
    }


def test_spec_load_failures():
    rng = {"kind": "range-min", "path": "$.start", "transform": None}
    assert not sqltemplate.generic_problems(parse_spec(_one_table(type="BIGINT", derivation=rng)).tables[0])
    t = parse_spec(_one_table(type="VARCHAR", derivation=rng)).tables[0]
    assert "must be BIGINT" in " ".join(sqltemplate.generic_problems(t))
    with pytest.raises(SpecError, match="fields only"):
        parse_spec(_one_table(type="BIGINT", derivation={**rng, "path": "$.start[*]"}))
    raw = _one_table(
        type="STRUCT(a VARCHAR)[]", derivation={"kind": "typed-copy", "path": "$.x", "transform": None}
    )
    raw["tables"][0]["extension_slots"] = {"x": "by-type"}
    with pytest.raises(SpecError, match="non-empty mapping or json-type"):
        parse_spec(raw)
    raw["tables"][0]["extension_slots"] = {"x": "json-type"}
    t = parse_spec(raw).tables[0]
    assert "has no extension struct" in " ".join(sqltemplate.generic_problems(t))


def test_at_most_one_record_column():
    raw = _one_table(type="VARCHAR", derivation={"kind": "record", "path": None, "transform": None})
    assert parse_spec(raw).tables[0].columns_of("record")
    raw["tables"][0]["columns"].append({**raw["tables"][0]["columns"][1], "name": "y"})
    raw["name"] = "t"
    raw["root"] = {"version_property": None, "other_values": False}
    with pytest.raises(SpecError, match="2 record columns"):
        check_compiled(parse_spec(raw), "test")
