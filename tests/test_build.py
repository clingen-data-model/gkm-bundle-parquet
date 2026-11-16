import gzip
import hashlib
import json
import threading
from argparse import Namespace
from pathlib import Path

import duckdb
import pytest
from _build_helpers import (
    KV,
    SQL,
    load_sections,
    make_con,
    make_spec,
    render,
    render_with,
    section_rows,
)
from _build_helpers import passthrough_spec as _passthrough_spec
from _build_helpers import passthrough_sql as _passthrough_sql
from _fixture import CLINVAR_COLUMNS, CLINVAR_SCHEMA, COUNTS, FIXTURE, SECTIONS, SPEC, SPEC_SOURCE
from _input_helpers import bundle_bytes, fixture_sections, local_input, write_gz

from clinvar_gkm_parquet import build as build_mod
from clinvar_gkm_parquet.build import (
    KV_KEYS,
    KV_PREFIX,
    build,
    build_order,
    build_tables,
    kv_metadata,
    resolve_build_inputs,
    run_build_command,
)
from clinvar_gkm_parquet.errors import BundleFormatError, InputError, SpecError
from clinvar_gkm_parquet.model import BuildInputs, InputFile


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _kv(con, path):
    rows = con.execute(f"SELECT key, value FROM parquet_kv_metadata('{path}')").fetchall()
    return {k.decode(): v.decode() for k, v in rows}


# --- the table stage -------------------------------------------------------------------------------------------


def test_build_tables_writes_every_table(tmp_path):
    spec, con = make_spec(), make_con()
    results = build_tables(con, spec, [t.name for t in spec.tables], tmp_path, KV, render=render)
    assert {r.table for r in results} == {"thing", "kv", "evA", "evB"}
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "evA.parquet",
        "evB.parquet",
        "kv.parquet",
        "thing.parquet",
    ]
    by = {r.table: r for r in results}
    assert by["thing"].rows == 30 and by["kv"].rows == 5 and by["evA"].rows == 7 and by["evB"].rows == 7
    for r in results:
        assert r.sha256 == _sha(r.file) and r.size == r.file.stat().st_size
        got = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{r.file}')").fetchall()
        assert [(g[0], g[1]) for g in got] == spec.table(r.table).describe()
        kv = _kv(con, r.file)
        assert list(kv) == [KV_PREFIX + k for k in KV_KEYS]
        assert kv[KV_PREFIX + "table"] == r.table
        assert kv[KV_PREFIX + "source_release"] == "2026-10-18"


def test_build_tables_values_order_and_writer_settings(tmp_path):
    spec, con = make_spec(), make_con(section_rows(5000))
    build_tables(con, spec, ["thing"], tmp_path, KV, render=render)
    f = tmp_path / "thing.parquet"
    ids = [r[0] for r in con.execute(f"SELECT id FROM read_parquet('{f}')").fetchall()]
    assert ids == sorted(ids)
    meta = con.execute(
        f"SELECT DISTINCT row_group_id, row_group_num_rows, compression FROM parquet_metadata('{f}') ORDER BY 1"
    ).fetchall()
    assert [m[1] for m in meta] == [2048, 2048, 904]
    assert {m[2] for m in meta} == {"SNAPPY"}
    row = con.execute(f"SELECT * FROM read_parquet('{f}') WHERE id = 'T0001'").fetchone()
    assert row[:4] == ("T0001", "thing 1", 1, ["t1", "x"])
    assert row[4] == {"a": "a1", "b": [1, 2]}
    assert row[5] == section_rows(2)["thing"][1][1]
    # empty-list rule and absent struct
    row0 = con.execute(f"SELECT tags, info FROM read_parquet('{f}') WHERE id = 'T0000'").fetchone()
    assert row0 == ([], None)


def test_build_order_largest_last_and_sections_released():
    spec = make_spec()
    order = build_order(spec, [t.name for t in spec.tables], {"thing": 30, "kv": 5, "ev": 14})
    assert [t.name for t in order] == ["kv", "evA", "evB", "thing"]


