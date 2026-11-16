import json
import shutil
from argparse import Namespace
from pathlib import Path

import duckdb
import pytest
from _build_helpers import KV, make_con, make_spec, passthrough_spec, passthrough_sql, render
from _fixture import FIXTURE

from gkm_bundle_parquet import build as build_mod
from gkm_bundle_parquet import verify as verify_mod
from gkm_bundle_parquet.build import KV_KEYS, KV_PREFIX, build_tables
from gkm_bundle_parquet.cli import main
from gkm_bundle_parquet.model import BuildInputs
from gkm_bundle_parquet.sources import resolve_input
from gkm_bundle_parquet.verify import recompute, recompute_failed, verify_dir

ALL = ["thing", "kv", "evA", "evB"]


@pytest.fixture
def built(tmp_path):
    spec, con = make_spec(), make_con()
    out = tmp_path / "out"
    build_tables(con, spec, ALL, out, KV, render=render)
    return spec, con, out


def _rewrite(con, path: Path, select: str, kv: dict | None = None) -> None:
    """Rewrite a Parquet file from a SELECT over `src` (the file's current content)."""
    tmp = path.with_suffix(".rewrite")
    shutil.move(path, tmp)
    meta = ""
    if kv is not None:
        items = ", ".join(f"'{KV_PREFIX}{k}': '{v}'" for k, v in kv.items())
        meta = f", KV_METADATA {{{items}}}"
    con.execute(
        f"COPY (WITH src AS (SELECT * FROM read_parquet('{tmp}')) {select}) TO '{path}' (FORMAT parquet{meta})"
    )
    tmp.unlink()


# --- verify_dir ------------------------------------------------------------------------------------------------


def test_verify_dir_passes_on_a_build(built):
    spec, _, out = built
    assert verify_dir(out, spec) == []


def test_verify_dir_finds_each_problem(built):
    spec, con, out = built
    (out / "evB.parquet").unlink()
    shutil.copy(out / "kv.parquet", out / "stray.parquet")
    _rewrite(con, out / "kv.parquet", "SELECT key, value, 1 AS name FROM src", kv={**KV, "table": "kv"})
    _rewrite(
        con,
        out / "evA.parquet",
        "SELECT * FROM src",
        kv={**KV, "table": "evA", "source_release": "2026-10-11"},
    )
    (out / "manifest.json").write_text(json.dumps({"tables": {"thing": {"rows": 31, "sha256": "00"}}}))
    problems = verify_dir(out, spec)
    text = "\n".join(problems)
    assert "evB: evB.parquet is missing" in text
    assert "stray: stray.parquet is not a table of spec 9.9" in text
    assert "kv: schema: column name: file INTEGER != spec VARCHAR" in text
    assert "gkm_bundle_parquet.source_release differs across files" in text
    assert "manifest.json: thing rows 31 != file 30" in text
    assert "manifest.json: thing sha256 00 != file" in text
    assert len(problems) == 6, problems


def test_verify_dir_missing_metadata(built):
    spec, con, out = built
    _rewrite(con, out / "kv.parquet", "SELECT * FROM src")
    (problem,) = verify_dir(out, spec)
    assert problem.startswith("kv: key-value metadata lacks gkm_bundle_parquet.tool_version")
    assert all(KV_PREFIX + k in problem for k in KV_KEYS)


# --- bundle recompute ------------------------------------------------------------------------------------------


def test_recompute_passes(built):
    spec, con, out = built
    groups = recompute(con, spec, out)
    assert [g["tables"] for g in groups] == [["thing"], ["kv"], ["evA", "evB"]]
    assert not any(recompute_failed(g) for g in groups)
    ev = groups[2]
    assert ev["bundle_keys"] == 14 and ev["rows"] == {"evA": 7, "evB": 7}


def test_recompute_finds_each_difference(built):
    spec, con, out = built
    con.execute("DELETE FROM bundle_kv WHERE key = 'K1'")  # a row the bundle doesn't have
    con.execute("""INSERT INTO bundle_thing VALUES ('T5000', '{"id":"T5000"}')""")  # a key no row has
    con.execute("""UPDATE bundle_ev SET record = '{"id":"B002","w":99}' WHERE key = 'B002'""")  # data differs
    _rewrite(
        con, out / "evB.parquet", "SELECT * FROM src UNION ALL SELECT 'A001', data FROM src WHERE id = 'B001'"
    )
    by = {tuple(g["tables"]): g for g in recompute(con, spec, out)}
    kv, thing, ev = by[("kv",)], by[("thing",)], by[("evA", "evB")]
    assert kv["not_in_bundle"] == {"count": 1, "samples": [{"key": "K1", "table": "kv"}]}
    assert kv["missing"]["count"] == 0 and kv["data_differs"]["count"] == 0
    assert thing["missing"] == {"count": 1, "samples": [{"key": "T5000", "table": None}]}
    assert thing["not_in_bundle"]["count"] == thing["data_differs"]["count"] == 0
    assert ev["data_differs"]["count"] == 2  # B002, and the duplicate A001 in evB (its data is B001's)
    assert {s["key"] for s in ev["data_differs"]["samples"]} == {"A001", "B002"}
    assert ev["duplicates"] == {"count": 1, "samples": [{"key": "A001", "tables": ["evA", "evB"]}]}
    assert all(recompute_failed(g) for g in (kv, thing, ev))


