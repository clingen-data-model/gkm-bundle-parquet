"""The round trip (ADR 0005; record-identical since ADR 0008 decisions 1.2, 1.3): a full sections build encodes
every record of the bundle, and `rebuild` writes back records whose canonical forms (docs/round-trip.md) are the
source's.

The expected records sha256s are computed here from the fixture by an independent reference
(_input_helpers.independent_records_sha256: stdlib json and hashlib, not canonical.py); every refusal and
build-time failure is checked on the real excerpt, changed minimally where a test says so. Nothing here is a
hand-written record.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
from pathlib import Path

import duckdb
import pytest
from _fixture import CLINVAR_SCHEMA, FIXTURE, GOLDEN_SECTIONS_SPEC, PAIRING, SPEC
from _input_helpers import independent_records_sha256, independent_section_sha256
from _tables_helpers import connect, read_fixture, roundtrip_check, write_bundle

from gkm_bundle_parquet.build import EMPTY_SHA256, KV_PREFIX
from gkm_bundle_parquet.cli import main
from gkm_bundle_parquet.compiler import root_parts
from gkm_bundle_parquet.errors import RoundTripError, SpecError
from gkm_bundle_parquet.roundtrip import RoundTripMismatch, lossless_problems, rebuild, record_sql
from gkm_bundle_parquet.schemas import load_schema_set
from gkm_bundle_parquet.spec import parse_spec
from gkm_bundle_parquet.sqltemplate import render_generic

TEXT = gzip.decompress(FIXTURE.read_bytes())
FULL = ["--full", FIXTURE]
DOC = json.loads(TEXT)
SECTION_NAMES = {s for t in SPEC.tables for s in t.sections}
RECORDS_SHA256 = independent_records_sha256(DOC, SECTION_NAMES)


def cli(capsys, *args) -> tuple[int, str, str]:
    rc = main([str(a) for a in args])
    got = capsys.readouterr()
    return rc, got.out, got.err


def kv_of(path: Path) -> dict[str, str]:
    rows = duckdb.connect().execute(f"SELECT key, value FROM parquet_kv_metadata('{path}')").fetchall()
    return {k.decode()[len(KV_PREFIX) :]: v.decode() for k, v in rows if k.decode().startswith(KV_PREFIX)}


def rewrite(path: Path, select: str, rename: dict[str, str] | None = None) -> None:
    """Rewrite a built file with `select` (over `t`), keeping its key-value metadata (keys renamed by `rename`): a
    tampered file."""
    con = duckdb.connect()
    kv = con.execute(f"SELECT key, value FROM parquet_kv_metadata('{path}')").fetchall()
    rename = {(KV_PREFIX + a).encode(): (KV_PREFIX + b).encode() for a, b in (rename or {}).items()}
    kv = [(rename.get(k, k), v) for k, v in kv]
    meta = ", ".join(f"'{k.decode()}': '{v.decode().replace(chr(39), chr(39) * 2)}'" for k, v in kv)
    tmp = path.with_name(path.name + ".new")
    con.execute(f"CREATE VIEW t AS SELECT * FROM read_parquet('{path}')")
    con.execute(f"COPY ({select}) TO '{tmp}' (FORMAT parquet, KV_METADATA {{{meta}}})")
    con.close()
    tmp.replace(path)


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("rt") / "sections"
    assert main(["build", *map(str, FULL), *PAIRING, "--out", str(out)]) == 0
    return out


@pytest.fixture
def copy(built, tmp_path) -> Path:
    d = tmp_path / "copy"
    shutil.copytree(built, d)
    return d


# --- the spec --------------------------------------------------------------------------------------------------


def test_sections_spec_is_lossless():
    assert SPEC.lossless and lossless_problems(SPEC) == []
    for t in SPEC.tables:
        # ADR 0008 decision 1.1: no ordinal column; the key is the first.
        assert t.json is not None and t.columns[0].derivation.kind == "key" and not t.columns_of("ordinal")
        for c in t.columns_of("bare-id"):
            assert c.derivation.targets, (t.name, c.name)
            if len(c.derivation.targets) > 1:  # its section column follows it
                sc = t.columns[t.columns.index(c) + 1]
                assert (sc.name, sc.derivation.kind, sc.derivation.path) == (
                    f"{c.name}_section",
                    "pointer-section",
                    c.derivation.path,
                )
        for c in t.columns:
            assert len(c.derivation.pointer_targets) == len(c.derivation.pointers)


def _raw_sections() -> dict:
    import yaml

    return yaml.safe_load(GOLDEN_SECTIONS_SPEC.read_text())


@pytest.mark.parametrize(
    "change, message",
    [
        (
            lambda r: r["tables"].append({**r["tables"][0], "name": "again"}),
            "the tables must read one section each, each section once (repeated: ['sequenceReference'])",
        ),
        (lambda r: r["tables"][0].pop("json"), "table sequenceReference: needs a json block"),
        (
            lambda r: r["tables"][0]["columns"].insert(0, r["tables"][0]["columns"].pop()),
            "an ordinal column (tool 1.x) is one, the last column",
        ),
        (lambda r: r["tables"][1]["columns"][-2]["derivation"].pop("targets"), "needs its target section(s)"),
        (lambda r: r["tables"][6]["columns"].pop(5), "needs a pointer-section column of the same path"),
        (
            lambda r: r["tables"][6]["columns"][3]["derivation"].update(pointers=["[*].allele", "[*].location"]),
            "pointers need their target sections",
        ),
        (lambda r: r["tables"][6]["extension_slots"].update(extensions={"x": "value_string"}), "json-type"),
        (lambda r: r["tables"][1]["columns"].pop(3), "needs all of range-exact, range-min, range-max"),
    ],
)
def test_lossless_spec_rules(change, message):
    from gkm_bundle_parquet.spec import check_compiled

    raw = _raw_sections()
    change(raw)
    spec = parse_spec(raw, require_name=True)
    with pytest.raises(SpecError, match=re.escape(message)):
        check_compiled(spec, "the test's spec")


def test_record_sql_refuses_a_type_with_no_exact_text():
    raw = _raw_sections()
    raw["tables"][3]["columns"][3]["type"] = "DATE"  # copyNumberCount.copies
    with pytest.raises(SpecError, match="no exact JSON text for type DATE"):
        record_sql(parse_spec(raw).table("copyNumberCount"))


# --- the round trip on the excerpt -----------------------------------------------------------------------------


def test_rebuild_is_record_identical(built, tmp_path, capsys):
    for name in ("bundle.json", "bundle.json.gz"):
        rc, out, _ = cli(capsys, "rebuild", built, "-o", tmp_path / name)
        assert rc == 0 and f"records sha256 {RECORDS_SHA256} (matches the manifest's" in out
    text = (tmp_path / "bundle.json").read_bytes()
    assert gzip.decompress((tmp_path / "bundle.json.gz").read_bytes()) == text
    rebuilt = json.loads(text)
    assert list(rebuilt) == list(DOC) and rebuilt == DOC
    assert independent_records_sha256(rebuilt, SECTION_NAMES) == RECORDS_SHA256
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bundle.json", "bundle.json.gz"]  # no partial files


def test_build_records_the_records_sha256s(built):
    manifest = json.loads((built / "manifest.json").read_text())
    assert manifest["records_sha256"] == RECORDS_SHA256
    assert manifest["round_trip"] == {"promise": "record-identical", "source_layout": "line"}
    assert not {"source_text_sha256", "source_text_size"} & set(manifest)
    for t in SPEC.tables:
        kv = kv_of(built / t.file_name)
        want = independent_section_sha256(DOC.get(t.name, {}))
        assert kv["section_records_sha256"] == want and "section_text_sha256" not in kv, t.name
    assert kv_of(built / "gene.parquet")["section_records_sha256"] != EMPTY_SHA256


def _shuffled(v):
    """The value with every object's keys and every array's elements reversed, at every depth."""
    if isinstance(v, dict):
        return {k: _shuffled(x) for k, x in reversed(list(v.items()))}
    if isinstance(v, list):
        return [_shuffled(x) for x in reversed(v)]
    return v


def _rewritten(edit) -> dict[str, list[tuple[str, str]]]:
    return {
        s: [(k, json.dumps(edit(json.loads(r)), ensure_ascii=False)) for k, r in recs] for s, recs in BASE.items()
    }


def _build_from(sections, path: Path, capsys) -> tuple[Path, dict]:
    src = write_bundle(sections, path.with_suffix(".json.gz"))
    assert cli(capsys, "build", "--full", src, *PAIRING, "--out", path)[0] == 0
    kvs = {t.name: kv_of(path / t.file_name)["section_records_sha256"] for t in SPEC.tables}
    return path, {"records_sha256": json.loads((path / "manifest.json").read_text())["records_sha256"], **kvs}


def test_reordered_keys_and_arrays_build_the_same_records(built, tmp_path, capsys):
    """Another text of the same records (keys and arrays reversed, spaced, non-ASCII written as itself) builds a
    set with the same records sha256s, which rebuilds; a changed value changes its section's and the bundle's."""
    sections = _rewritten(_shuffled)
    assert sections["scv"][0][1] != BASE["scv"][0][1]
    out, got = _build_from(sections, tmp_path / "reordered", capsys)
    assert got["records_sha256"] == RECORDS_SHA256
    assert got == {"records_sha256": RECORDS_SHA256} | {
        t.name: kv_of(built / t.file_name)["section_records_sha256"] for t in SPEC.tables
    }
    rc, stdout, _ = cli(capsys, "rebuild", out, "-o", tmp_path / "r.json")
    assert rc == 0 and f"records sha256 {RECORDS_SHA256} (matches the manifest's" in stdout

    def changed(o):
        o = _shuffled(o)
        if o.get("id") == BASE["submitter"][0][0]:
            o["name"] += "."
        return o

    _, other = _build_from(_rewritten(changed), tmp_path / "changed", capsys)
    assert sorted(k for k in got if other[k] != got[k]) == ["records_sha256", "submitter"]


def test_rows_are_written_in_key_order(built):
    """ADR 0008 decision 1.1: no record order is kept; the rows are in key order."""
    con = duckdb.connect()
    for section, records in read_fixture().items():
        got = con.execute(f"SELECT id FROM read_parquet('{built / (section + '.parquet')}')")
        assert [r[0] for r in got.fetchall()] == sorted(k for k, _ in records), section


def test_verify_roundtrip(built, capsys):
    rc, out, _ = cli(capsys, "verify", built, "--roundtrip", "--full", FIXTURE)
    assert rc == 0 and "round trip: identical" in out


# --- rebuild refuses what it can't reproduce -------------------------------------------------------------------


def test_rebuild_refuses_a_set_of_a_retired_spec(copy, tmp_path):
    """A set 1.x wrote with another table spec (here, as baseline would label it) names no compiled spec."""
    con = duckdb.connect()
    for path in copy.glob("*.parquet"):
        kv = con.execute(f"SELECT key, value FROM parquet_kv_metadata('{path}')").fetchall()
        kv = {k.decode(): v.decode() for k, v in kv}
        for k in ("table_spec_sha256", "schema_set_sha256", "column_set_sha256", "section_records_sha256"):
            kv.pop(KV_PREFIX + k, None)
        kv[KV_PREFIX + "table_spec"] = "baseline"
        meta = ", ".join(f"'{k}': '{v.replace(chr(39), chr(39) * 2)}'" for k, v in kv.items())
        tmp = path.with_name(path.name + ".new")
        con.execute(
            f"COPY (SELECT * FROM read_parquet('{path}')) TO '{tmp}' (FORMAT parquet, KV_METADATA {{{meta}}})"
        )
        tmp.replace(path)
    with pytest.raises(SpecError, match="not a compiled spec; this tool reads only compiled specs since 2.0.0"):
        rebuild(copy, tmp_path / "b.json")


def test_rebuild_refuses_a_table_subset(tmp_path, capsys):
    out = tmp_path / "subset"
    assert cli(capsys, "build", *FULL, *PAIRING, "--tables", "gene,scv", "--out", out)[0] == 0
    with pytest.raises(RoundTripError, match="missing table"):
        rebuild(out, tmp_path / "b.json")


def test_rebuild_refuses_a_selection(tmp_path, capsys):
    out = tmp_path / "sel"
    assert cli(capsys, "build", *FULL, *PAIRING, "--driver", "submitter", "--max-depth", "0", "--out", out)[0] == 0
    assert not any("section_records_sha256" in kv_of(p) for p in out.glob("*.parquet"))
    with pytest.raises(RoundTripError, match="selective build"):
        rebuild(out, tmp_path / "b.json")


def test_rebuild_refuses_a_missing_table(copy, tmp_path):
    (copy / "gene.parquet").unlink()
    with pytest.raises(RoundTripError, match="missing table"):
        rebuild(copy, tmp_path / "b.json")


def test_rebuild_refuses_a_repeated_key(copy, tmp_path):
    rewrite(copy / "gene.parquet", "SELECT * FROM t UNION ALL (SELECT * FROM t LIMIT 1)")
    with pytest.raises(RoundTripError, match="table gene: 61 rows but 60 distinct keys"):
        rebuild(copy, tmp_path / "b.json")
    assert not (tmp_path / "b.json").exists()


def test_rebuild_refuses_a_pointer_outside_its_targets(copy, tmp_path):
    rewrite(copy / "scv.parquet", "SELECT * REPLACE ('evidenceLine' AS proposition_section) FROM t")
    with pytest.raises(RoundTripError, match="names section evidenceLine, not one of"):
        rebuild(copy, tmp_path / "b.json")
    assert not list(tmp_path.glob("*b.json*"))


def test_rebuild_refuses_a_changed_value(copy, tmp_path):
    rewrite(copy / "submitter.parquet", "SELECT * REPLACE (upper(name) AS name) FROM t")
    with pytest.raises(RoundTripMismatch, match="section submitter: the rebuilt records have records sha256"):
        rebuild(copy, tmp_path / "b.json")
    assert not list(tmp_path.glob("*b.json*"))


def test_rebuild_refuses_a_changed_manifest(copy, tmp_path):
    m = json.loads((copy / "manifest.json").read_text())
    m["records_sha256"] = "0" * 64
    (copy / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(RoundTripMismatch, match="the manifest records records_sha256 0000"):
        rebuild(copy, tmp_path / "b.json")


def test_rebuild_refuses_a_missing_manifest(copy, tmp_path):
    (copy / "manifest.json").unlink()
    with pytest.raises(RoundTripError, match="manifest.json: missing"):
        rebuild(copy, tmp_path / "b.json")
    assert not (tmp_path / "b.json").exists()


def test_rebuild_refuses_an_existing_output(built, tmp_path):
    (tmp_path / "b.json").write_text("x")
    with pytest.raises(RoundTripError, match="already exists"):
        rebuild(built, tmp_path / "b.json")


def _as_if_built_by_1x(d: Path) -> None:
    """The set as tool 1.x would have recorded it: section_text_sha256 (a text sha256, here a stand-in) in every
    file, no records sha256 anywhere."""
    for p in d.glob("*.parquet"):
        rewrite(p, "SELECT * FROM t", {"section_records_sha256": "section_text_sha256"})
    m = json.loads((d / "manifest.json").read_text())
    m.pop("records_sha256")
    for f in m["files"]:
        data = (d / f["name"]).read_bytes()
        f.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        if f["name"].endswith(".parquet"):
            m["tables"][f["name"][: -len(".parquet")]]["sha256"] = f["sha256"]
    (d / "manifest.json").write_text(json.dumps(m))


def test_a_set_built_by_1x_rebuilds_unchecked_and_verifies_only_against_its_bundle(copy, tmp_path, capsys):
    _as_if_built_by_1x(copy)
    result = rebuild(copy, tmp_path / "b.json")
    assert not result.checked and result.records_sha256 == RECORDS_SHA256
    rc, stdout, _ = cli(capsys, "rebuild", copy, "-o", tmp_path / "c.json")
    assert rc == 0 and "not checked: tool" in stdout and "verify --roundtrip --full" in stdout
    rc, _, stderr = cli(capsys, "verify", copy, "--roundtrip")
    assert rc == 2 and "no records sha256" in stderr
    rc, stdout, _ = cli(capsys, "verify", copy, "--roundtrip", "--full", FIXTURE)
    assert rc == 0 and "round trip: identical" in stdout, stdout


def test_verify_roundtrip_reports_a_difference(copy, capsys):
    rewrite(copy / "submitter.parquet", "SELECT * REPLACE (upper(name) AS name) FROM t")
    rc, out, _ = cli(capsys, "verify", copy, "--roundtrip")
    assert rc == 1 and "round trip: DIFFERS" in out and "section submitter" in out


# --- the build's round-trip check ------------------------------------------------------------------------------

BASE = read_fixture()


def _build_one(section: str, edit) -> list[dict]:
    """Build one sections table from the excerpt with the first record of `section` changed by edit(text)."""
    sections = {s: list(r) for s, r in BASE.items()}
    k, r = sections[section][0]
    sections[section][0] = (k, edit(r))
    con = connect(sections)
    cur = con.execute(f"SELECT * FROM ({render_generic(SPEC.table(section))}) ORDER BY id")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def _obj(edit):
    return lambda r: json.dumps(edit(json.loads(r)), ensure_ascii=False, separators=(",", ":"))


def _check_one(section: str, edit, work: Path) -> None:
    """The build's round-trip check (roundtrip.check_rows) of one sections table, the first record of `section`
    changed by edit(text)."""
    sections = {s: list(r) for s, r in BASE.items()}
    k, r = sections[section][0]
    sections[section][0] = (k, edit(r))
    roundtrip_check(connect(sections), SPEC.table(section), work)


@pytest.mark.parametrize(
    "section, edit",
    [
        ("scv", _obj(lambda o: {**o, "description": None})),  # a JSON null: read as absent
        ("location", _obj(lambda o: {**o, "start": [None, None]})),  # an open Range: no column tells it apart
        ("allele", _obj(lambda o: {**o, "location": o["location"].replace("#/location/", "#/gene/")})),
        # a fraction in an integer column: the cast rounds it
        ("copyNumberCount", lambda r: re.sub(r'"copies":(\d+)', r'"copies":\1.01', r, count=1)),
    ],
)
def test_build_check_refuses_what_it_cannot_write_back(section, edit, tmp_path):
    key = BASE[section][0][0]
    with pytest.raises(RoundTripError, match=rf"table {section}: round trip: key {re.escape(key)}: .*differs"):
        _check_one(section, edit, tmp_path)


@pytest.mark.parametrize(
    "section, edit",
    [
        ("submitter", _obj(lambda o: dict(reversed(list(o.items()))))),  # keys out of BigQuery's sorted order
        ("scv", _obj(lambda o: dict(sorted(o.items())))),  # keys out of the STRUCT order
        ("copyNumberCount", lambda r: re.sub(r'"copies":(\d+)', r'"copies":\1.0', r, count=1)),  # 1 as 1.0
        ("submitter", lambda r: r.replace(":", ": ", 1)),  # not the producer's compact layout
        ("gene", lambda r: r.replace("ncbigene", "\\u006ecbigene", 1)),  # an escape the producer does not write
    ],
)
def test_build_check_passes_another_text_of_the_same_record(section, edit, tmp_path):
    """Key order, spacing, escapes and number spelling don't count (tool 1.x's byte check refused them)."""
    assert edit(BASE[section][0][1]) != BASE[section][0][1]
    _check_one(section, edit, tmp_path)


def test_build_check_passes_the_excerpt(tmp_path):
    con = connect(BASE)
    for t in SPEC.tables:
        roundtrip_check(con, t, tmp_path)


def test_empty_list_and_control_character_round_trip():
    """Absent and empty lists stay apart at every depth (an absent nested list is NULL, an empty one []), and a
    control character is escaped as the producer escapes it (Python's lower-case \\u001f)."""

    def edit(o):
        o["primaryCoding"]["iris"] = []
        o["name"] = o["name"] + "\x1f"
        return o

    raw = BASE["gene"][0][1]
    assert "iris" in json.loads(raw)["primaryCoding"] and "mappings" in json.loads(raw)
    rows = _build_one("gene", _obj(edit))
    edited = next(r for r in rows if r["id"] == BASE["gene"][0][0])
    assert edited["primaryCoding"]["iris"] == [] and edited["name"].endswith("\x1f")
    assert json.dumps("\x1f") == '"\\u001f"'
    other = [
        r
        for r in rows
        if r is not edited and r["mappings"] is not None and not any(m["coding"]["iris"] for m in r["mappings"])
    ]
    assert all(m["coding"]["iris"] is None for r in other for m in r["mappings"])


def test_every_section_has_one_table():
    sections = root_parts(load_schema_set(str(CLINVAR_SCHEMA)), "schema_version").collections
    assert sorted(t.sections[0] for t in SPEC.tables) == sorted(sections)