def test_on_section_done_after_last_reader(tmp_path):
    spec, con = make_spec(), make_con()
    events = []
    build_tables(
        con,
        spec,
        [t.name for t in spec.tables],
        tmp_path,
        KV,
        render=render,
        on_section_done=events.append,
        log=lambda m: events.append(m) if m.endswith("writing") else None,
    )
    assert events == [
        "table kv: writing",
        "kv",
        "table evA: writing",
        "table evB: writing",
        "ev",
        "table thing: writing",
        "thing",
    ]


def test_describe_mismatch_fails_and_leaves_no_file(tmp_path):
    spec, con = make_spec(), make_con()
    bad = SQL["thing"].replace("AS BIGINT) AS n", "AS INTEGER) AS n")
    with pytest.raises(SpecError, match=r"thing: DESCRIBE differs.*'n', 'INTEGER'"):
        build_tables(con, spec, ["thing"], tmp_path, KV, render=render_with(thing=bad))
    assert list(tmp_path.iterdir()) == []


def test_row_count_mismatch_fails(tmp_path):
    spec, con = make_spec(), make_con()
    bad = SQL["thing"] + " WHERE key <> 'T0003'"
    with pytest.raises(SpecError, match=r"29 rows .* hold 30 keys.*T0003 \(missing\)"):
        build_tables(con, spec, ["thing"], tmp_path, KV, render=render_with(thing=bad))
    assert not (tmp_path / "thing.parquet").exists()


def test_data_for_wrong_key_fails(tmp_path):
    spec, con = make_spec(), make_con()
    # data of T0001 and T0002 swapped: counts match, (id, data) pairs don't
    bad = SQL["kv"].replace(
        "record AS value",
        "CASE key WHEN 'K1' THEN (SELECT record FROM bundle_kv WHERE key = 'K2') "
        "WHEN 'K2' THEN (SELECT record FROM bundle_kv WHERE key = 'K1') ELSE record END AS value",
    )
    with pytest.raises(SpecError, match=r"kv: \(id, data\) pairs differ.*K1 \(data differs\); K2"):
        build_tables(con, spec, ["kv"], tmp_path, KV, render=render_with(kv=bad))


def test_duplicate_sort_key_fails(tmp_path):
    spec, con = make_spec(), make_con()
    bad = SQL["kv"] + " UNION ALL SELECT 'K1', '{}', NULL"
    with pytest.raises(SpecError, match="sort key key is not unique"):
        build_tables(con, spec, ["kv"], tmp_path, KV, render=render_with(kv=bad))


def test_evidence_line_split_must_cover_the_section(tmp_path):
    spec, con = make_spec(), make_con()
    con.execute("""INSERT INTO bundle_ev VALUES ('C001', '{"id":"C001"}')""")
    with pytest.raises(SpecError, match=r"evA, evB: 14 rows \(evA=7, evB=7\) but section\(s\) ev hold 15 keys"):
        build_tables(con, spec, ["evA", "evB"], tmp_path, KV, render=render)
    assert list(tmp_path.iterdir()) == []


def test_evidence_line_split_overlap_fails(tmp_path):
    spec, con = make_spec(), make_con()
    bad = "SELECT key AS id, record AS data FROM bundle_ev WHERE key LIKE 'B%' OR key = 'A001'"
    with pytest.raises(SpecError, match="15 rows"):
        build_tables(con, spec, ["evA", "evB"], tmp_path, KV, render=render_with(evB=bad))


def test_subset_of_a_split_checks_rows_against_the_section(tmp_path):
    spec, con = make_spec(), make_con()
    (r,) = build_tables(con, spec, ["evA"], tmp_path, KV, render=render)
    assert r.rows == 7
    bad = "SELECT key AS id, record || ' ' AS data FROM bundle_ev WHERE key LIKE 'A%'"
    with pytest.raises(SpecError, match=r"evA: rows whose \(id, data\) is not .*A000"):
        build_tables(con, spec, ["evA"], tmp_path / "bad", KV, render=render_with(evA=bad))


