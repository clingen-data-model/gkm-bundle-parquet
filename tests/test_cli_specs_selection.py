"""The CLI end to end on the real 1.1.0 excerpt with ClinVar-GKM's pairing: the sections spec, selective builds
(`--driver` with `--where`, `--ids`, `--ids-file`, the follow modes, `--max-depth`, `--sections`), the `select`
command, and `verify` of each result (the directory check, the bundle recompute, a subset against its
mini-bundle).

Expectations come from the bundle records parsed here with stdlib json, independently of the tool; the follow and
depth semantics themselves are checked against a breadth-first search in tests/test_select.py.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import duckdb
import pytest
from _fixture import FIXTURE, PAIRING, SECTIONS, SPEC_SOURCE, reader_rules
from _input_helpers import local_input

from clinvar_gkm_parquet import select as sel
from clinvar_gkm_parquet.cli import main

FULL = ["--full", FIXTURE, *PAIRING]
FIXTURE_SHA = (FIXTURE.parent / (FIXTURE.name + ".sha256")).read_text().split()[0]
SOMATIC_VCV = "VCV004530381.1-S-SCI-CP-T1"  # ADR 0004's measured example: `out` reaches 14 records


# --- helpers ---------------------------------------------------------------------------------------------------


def cli(capsys, *args) -> tuple[int, str, str]:
    rc = main([str(a) for a in args])
    got = capsys.readouterr()
    return rc, got.out, got.err


def kv(path: Path) -> dict[str, str]:
    rows = duckdb.connect().execute(f"SELECT key, value FROM parquet_kv_metadata('{path}')").fetchall()
    p = "clinvar_gkm_parquet."
    return {k.decode()[len(p) :]: v.decode() for k, v in rows if k.decode().startswith(p)}


def ids(path: Path, col: str = "id") -> list[str]:
    return [r[0] for r in duckdb.connect().execute(f"SELECT \"{col}\" FROM read_parquet('{path}')").fetchall()]


def parse_bundle(path: Path) -> dict[str, list[tuple[str, str]]]:
    """section -> [(key, record text)], in file order, with stdlib json (not the tool's reader)."""
    out: dict[str, list[tuple[str, str]]] = {}
    section = None
    dec = json.JSONDecoder()
    for line in gzip.decompress(Path(path).read_bytes()).decode().split("\n"):
        if line.startswith('  "') and line.endswith(": {"):
            section = json.loads(line.strip()[:-3])
            out[section] = []
        elif line.startswith("    "):
            key, end = dec.raw_decode(line[4:])
            out[section].append((key, line[4 + end + 2 :].removesuffix(",")))
    return out


def walk_refs(node, path: str = ""):
    """Every `#/<section>/<id>` string in document order, with its path."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk_refs(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for v in node:
            yield from walk_refs(v, path + "[]")
    elif isinstance(node, str) and node.startswith("#/"):
        section, _, key = node[2:].partition("/")
        yield {"path": path, "section": section, "id": key}


def built_keys(out: Path) -> set[tuple[str, str]]:
    """(section, key) of every row of a build."""
    return {(s, k) for s in SECTIONS for k in ids(out / f"{s}.parquet")}


def bundle_keys(path: Path) -> set[tuple[str, str]]:
    return {(s, k) for s, recs in parse_bundle(path).items() for k, _ in recs}


def pointing_to(target: str) -> set[tuple[str, str]]:
    """Records of the fixture holding the pointer `target`."""
    return {
        (s, k)
        for s, recs in parse_bundle(FIXTURE).items()
        for k, r in recs
        if any(f"#/{x['section']}/{x['id']}" == target for x in walk_refs(json.loads(r)))
    }


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --- the spec ----------------------------------------------------------------------------------------------------


def test_the_pairing_builds_the_sections_spec(tmp_path, capsys):
    """The sections spec, one table per section, keyed by the bundle key, with no `data` column."""
    out = tmp_path / "out"
    rc, stdout, _ = cli(capsys, "build", *FULL, "--out", out)
    assert rc == 0 and "built 20 table(s) of spec sections 2.0, 331 rows" in stdout
    assert sorted(p.stem for p in out.glob("*.parquet")) == sorted(SECTIONS)
    records = parse_bundle(FIXTURE)
    con = duckdb.connect()
    for s in SECTIONS:
        cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{out / s}.parquet')").fetchall()]
        assert cols[0] == "id" and "data" not in cols
        assert ids(out / f"{s}.parquet") == sorted(k for k, _ in records.get(s, []))
        props = {p for _, r in records.get(s, []) for p in json.loads(r)} - {"id"}
        assert props <= set(cols) | {"start", "end"}  # location's start/end are the loc_* range columns
    meta = kv(out / "scv.parquet")
    assert (meta["table_spec"], meta["table_spec_version"]) == ("sections", "2.0")
    assert "selection" not in meta and "table_spec_builtin" not in meta
    m = json.loads((out / "manifest.json").read_text())
    source = SPEC_SOURCE
    assert m["table_spec"] == {
        "name": "sections",
        "version": "2.0",
        "builtin": True,
        "sha256": source.sha256,
        "schema_set_sha256": source.schema_set_sha256,
        "column_set_sha256": source.column_set_sha256,
        "file": "table-spec.json",
        # The fixture column set narrows the generated default: a named projection (ADR 0007, ADR 0008 decision 3).
        "policy_version": "2.0",
        "column_set_kind": "projection",
        "projection": "clinvar-gkm-producer",
    }
    assert (meta["policy_version"], meta["column_set_kind"], meta["projection"]) == (
        "2.0",
        "projection",
        "clinvar-gkm-producer",
    )
    assert cli(capsys, "verify", out)[0] == 0
    assert cli(capsys, "verify", out, "--bundle-recompute", *FULL, "--work-dir", tmp_path / "w")[0] == 0


# --- selective builds ------------------------------------------------------------------------------------------


def test_driver_where_follow_in(tmp_path, capsys):
    """--where over the driver table's columns; `in` with depth 1 adds every record pointing to the driver record.
    Then select regenerates the mini-bundle, and verify recomputes the subset against it."""
    out = tmp_path / "out"
    where = "name = 'OMIM'"
    opts = ["--driver", "submitter", "--where", where, "--follow", "in", "--max-depth", "1"]
    rc, stdout, _ = cli(capsys, "build", *FULL, "--out", out, *opts)
    assert rc == 0 and "a selection driven by submitter" in stdout
    omim = "clinvar.submitter:3"
    want = {("submitter", omim)} | pointing_to(f"#/submitter/{omim}")
    assert len(want) > 1 and built_keys(out) == want
    meta = kv(out / "scv.parquet")
    params = json.loads(meta["selection"])
    assert params["driver"] == "submitter" and params["where"] == where and params["ids_sha256"] is None
    assert (params["follow"], params["max_depth"], params["sections"]) == ("in", 1, None)
    assert params["seeds"] == {"submitter": {"count": 1, "sha256": sel.ids_sha256([omim])}}
    assert params["selected_records"] == len(want)
    # the source keys still name the published input
    assert meta["source_bundle_sha256"] == FIXTURE_SHA
    m = json.loads((out / "manifest.json").read_text())
    assert m["selection"] == params and m["inputs"]["full"]["sha256"] == FIXTURE_SHA
    assert sum(m["sections"].values()) == len(want)  # what the tables were built from: the mini-bundle
    assert cli(capsys, "verify", out)[0] == 0

    # select writes the same mini-bundle; the recompute runs against it
    mini = tmp_path / "mini.json.gz"
    rc, stdout, _ = cli(capsys, "select", *FULL, "--out", mini, *opts)
    assert rc == 0 and f"selection: {meta['selection']}" in stdout
    assert sha(mini) == params["selected_sha256"] and bundle_keys(mini) == want
    rc = cli(capsys, "verify", out, "--bundle-recompute", "--full", mini, "--work-dir", tmp_path / "w")[0]
    assert rc == 0
    recompute = ["--bundle-recompute", "--full", mini, *PAIRING, "--work-dir", tmp_path / "w2"]
    assert cli(capsys, "verify", out, *recompute)[0] == 0
    rc, _, err = cli(capsys, "select", *FULL, "--out", mini, *opts)
    assert rc == 2 and "already exists" in err
    # recompute against anything but the mini-bundle is refused
    rc, _, err = cli(capsys, "verify", out, "--bundle-recompute", *FULL)
    assert rc == 2 and "is not the mini-bundle these files were built from" in err


def test_driver_where_on_sections(tmp_path, capsys):
    """The default spec: --where over the driver table's own typed columns (a property, and an extension's
    json-type slot)."""
    out = tmp_path / "out"
    where = "name LIKE 'NC_000002%'"
    rc, _, _ = cli(capsys, "build", *FULL, "--out", out, "--driver", "allele", "--where", where, "--max-depth", 0)
    assert rc == 0
    want = {k for k, r in parse_bundle(FIXTURE)["allele"] if json.loads(r)["name"].startswith("NC_000002")}
    assert want and set(ids(out / "allele.parquet")) == want
    assert built_keys(out) == {("allele", k) for k in want}
    assert cli(capsys, "verify", out)[0] == 0

    out = tmp_path / "out2"
    where = "len(list_filter(extensions, lambda e: e.name = 'assembly' AND e.value_string = 'GRCh37')) > 0"
    opts = ["--driver", "sequenceReference", "--where", where, "--max-depth", 0]
    assert cli(capsys, "build", *FULL, "--out", out, *opts)[0] == 0
    want = {
        k
        for k, r in parse_bundle(FIXTURE)["sequenceReference"]
        if {"name": "assembly", "value": "GRCh37"} in json.loads(r)["extensions"]
    }
    assert want and set(ids(out / "sequenceReference.parquet")) == want


def test_driver_where_on_a_struct_field(tmp_path, capsys):
    """--where over a struct field of the driver table."""
    out = tmp_path / "out"
    where = "quality.name = 'expert panel'"
    rc, _, err = cli(capsys, "build", *FULL, "--out", out, "--driver", "vcv", "--where", where)
    assert rc == 0, err
    want = {k for k, r in parse_bundle(FIXTURE)["vcv"] if json.loads(r)["quality"]["name"] == "expert panel"}
    assert want and set(ids(out / "vcv.parquet")) == want
    params = json.loads(kv(out / "vcv.parquet")["selection"])
    assert params["seeds"] == {"vcv": {"count": len(want), "sha256": sel.ids_sha256(want)}}
    assert len(list(out.glob("*.parquet"))) == len(SECTIONS)
    assert cli(capsys, "verify", out)[0] == 0
    mini = tmp_path / "mini.json.gz"
    opts = ["--driver", "vcv", "--where", where]
    assert cli(capsys, "select", *FULL, "--out", mini, *opts)[0] == 0
    assert sha(mini) == params["selected_sha256"]
    rc = cli(capsys, "verify", out, "--bundle-recompute", "--full", mini, "--work-dir", tmp_path / "w")[0]
    assert rc == 0


def test_ids_and_ids_file(tmp_path, capsys):
    scvs = [k for k, _ in parse_bundle(FIXTURE)["scv"]][:3]
    f = tmp_path / "ids.txt"
    f.write_text(f"{scvs[0]}\n\n  {scvs[1]}  \n")
    out = tmp_path / "out"
    opts = ["--driver", "scv", "--ids-file", f, "--ids", scvs[2], "--max-depth", "0"]
    assert cli(capsys, "build", *FULL, "--out", out, *opts)[0] == 0
    assert built_keys(out) == {("scv", k) for k in scvs}
    params = json.loads(kv(out / "gene.parquet")["selection"])
    assert params["where"] is None and params["ids_sha256"] == sel.ids_sha256(scvs)
    assert cli(capsys, "verify", out)[0] == 0


def test_where_and_ids_are_united(tmp_path, capsys):
    vcvs = [k for k, _ in parse_bundle(FIXTURE)["vcv"]]
    out = tmp_path / "out"
    opts = ["--driver", "vcv", "--where", f"id = '{vcvs[0]}'", "--ids", vcvs[1], "--max-depth", "0"]
    assert cli(capsys, "build", *FULL, "--out", out, *opts)[0] == 0
    assert set(ids(out / "vcv.parquet")) == {vcvs[0], vcvs[1]}


def test_no_where_and_no_ids_selects_every_driver_row(tmp_path, capsys):
    out = tmp_path / "out"
    assert cli(capsys, "build", *FULL, "--out", out, "--driver", "vcv", "--max-depth", "0")[0] == 0
    want = {k for k, _ in parse_bundle(FIXTURE)["vcv"]}
    assert want and built_keys(out) == {("vcv", k) for k in want}
    assert json.loads(kv(out / "vcv.parquet")["selection"])["seeds"]["vcv"]["count"] == len(want)


@pytest.mark.parametrize("follow, depth", [("out", None), ("out", 2), ("both", 2), ("in", None)])
def test_follow_modes_depth_and_sections(tmp_path, capsys, follow, depth):
    """The build's mini-bundle is the engine's selection with the same options (the engine is checked against a
    breadth-first search in test_select.py); only --sections are written."""
    sections = ["vcv", "propositionSomaticClinicalImpact", "evidenceLine", "scv", "variation", "submitter"]
    opts = ["--driver", "vcv", "--ids", SOMATIC_VCV, "--follow", follow, "--sections", ",".join(sections)]
    if depth is not None:
        opts += ["--max-depth", depth]
    out = tmp_path / "out"
    assert cli(capsys, "build", *FULL, "--out", out, *opts)[0] == 0
    engine = sel.select(
        sel.BundleFileSource(local_input(FIXTURE, sha=FIXTURE_SHA), rules=reader_rules()),
        {"vcv": [SOMATIC_VCV]},
        tmp_path / "engine.json.gz",
        follow=follow,
        max_depth=depth,
        sections=sections,
    )
    got = built_keys(out)
    assert got == bundle_keys(engine.path) and {s for s, _ in got} <= set(sections)
    params = json.loads(kv(out / "vcv.parquet")["selection"])
    assert params["selected_sha256"] == engine.sha256
    assert params["sections"] == [s for s in SECTIONS if s in sections]  # in bundle order
    assert (params["follow"], params["max_depth"]) == (follow, depth)
    rc = cli(capsys, "verify", out, "--bundle-recompute", "--full", engine.path, "--work-dir", tmp_path / "w")[0]
    assert rc == 0


# --- failures --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "opts, message",
    [
        (["--driver", "nope"], "--driver nope: not a table of spec sections 2.0"),
        (["--driver", "vcv", "--where", "id = 'x'; DROP TABLE t"], "a ';' is not allowed"),
        (
            ["--driver", "vcv", "--where", "id = 'no-such-vcv'"],
            "no driver keys: --where \"id = 'no-such-vcv'\" matches no row of driver table vcv",
        ),
        (
            ["--driver", "vcv", "--tables", "gene,vcv", "--sections", "vcv,evidenceLine"],
            "--tables and --sections conflict: gene reads gene, outside --sections vcv,evidenceLine",
        ),
        (["--driver", "vcv", "--sections", "gene"], "must include the driver's section vcv"),
        (["--where", "id = 'x'"], "--where need(s) --driver"),
        (["--driver", "vcv", "--where", "no_such_column = 1"], "--where 'no_such_column = 1' on driver table vcv"),
        (["--driver", "vcv", "--ids", "VCV-not-there"], "1 driver key(s) not in clinvar-gkm_excerpt.json.gz"),
    ],
)
def test_selection_failures(tmp_path, capsys, opts, message):
    out = tmp_path / "out"
    rc, _, err = cli(capsys, "build", *FULL, "--out", out, *opts)
    assert rc == 2 and message in err, err
    assert not out.exists() or not list(out.iterdir())  # no partial set
