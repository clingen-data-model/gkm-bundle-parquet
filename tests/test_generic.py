"""The generic renderer (sqltemplate.render_generic): a table's SELECT built from its spec's derivation kinds
alone (ADR 0003, spec.py's path rules).

A hand-written typed spec (tests/fixtures/custom-spec/tables.yaml, test data: the tool reads no tables.yaml since
2.0.0) over the real excerpt's records: its tables checked value by value against the records parsed here with
stdlib json, the fast SQL against the slow reference, and the fail-loud cases, when the spec loads and when the
build reads a record. The sections spec's own tables are tested in tests/test_sections.py.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import duckdb
import pytest
import yaml
from _tables_helpers import connect, read_fixture, rows

from clinvar_gkm_parquet import sqltemplate
from clinvar_gkm_parquet.errors import SpecError
from clinvar_gkm_parquet.spec import Spec, check_compiled, parse_spec

CUSTOM = Path(__file__).parent / "fixtures" / "custom-spec" / "tables.yaml"


def _load(raw: dict) -> Spec:
    """Parse and check a spec mapping, as a compiled spec is checked."""
    spec = parse_spec(raw, where="tables.yaml", require_name=True)
    check_compiled(spec, "tables.yaml")
    return spec


# --- the custom spec: expectations from the records, parsed with stdlib json ----------------------------------


def _records(section: str) -> dict[str, dict]:
    return {k: json.loads(r) for k, r in read_fixture()[section]}


def _bare(p: str | None) -> str | None:
    assert p is None or re.match(r"^#/[^/]+/.", p), p
    return None if p is None else p.split("/", 2)[2]


def _section_of(p: str | None) -> str | None:
    return None if p is None else p.split("/", 2)[1]


def _want_variation(rec: dict) -> dict:
    return {
        "name": rec.get("name"),
        "members": [_bare(m) for m in rec.get("members", [])],
        "mappings": [
            {
                "coding": {
                    "system": m["coding"].get("system"),
                    "code": m["coding"].get("code"),
                    "iris": m["coding"].get("iris", []),
                },
                "relation": m.get("relation"),
            }
            for m in rec.get("mappings", [])
        ],
        "mapping_codes": [m.get("coding", {}).get("code") for m in rec.get("mappings", [])],
    }


def _want_scv(rec: dict) -> dict:
    c = rec.get("classification")
    classification = None
    if c is not None:
        assert all(e["name"] == "description" for e in c.get("extensions", []))
        classification = {
            "type": c.get("type"),
            "conceptType": c.get("conceptType"),
            "name": c.get("name"),
            "primaryCoding": None
            if "primaryCoding" not in c
            else {"code": c["primaryCoding"].get("code"), "system": c["primaryCoding"].get("system")},
            "extensions": [{"name": e["name"], "value_string": e["value"]} for e in c.get("extensions", [])],
        }
    contributions = rec.get("contributions", [])
    return {
        "classification": classification,
        "proposition_id": _bare(rec.get("proposition")),
        "proposition_section": _section_of(rec.get("proposition")),
        "contributor_ids": [_bare(x.get("contributor")) for x in contributions],
        "contribution_dates": [x.get("date") for x in contributions],
        "evidence_line_sections": [_section_of(p) for p in rec.get("hasEvidenceLines", [])],
    }


def _dates_as_text(rows: dict[str, dict]) -> dict[str, dict]:
    for r in rows.values():
        r["contribution_dates"] = [None if d is None else d.isoformat() for d in r["contribution_dates"]]
    return rows


def _built(table: str, columns: list[str]) -> dict[str, dict]:
    """id -> the columns, of the table as the generic renderer builds it from the excerpt's records."""
    con = connect()
    con.execute(f"CREATE TABLE t AS SELECT * FROM ({sqltemplate.render_generic(_load(_raw()).table(table))})")
    return {r["id"]: {c: r[c] for c in columns} for r in rows(con, "t")}


def test_custom_spec_values():
    variations, scvs = _records("variation"), _records("scv")
    got = _built("variation_summary", ["name", "members", "mappings", "mapping_codes"])
    assert got == {k: _want_variation(r) for k, r in variations.items()}
    assert any(r["members"] for r in got.values())
    cols = list(_want_scv(next(iter(scvs.values()))))
    got = _dates_as_text(_built("scv_summary", cols))
    assert got == {k: _want_scv(r) for k, r in scvs.items()}
    assert any(r["evidence_line_sections"] for r in got.values())  # the excerpt has one such SCV
    # data is the record, byte for byte (JSON-typed in variation_summary)
    for table, section in (("variation_summary", "variation"), ("scv_summary", "scv")):
        con = connect()
        sql = sqltemplate.render_generic(_load(_raw()).table(table))
        data = con.execute(f"SELECT id, CAST(data AS VARCHAR) FROM ({sql})").fetchall()
        assert sorted(data) == sorted(read_fixture()[section])