def test_unknown_table_and_query_error(tmp_path):
    spec, con = make_spec(), make_con()
    with pytest.raises(SpecError, match="unknown table"):
        build_tables(con, spec, ["nope"], tmp_path, KV, render=render)
    with pytest.raises(SpecError, match="table kv: query failed"):
        build_tables(con, spec, ["kv"], tmp_path, KV, render=render_with(kv="SELECT error('unknown extension x')"))


def test_query_error_mid_copy_leaves_no_file(tmp_path):
    spec, con = make_spec(), make_con()
    bad = SQL["kv"].replace(
        "record AS value", "CASE WHEN key = 'K2' THEN error('bad record K2') ELSE record END AS value"
    )
    with pytest.raises(SpecError, match="table kv: query failed.*bad record K2"):
        build_tables(con, spec, ["kv"], tmp_path, KV, render=render_with(kv=bad))
    assert list(tmp_path.iterdir()) == []


def test_empty_section_gives_an_empty_table(tmp_path):
    spec = make_spec()
    rows = section_rows()
    rows["kv"] = []
    con = make_con(rows)
    (r,) = build_tables(con, spec, ["kv"], tmp_path, KV, render=render)
    assert r.rows == 0
    assert con.execute(f"SELECT count(*) FROM read_parquet('{r.file}')").fetchone()[0] == 0


def _build_once(out: Path, threads: int, n: int = 30_000) -> dict[str, str]:
    con = duckdb.connect()
    con.execute(f"SET threads = {threads}")
    load_sections(con, {"kv": [], "ev": []})
    # thing: generated in SQL, in scrambled key order, so the sort does real work
    con.execute(
        f"""CREATE OR REPLACE TABLE bundle_thing AS
            SELECT printf('T%07d', (i * 7919) % {n}) AS key,
                   '{{"name":"n' || i || '","n":' || i || ',"tags":["a","b' || (i % 5) || '"],'
                   || '"info":{{"a":"x' || i || '","b":[' || i || ']}}}}' AS record
            FROM range({n}) t(i)"""
    )
    spec = make_spec()
    results = build_tables(con, spec, ["thing", "kv", "evA", "evB"], out, KV, render=render)
    con.close()
    return {r.table: r.sha256 for r in results}


def test_determinism_across_runs_and_threads(tmp_path):
    a = _build_once(tmp_path / "a", threads=1)
    b = _build_once(tmp_path / "b", threads=4)
    c = _build_once(tmp_path / "c", threads=8)
    assert a == b == c
    assert duckdb.__version__ == "1.5.4"


def test_kv_metadata_has_no_timestamp_and_no_release():
    """source_deltas and source_release stay in every file (a 1.x reader expects them) but name nothing: the
    tool reads one bundle and doesn't know which release it is."""
    kv = kv_metadata(table_spec_version="1.0", bundle_schema_version="1.1.0", source_bundle_sha256="f" * 64)
    assert set(kv) == set(KV_KEYS) - {"table"}
    assert (kv["source_deltas"], kv["source_release"]) == ("[]", "")


# --- the whole build: the section pipeline on the real fixture -------------------------------------------------
#
# A pass-through spec (one table per bundle section: id = key, data = record) exercises the real reader,
# pipeline, build and manifest on a real bundle without depending on the table SQL (tests/test_tables*.py).


def _failing_sql(t) -> str:
    """Fails while the rows are read, as a record the spec can't map does."""
    return f"""SELECT key AS id, error('boom')::VARCHAR AS data FROM "bundle_{t.name}\""""


@pytest.fixture
def passthrough(monkeypatch):
    monkeypatch.setattr(build_mod.sqltemplate, "macros_sql", lambda: "SELECT 1")
    monkeypatch.setattr(build_mod, "render_table", _passthrough_sql)
    return _passthrough_spec()