def test_recompute_reports_missing_file(built):
    spec, con, out = built
    (out / "evA.parquet").unlink()
    g = [g for g in recompute(con, spec, out) if "evA" in g["tables"]][0]
    assert g["problems"] == ["not checked: evA missing"] and recompute_failed(g)


# --- the command -----------------------------------------------------------------------------------------------


def _vargs(d, **kw):
    base = dict(
        dir=Path(d),
        bundle_recompute=False,
        full=None,
        sha256=None,
        bundle_schema=None,
        column_set=None,
        report=None,
        work_dir=None,
        memory_limit=None,
        threads=None,
    )
    base.update(kw)
    return Namespace(**base)


@pytest.fixture
def spec_patched(monkeypatch):
    spec = make_spec()
    monkeypatch.setattr(verify_mod, "spec_for_dir", lambda con, out_dir: spec)
    return spec


def test_command_exit_codes_and_report(built, tmp_path, spec_patched, capsys):
    _, con, out = built
    assert verify_mod.run_verify_command(_vargs(out)) == 0
    _rewrite(con, out / "thing.parquet", "SELECT * EXCLUDE (n) FROM src", kv=KV | {"table": "thing"})
    report = tmp_path / "rep" / "verify.md"
    assert verify_mod.run_verify_command(_vargs(out, report=report)) == 1
    md = report.read_text()
    assert "Result: **differences**" in md and "thing: schema:" in md
    js = json.loads(report.with_suffix(".json").read_text())
    assert js["passed"] is False and "oracle" not in js and js["spec_check"]["problems"]
    assert "directory check: 1 problem(s)" in capsys.readouterr().out


def test_command_usage_errors(built, tmp_path, spec_patched):
    _, _, out = built
    assert verify_mod.run_verify_command(_vargs(tmp_path / "nope")) == 2
    assert verify_mod.run_verify_command(_vargs(out, bundle_recompute=True)) == 2
    assert verify_mod.run_verify_command(_vargs(out, full="x.json.gz")) == 2
    assert verify_mod.run_verify_command(_vargs(out, bundle_schema="s.json")) == 2  # without --column-set
    assert main(["verify", str(tmp_path / "nope")]) == 2


def test_command_spec_problems_exit_1(built, spec_patched):
    _, _, out = built
    (out / "kv.parquet").unlink()
    assert verify_mod.run_verify_command(_vargs(out)) == 1


def test_command_bundle_recompute(tmp_path, monkeypatch):
    """The streaming recompute (one section at a time, build.SectionPipeline) against a pass-through build of
    the fixture with one row planted that the bundle doesn't have."""
    spec = passthrough_spec()
    monkeypatch.setattr(build_mod.sqltemplate, "macros_sql", lambda: "SELECT 1")
    monkeypatch.setattr(build_mod, "render_table", passthrough_sql)
    monkeypatch.setattr(verify_mod, "spec_for_dir", lambda con, out_dir: spec)
    out = tmp_path / "out"
    build_mod.build(BuildInputs(full=resolve_input(str(FIXTURE))), out, spec=spec)
    con = duckdb.connect()
    kv = {k[len(KV_PREFIX) :]: v for k, v in _file_kv(con, out / "gene.parquet").items()}
    _rewrite(con, out / "gene.parquet", "SELECT * FROM src UNION ALL SELECT 'synthetic:extra', '{}'", kv=kv)
    report = tmp_path / "rc.md"
    args = _vargs(
        out,
        bundle_recompute=True,
        full=str(FIXTURE),
        report=report,
        work_dir=tmp_path / "w",
    )
    assert verify_mod.run_verify_command(args) == 1
    js = json.loads(report.with_suffix(".json").read_text())
    groups = {tuple(g["tables"]): g for g in js["bundle_recompute"]}
    assert len(groups) == len(spec.tables)
    assert groups[("gene",)]["not_in_bundle"]["samples"] == [{"key": "synthetic:extra", "table": "gene"}]
    assert not any(recompute_failed(g) for t, g in groups.items() if t != ("gene",))
    assert not list((tmp_path / "w").iterdir())  # the verify work dir is removed


def _file_kv(con, path) -> dict[str, str]:
    rows = con.execute(f"SELECT key, value FROM parquet_kv_metadata('{path}')").fetchall()
    return {k.decode(): v.decode() for k, v in rows if k.decode().startswith(KV_PREFIX)}


def test_cli_unreadable_parquet_is_an_input_error(tmp_path, capsys):
    # A truncated Parquet file raises duckdb.Error; the CLI reports it as an input error (exit 2), not as
    # differences (exit 1).
    for name in ("gene", "scv"):
        (tmp_path / f"{name}.parquet").write_bytes(b"PAR1 truncated")
    assert main(["verify", str(tmp_path)]) == 2
    assert "error:" in capsys.readouterr().err