def test_custom_spec_fast_equals_slow():
    spec = _load(_raw())
    con = connect()
    for t in spec.tables:
        fast = con.execute(f"SELECT * FROM ({sqltemplate.render_generic(t)}) ORDER BY id").fetchall()
        slow = con.execute(f"SELECT * FROM ({sqltemplate.render_generic(t, fast=False)}) ORDER BY id").fetchall()
        assert fast == slow and len(fast) == len(read_fixture()[t.sections[0]])


# --- fail loud: when the spec loads ----------------------------------------------------------------------------


def _raw() -> dict:
    return yaml.safe_load(CUSTOM.read_text(encoding="utf-8"))


def _col(raw: dict, table: str, column: str) -> dict:
    t = next(t for t in raw["tables"] if t["name"] == table)
    return next(c for c in t["columns"] if c["name"] == column)


def _table(raw: dict, table: str) -> dict:
    return next(t for t in raw["tables"] if t["name"] == table)


def _set(table, column, **changes):
    def mutate(raw):
        c = _col(raw, table, column)
        for k, v in changes.items():
            if k in ("kind", "path", "pointers", "transform"):
                c["derivation"][k] = v
            else:
                c[k] = v
        return raw

    return mutate


def _add_column(table, column):
    def mutate(raw):
        t = _table(raw, table)
        t["columns"].insert(1, column)
        return raw

    return mutate


def _table_set(table, **changes):
    def mutate(raw):
        _table(raw, table).update(changes)
        return raw

    return mutate


def _unknown_derivation_key(raw):
    _col(raw, "scv_summary", "proposition_id")["derivation"]["via"] = "x"
    return raw


def _slots(raw):
    raw["tables"][1]["extension_slots"]["classification"] = {"description": "value_nope"}
    return raw


def _column(name, type_, kind, path, nulls="when-absent", transform=None) -> dict:
    return {
        "name": name,
        "type": type_,
        "nulls": nulls,
        "derivation": {"kind": kind, "path": path, "transform": transform},
        "description": "test",
    }


LOAD_FAILS = [
    (
        "a [] path",
        _set("scv_summary", "contributor_ids", path="$.contributions[].contributor"),
        "is not $.<field>",
    ),
    ("a path without $", _set("scv_summary", "proposition_id", path="proposition"), "is not $.<field>"),
    ("a path into a key", _set("scv_summary", "proposition_id", path="$.a..b"), "is not $.<field>"),
    ("a list path on a typed copy", _set("variation_summary", "mappings", path="$.mappings[*]"), "fields only"),
    ("a path on a key", _set("scv_summary", "id", path="$.id"), "takes no path"),
    ("too few list levels", _set("scv_summary", "contributor_ids", type="VARCHAR"), "list level(s)"),
    ("too many list levels", _set("scv_summary", "proposition_id", type="VARCHAR[][]"), "list level(s)"),
    ("a bare id that is not text", _set("scv_summary", "proposition_id", type="BIGINT"), "VARCHAR or a list"),
    (
        "a scalar path of a struct",
        _set("variation_summary", "name", type="STRUCT(a VARCHAR)"),
        "a scalar or a list",
    ),
    (
        "a derivation kind the tool doesn't have",
        _add_column("scv_summary", _column("n", "BIGINT", "computed", "$.contributions", transform="count")),
        "derivation kind must be one of",
    ),
    ("a row filter", _table_set("scv_summary", row_filter="only some rows"), "it has a row filter"),
    (
        "a pointer position not in the type",
        _set("variation_summary", "mappings", pointers=["[*].nope"]),
        "ptr position(s) not in the type",
    ),
    (
        "a pointer position of bad syntax",
        _set("variation_summary", "mappings", pointers=["[].x"]),
        "pointer position",
    ),
    ("pointers on a scalar path", _set("variation_summary", "name", pointers=["[*]"]), "pointers is for"),
    (
        "known_fields with a nested path",
        _table_set("variation_summary", known_fields=True),
        "known_fields needs every column to read a top-level field",
    ),
    ("an extension slot not in the type", _slots, "slot(s) not in the column type"),
    ("an unknown derivation key", _unknown_derivation_key, "unknown key"),
    ("a bad type", _set("variation_summary", "mappings", type="STRUCT(oops"), "invalid DuckDB type"),
]