def _fixture_inputs(full: InputFile | None = None) -> BuildInputs:
    sha = (FIXTURE.parent / (FIXTURE.name + ".sha256")).read_text().split()[0]
    inp = full or InputFile(
        source=str(FIXTURE),
        name=FIXTURE.name,
        expected_sha256=sha,
        sha256_source="sidecar",
        size=FIXTURE.stat().st_size,
    )
    return BuildInputs(full=inp)


def _stage_dirs(work: Path) -> dict[str, int]:
    """section -> chunk files present, under every build-*/stage of `work`."""
    out = {}
    for d in work.glob("build-*/stage/*"):
        n = len(list(d.glob("*.tsv.gz")))
        if n:
            out[d.name] = n
    return out


def test_build_end_to_end(passthrough, tmp_path):
    spec = passthrough
    out = tmp_path / "out"
    res = build(_fixture_inputs(), out, spec=spec, threads=2, memory_limit="1GB")
    assert [t.table for t in res.tables] == list(SECTIONS)  # spec order
    assert sorted(p.name for p in out.iterdir()) == sorted([f"{s}.parquet" for s in SECTIONS] + ["manifest.json"])
    assert not (tmp_path / "out.work").exists()  # the work dir (a sibling, created by the build) is gone
    con = duckdb.connect()
    kv = _kv(con, out / "gene.parquet")
    full_sha = _fixture_inputs().full.expected_sha256
    assert kv[KV_PREFIX + "source_bundle_sha256"] == full_sha
    assert (kv[KV_PREFIX + "source_release"], kv[KV_PREFIX + "source_deltas"]) == ("", "[]")
    assert kv[KV_PREFIX + "table_spec_version"] == "9.9"
    gene = dict(con.execute(f"SELECT id, data FROM read_parquet('{out / 'gene.parquet'}')").fetchall())
    s = fixture_sections()
    assert gene[json.loads(s["gene"][0][0])] == s["gene"][0][1].decode()
    assert len(gene) == COUNTS["gene"]
    m = json.loads((out / "manifest.json").read_text())
    assert m["label"] == "derived from the bundle; may be regenerated" and m["derived"] is True
    assert m["source_release"] is None and m["release"] is None  # the producer's to fill
    assert m["build_date"] == m["ended_at"][:10]
    assert m["schema_version"] == m["bundle_schema_version"] == "1.1.0"
    assert m["sections"] == {s: COUNTS.get(s, 0) for s in sorted(SECTIONS)}
    assert m["tables"]["gene"] == {
        "file": "gene.parquet",
        "rows": COUNTS["gene"],
        "sha256": _sha(out / "gene.parquet"),
        "size": (out / "gene.parquet").stat().st_size,
    }
    assert m["inputs"]["full"]["sha256"] == full_sha and list(m["inputs"]) == ["full"]
    assert "source_deltas" not in m and "release" not in m["inputs"]["full"]
    for k in ("generated_at", "started_at", "tool_version", "duckdb_version", "note"):
        assert m[k]
    # the fields ClinVar-GKM's r2_layout.build_index reads (its own parquet-manifest shape)
    assert m["builder"] == f"clinvar-gkm-parquet {m['tool_version']}" and m["dataset"] is None
    assert m["files"] == [
        {"name": n, "size": (out / n).stat().st_size, "sha256": _sha(out / n)}
        for n in sorted(f"{s}.parquet" for s in SECTIONS)
    ]


def test_build_refuses_an_output_dir_holding_a_set(passthrough, tmp_path, monkeypatch):
    out = tmp_path / "out"
    build(_fixture_inputs(), out, spec=passthrough, tables=["gene"])
    calls = []
    monkeypatch.setattr(build_mod.bundle, "stream_bundle", lambda *a: calls.append(a))
    with pytest.raises(InputError, match="already holds .*manifest.json"):
        build(_fixture_inputs(), out, spec=passthrough, tables=["submitter"])
    assert calls == []  # refused before anything was read


def test_failed_build_leaves_no_partial_set(passthrough, tmp_path, monkeypatch):
    def render(t):  # rcv is the last section: every other table has been written when it fails
        return _failing_sql(t) if t.name == "rcv" else _passthrough_sql(t)

    monkeypatch.setattr(build_mod, "render_table", render)
    out = tmp_path / "out"
    with pytest.raises(SpecError, match="table rcv: query failed.*boom"):
        build(_fixture_inputs(), out, spec=passthrough)
    assert list(out.iterdir()) == []
    assert not (tmp_path / "out.work").exists()


def test_build_keep_work_keeps_staging(passthrough, tmp_path):
    work = tmp_path / "w"
    build(_fixture_inputs(), tmp_path / "out", spec=passthrough, work_dir=work, keep_work=True)
    assert set(_stage_dirs(work)) == {s for s in SECTIONS if COUNTS.get(s)}


def test_build_stages_a_section_at_a_time(passthrough, tmp_path, monkeypatch):
    """While a section's tables are built, the chunks of the sections before it are gone and at most
    QUEUE_SIZE + 1 later sections are staged (queued, or being read); every DuckDB connection is closed."""
    work = tmp_path / "w"
    work.mkdir()
    seen: dict[str, dict[str, int]] = {}
    real = build_mod.build_tables

    def spy(con, spec_, names, out_dir, kv, **kw):
        (section,) = {spec_.table(n).sections[0] for n in names}
        seen[section] = _stage_dirs(work)
        return real(con, spec_, names, out_dir, kv, **kw)

    monkeypatch.setattr(build_mod, "build_tables", spy)
    build(_fixture_inputs(), tmp_path / "out", spec=passthrough, work_dir=work)
    assert list(seen) == list(SECTIONS)  # each section once, in bundle order
    order = {s: i for i, s in enumerate(SECTIONS)}
    for section, staged in seen.items():
        assert all(order[s] >= order[section] for s in staged), (section, staged)
        assert len([s for s in staged if s != section]) <= build_mod.QUEUE_SIZE + 1, (section, staged)
        if COUNTS.get(section):
            assert section in staged  # its chunks stay until its tables are built (the view reads them)
    assert work.exists() and not list(work.iterdir())  # a given work dir stays, emptied


def test_small_chunks_give_the_same_files(passthrough, tmp_path, monkeypatch):
    a = build(_fixture_inputs(), tmp_path / "a", spec=passthrough)
    monkeypatch.setattr(build_mod, "CHUNK_BYTES", 2048)
    monkeypatch.setattr(build_mod.bundle, "_WRITE_CHUNK", 512)
    work = tmp_path / "w"
    b = build(_fixture_inputs(), tmp_path / "b", spec=passthrough, work_dir=work, keep_work=True)
    assert max(_stage_dirs(work).values()) > 3  # several chunks per section
    assert [(t.table, t.sha256) for t in a.tables] == [(t.table, t.sha256) for t in b.tables]


def test_tables_subset_stages_only_the_sections_it_reads(passthrough, tmp_path):
    work = tmp_path / "w"
    res = build(
        _fixture_inputs(),
        tmp_path / "out",
        spec=passthrough,
        tables=["scv", "gene"],
        work_dir=work,
        keep_work=True,
    )
    assert [t.table for t in res.tables] == ["scv", "gene"]
    assert set(_stage_dirs(work)) == {"gene", "scv"}
    m = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert m["sections"] == {s: COUNTS.get(s, 0) for s in sorted(SECTIONS)}  # every section read and counted


def test_tables_subset_still_checks_every_line(passthrough, tmp_path):
    text = gzip.decompress(FIXTURE.read_bytes()) + b"\n"  # trailing garbage after the last section
    path = write_gz(tmp_path / "bad.json.gz", text)
    with pytest.raises(BundleFormatError, match="trailing data"):
        build(
            _fixture_inputs(full=local_input(path)),
            tmp_path / "out",
            spec=passthrough,
            tables=["sequenceReference"],
        )
    assert list((tmp_path / "out").iterdir()) == []