@pytest.mark.parametrize("label, mutate, match", LOAD_FAILS, ids=[c[0] for c in LOAD_FAILS])
def test_custom_spec_fails_when_it_loads(label, mutate, match):
    with pytest.raises(SpecError, match=re.escape(match)):
        _load(mutate(_raw()))


def test_the_example_spec_loads():
    spec = _load(_raw())
    assert [t.name for t in spec.tables] == ["variation_summary", "scv_summary"]
    assert parse_spec(_raw()) == spec


# --- fail loud: when the build reads a record ------------------------------------------------------------------


def _fails(raw: dict, table: str, *needles: str) -> None:
    """Rendering the table and running it on the real records stops with every needle in the message; the slow
    reference stops with the same message."""
    spec = parse_spec(raw)
    t = spec.table(table)
    assert not sqltemplate.generic_problems(t)
    msgs = []
    for fast in (True, False):
        con = connect()
        with pytest.raises(duckdb.InvalidInputException) as exc:
            con.execute(f"SELECT * FROM ({sqltemplate.render_generic(t, fast=fast)}) ORDER BY 1").fetchall()
        msgs.append(str(exc.value))
    for n in needles:
        assert n in msgs[0], msgs[0]
    assert msgs[1] == msgs[0]


def test_unknown_field_under_a_typed_copy_stops_the_build():
    """The spec's classification type lacks primaryCoding, which the records have: the build stops, naming the
    table, column, key and field (no field is dropped)."""
    raw = _raw()
    c = _col(raw, "scv_summary", "classification")
    c["type"] = c["type"].replace('primaryCoding STRUCT(code VARCHAR, "system" VARCHAR), ', "")
    _fails(
        raw,
        "scv_summary",
        "table scv_summary: column classification",
        "field(s) not in the spec type",
        "primaryCoding",
        "table scv_summary: column classification: key clinvar.submission:SCV",
        "field(s) not in the spec type at (column): [primaryCoding]",
    )


def test_non_pointer_at_a_bare_id_path_stops_the_build():
    raw = _add_column("variation_summary", _column("name_id", "VARCHAR", "bare-id", "$.name"))(_raw())
    _fails(raw, "variation_summary", "column name_id", "not a #/<section>/<id> pointer at (column)")


def test_non_pointer_in_a_list_path_stops_the_build():
    raw = _add_column(
        "scv_summary", _column("dates", "VARCHAR[]", "bare-id", "$.contributions[*].date", "empty-list")
    )(_raw())
    _fails(raw, "scv_summary", "column dates", "not a #/<section>/<id> pointer at contributions[].date")


def test_field_step_on_a_non_object_stops_the_build():
    raw = _add_column(
        "variation_summary", _column("x", "VARCHAR[]", "scalar-path", "$.members[*].x", "empty-list")
    )(_raw())
    _fails(raw, "variation_summary", "column x", "expected an object at members[], got ")


def test_list_step_on_a_non_list_stops_the_build():
    raw = _add_column(
        "scv_summary", _column("names", "VARCHAR[]", "scalar-path", "$.classification[*].name", "empty-list")
    )(_raw())
    _fails(raw, "scv_summary", "column names", "expected a list at classification, got ")


def test_object_at_a_scalar_path_stops_the_build():
    raw = _add_column("scv_summary", _column("c", "VARCHAR", "scalar-path", "$.classification"))(_raw())
    _fails(raw, "scv_summary", "column c", "expected a scalar")


def test_known_fields_with_a_list_path():
    """known_fields on a table with a $.<field>[*] path renders; a record field no column reads stops the
    build."""
    raw = _raw()
    raw["tables"] = [
        {
            "name": "scv_known",
            "sections": ["scv"],
            "row_filter": None,
            "sort_key": "id",
            "known_fields": True,
            "description": "test",
            "columns": [
                _column("id", "VARCHAR", "key", None, "never"),
                _column("el", "VARCHAR[]", "pointer-section", "$.hasEvidenceLines[*]", "empty-list"),
                _column("data", "VARCHAR", "record", None, "never"),
            ],
        }
    ]
    _fails(raw, "scv_known", "table scv_known: record: key clinvar.submission:SCV", "top-level field(s) no column")