def test_worker_failure_stops_the_reader(passthrough, tmp_path, monkeypatch):
    closed = []
    real_close = build_mod.SectionPipeline._close

    def close(self, section):
        closed.append(section)
        real_close(self, section)

    def render(t):
        return _failing_sql(t) if t.name == "sequenceReference" else _passthrough_sql(t)

    monkeypatch.setattr(build_mod.SectionPipeline, "_close", close)
    monkeypatch.setattr(build_mod, "render_table", render)
    with pytest.raises(SpecError, match="table sequenceReference: query failed.*boom"):
        build(_fixture_inputs(), tmp_path / "out", spec=passthrough)
    assert len(closed) <= build_mod.QUEUE_SIZE + 2 < len(SECTIONS)  # the reader stopped early
    assert list((tmp_path / "out").iterdir()) == []
    assert not any(t.name == "clinvar-gkm-parquet-worker" for t in threading.enumerate())


def test_reader_failure_stops_the_worker(passthrough, tmp_path):
    text = gzip.decompress(FIXTURE.read_bytes()).replace(b"\n}\n", b"\n}\njunk\n")
    path = write_gz(tmp_path / "bad.json.gz", text)
    with pytest.raises(BundleFormatError, match="trailing data"):
        build(_fixture_inputs(full=local_input(path)), tmp_path / "out", spec=passthrough)
    assert list((tmp_path / "out").iterdir()) == []  # the tables built before the error are removed
    assert not (tmp_path / "out.work").exists()
    assert not any(t.name == "clinvar-gkm-parquet-worker" for t in threading.enumerate())


def test_checksum_mismatch_found_at_the_end_removes_the_tables(passthrough, tmp_path):
    full = local_input(FIXTURE, sha="0" * 64)
    with pytest.raises(InputError, match="sha256 mismatch"):
        build(_fixture_inputs(full=full), tmp_path / "out", spec=passthrough)
    assert list((tmp_path / "out").iterdir()) == []


def test_worker_failure_with_a_wrong_checksum_reports_the_mismatch(passthrough, tmp_path, monkeypatch):
    monkeypatch.setattr(build_mod, "render_table", _failing_sql)
    full = local_input(FIXTURE, sha="0" * 64)
    with pytest.raises(InputError, match="sha256 mismatch.*also fails"):
        build(_fixture_inputs(full=full), tmp_path / "out", spec=passthrough)


def test_full_without_a_checksum_is_hashed_first(passthrough, tmp_path):
    path = tmp_path / "in" / "clinvar-gkm_copy.json.gz"
    path.parent.mkdir()
    path.write_bytes(FIXTURE.read_bytes())  # no sidecar
    logs = []
    build(
        _fixture_inputs(full=local_input(path)),
        tmp_path / "out",
        spec=passthrough,
        tables=["gene"],
        log=logs.append,
    )
    assert any("hashing clinvar-gkm_copy.json.gz first" in m for m in logs)
    kv = _kv(duckdb.connect(), tmp_path / "out" / "gene.parquet")
    assert kv[KV_PREFIX + "source_bundle_sha256"] == _sha(FIXTURE)


def test_build_refuses_sections_out_of_order(passthrough, tmp_path):
    s = fixture_sections()
    path = write_gz(tmp_path / "b.json.gz", bundle_bytes({"gene": s["gene"], "location": s["location"]}))
    with pytest.raises(BundleFormatError, match="section location comes after gene"):
        build(_fixture_inputs(full=local_input(path)), tmp_path / "out", spec=passthrough)


# --- CLI mapping -----------------------------------------------------------------------------------------------


def _args(**kw):
    base = dict(
        full=None,
        sha256=None,
        bundle_schema=None,
        column_set=None,
        out=Path("out"),
        tables=None,
        work_dir=None,
        keep_work=False,
        memory_limit=None,
        threads=None,
    )
    base.update(kw)
    return Namespace(**base)


def test_resolve_build_inputs(monkeypatch):
    seen = []

    def resolve_input(source, *, sha256=None):
        seen.append((source, sha256))
        return "FULL"

    monkeypatch.setattr(build_mod.sources, "resolve_input", resolve_input)
    assert resolve_build_inputs(_args(full="f/bundle.json.gz", sha256="ab")) == BuildInputs(full="FULL")
    assert seen == [("f/bundle.json.gz", "ab")]
    with pytest.raises(InputError, match="--full is required"):
        resolve_build_inputs(_args())


def test_run_build_command(monkeypatch, capsys, tmp_path):
    """The CLI compiles the sections spec from --bundle-schema and --column-set (ADR 0006, ADR 0009)."""
    monkeypatch.setattr(build_mod, "resolve_build_inputs", lambda args: _fixture_inputs())
    args = _args(
        full="x",
        out=tmp_path / "o",
        tables="gene, submitter,allele",
        bundle_schema=str(CLINVAR_SCHEMA),
        column_set=str(CLINVAR_COLUMNS),
    )
    assert run_build_command(args) == 0
    out = capsys.readouterr().out
    n = COUNTS["gene"] + COUNTS["submitter"] + COUNTS["allele"]
    assert f"built 3 table(s) of spec sections 2.0, {n} rows" in out
    kv = _kv(duckdb.connect(), tmp_path / "o" / "gene.parquet")
    assert kv[KV_PREFIX + "table_spec"] == "sections" and KV_PREFIX + "selection" not in kv
    assert kv[KV_PREFIX + "table_spec_sha256"] == SPEC_SOURCE.sha256
    with pytest.raises(InputError, match="--bundle-schema and --column-set are required"):
        run_build_command(_args(full="x", out=tmp_path / "o2", bundle_schema=str(CLINVAR_SCHEMA)))


def test_build_api_needs_a_spec(tmp_path):
    """build() has no default spec: the caller compiles one (compiled.compile_pairing)."""
    with pytest.raises(TypeError, match="spec"):
        build(_fixture_inputs(), tmp_path / "o", tables=["gene"])
    res = build(_fixture_inputs(), tmp_path / "o", tables=["gene"], spec=SPEC, spec_source=SPEC_SOURCE)
    assert [t.table for t in res.tables] == ["gene"]
    kv = _kv(duckdb.connect(), tmp_path / "o" / "gene.parquet")
    assert (kv[KV_PREFIX + "table_spec"], kv[KV_PREFIX + "table_spec_version"]) == ("sections", "2.0")
    assert KV_PREFIX + "table_spec_builtin" not in kv


# --- end to end on the real fixture ----------------------------------------------------------------------------


def test_build_and_verify_real_fixture(passthrough, tmp_path):
    """The pass-through build on the fixture is deterministic across threads; verify and the streaming bundle
    recompute pass."""
    from clinvar_gkm_parquet.verify import bundle_recompute, recompute_failed, verify_dir

    spec = passthrough
    a = build(_fixture_inputs(), tmp_path / "a", spec=spec, threads=1)
    b = build(_fixture_inputs(), tmp_path / "b", spec=spec, threads=4, work_dir=tmp_path / "w")
    assert [(t.table, t.sha256) for t in a.tables] == [(t.table, t.sha256) for t in b.tables]
    rows = {t.table: t.rows for t in a.tables}
    assert rows == {s: COUNTS.get(s, 0) for s in rows}
    assert verify_dir(tmp_path / "a", spec) == []
    m = json.loads((tmp_path / "a" / "manifest.json").read_text())
    assert m["source_bundle_sha256"] == _fixture_inputs().full.expected_sha256
    assert m["source_release"] is None and "source_deltas" not in m
    groups = bundle_recompute(tmp_path / "a", _fixture_inputs(), spec=spec, work_dir=tmp_path / "rc")
    assert len(groups) == len(SECTIONS) and not any(recompute_failed(g) for g in groups)
    assert not (tmp_path / "rc").exists() or not list((tmp_path / "rc").iterdir())
