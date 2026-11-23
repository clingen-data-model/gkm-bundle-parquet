"""`apply` (ADR 0011): a built set brought up to date from an upserts bundle and a deletes file equals, table for
table, a build of the replayed bundle, and records that bundle's records sha256s, so `verify` and `rebuild` check
it.

The data are real: an excerpt of two consecutive ClinVar-GKM 1.2.0 weekly deltas and the deletes the second one
lists (tests/fixtures/real-1.2.0-apply/), and CIViC's two bundles (tests/fixtures/civic/). The replayed bundle, the
golden side, is made here with the standard library's json from the source files, not by the code under test.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import duckdb
import pytest
from _fixture import FIXTURES
from _input_helpers import independent_records_sha256

from gkm_bundle_parquet.build import KV_PREFIX
from gkm_bundle_parquet.cli import main
from gkm_bundle_parquet.compiled import compile_default, compile_pairing

APPLY = FIXTURES / "real-1.2.0-apply"
BASE, UPSERTS, DELETES = APPLY / "base.json.gz", APPLY / "upserts.json.gz", APPLY / "deletes.json"
SCHEMA_12 = FIXTURES / "clinvar-gkm" / "1.2.0" / "schema-set"
P12 = ["--bundle-schema", SCHEMA_12, "--version-property", "schema_version"]
SCHEMA_11 = FIXTURES / "clinvar-gkm" / "1.1.0" / "schema-set"
REAL_11 = FIXTURES / "real-1.1.0" / "clinvar-gkm_excerpt.json.gz"
CIVIC = FIXTURES / "civic"
CIVIC_9, CIVIC_251 = CIVIC / "civic-assertion-9-bundle.json", CIVIC / "civic-assertion-251-bundle.json"
PC = ["--bundle-schema", CIVIC / "schema-set"]


def cli(capsys, *args) -> tuple[int, str, str]:
    rc = main([str(a) for a in args])
    got = capsys.readouterr()
    return rc, got.out, got.err


def load(path: Path) -> dict:
    data = path.read_bytes()
    return json.loads(gzip.decompress(data) if path.suffix == ".gz" else data)


def replay(base: dict, upserts: dict, deletes: dict, sections: set[str]) -> dict:
    """The replayed bundle (ADR 0011): per section, the base's records less the deleted and the upserted keys, plus
    the upserts; a root value replaced by the upserts bundle's of the same name; root members in the base's order,
    then the ones the upserts bundle adds."""
    out = {}
    for name, value in base.items():
        if name in sections:
            removed = set(deletes.get(name, ())) | set(upserts.get(name, {}))
            out[name] = {k: r for k, r in value.items() if k not in removed} | upserts.get(name, {})
        else:
            out[name] = upserts.get(name, value)
    for name, value in upserts.items():
        out.setdefault(name, value)
    return out


def kv_of(path: Path) -> dict[str, str]:
    rows = duckdb.connect().execute(f"SELECT key, value FROM parquet_kv_metadata('{path}')").fetchall()
    return {k.decode()[len(KV_PREFIX) :]: v.decode() for k, v in rows if k.decode().startswith(KV_PREFIX)}


def manifest(d: Path) -> dict:
    return json.loads((d / "manifest.json").read_text())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def table_differences(a: Path, b: Path) -> list[str]:
    """Every table of the two sets: the same columns and types, and the same rows as multisets (order ignored)."""
    names = sorted(p.name for p in a.glob("*.parquet"))
    out = [] if names == sorted(p.name for p in b.glob("*.parquet")) else [f"files {names}"]
    con = duckdb.connect()
    for n in names:
        ra, rb = f"read_parquet('{a / n}')", f"read_parquet('{b / n}')"
        if (
            con.execute(f"DESCRIBE SELECT * FROM {ra}").fetchall()
            != con.execute(f"DESCRIBE SELECT * FROM {rb}").fetchall()
        ):
            out.append(f"{n}: columns")
            continue
        for x, y in ((ra, rb), (rb, ra)):
            extra = con.execute(
                f"SELECT count(*) FROM (SELECT * FROM {x} EXCEPT ALL SELECT * FROM {y})"
            ).fetchone()[0]
            if extra:
                out.append(f"{n}: {extra} row(s) of one set not in the other")
    return out


def spec_sections(schema: Path, version_property: str | None) -> set[str]:
    spec, _ = compile_default(str(schema), version_property)
    return {t.sections[0] for t in spec.tables}


# --- ClinVar-GKM 1.2.0: two real deltas --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def clinvar(tmp_path_factory):
    """(base set, applied set, build of the replayed bundle, the replayed bundle's path and doc)."""
    tmp = tmp_path_factory.mktemp("apply")
    base, applied, replayed = tmp / "base", tmp / "applied", tmp / "replayed"
    assert main(["build", "--full", str(BASE), *map(str, P12), "--out", str(base)]) == 0
    argv = ["apply", str(base), "--upserts", str(UPSERTS), "--deletes", str(DELETES), *map(str, P12)]
    assert main([*argv, "--out", str(applied)]) == 0
    doc = replay(load(BASE), load(UPSERTS), load(DELETES), spec_sections(SCHEMA_12, "schema_version"))
    path = tmp / "replayed.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert main(["build", "--full", str(path), *map(str, P12), "--out", str(replayed)]) == 0
    return base, applied, replayed, path, doc


def test_apply_equals_a_build_of_the_replayed_bundle(clinvar):
    _, applied, replayed, _, doc = clinvar
    assert table_differences(applied, replayed) == []
    got, want = manifest(applied), manifest(replayed)
    assert got["records_sha256"] == want["records_sha256"]
    assert got["records_sha256"] == independent_records_sha256(doc, spec_sections(SCHEMA_12, "schema_version"))
    # Root order is not content (ADR 0008 decision 1.2): the general reader may list the members in another order.
    assert got["sections"] == want["sections"] and sorted(got["source_root"]) == sorted(want["source_root"])


def test_apply_covers_every_kind_of_change(clinvar):
    """The fixture replaces, deletes, adds and keeps records, adds a root member and empties a section."""
    base, applied, _, _, _ = clinvar
    b, a = manifest(base), manifest(applied)
    assert "condition" not in b["source_root"] and a["source_root"][-1] == "condition"
    assert b["sections"]["conditionSet"] == 1 and a["sections"]["conditionSet"] == 0
    assert "conditionSet" in a["source_root"]
    up, dels = load(UPSERTS), load(DELETES)
    for s in ("scv", "variation", "propositionPathogenicity"):
        kept = b["sections"][s] - len(dels.get(s, [])) - len(set(load(BASE)[s]) & set(up[s]))
        assert a["sections"][s] == kept + len(up[s])
    # copyNumberCount: neither input touches it, so its file keeps the base's records sha256
    assert (
        kv_of(applied / "copyNumberCount.parquet")["section_records_sha256"]
        == kv_of(base / "copyNumberCount.parquet")["section_records_sha256"]
    )


def test_applied_set_verifies_and_rebuilds_as_the_replayed_bundle(clinvar, tmp_path, capsys):
    _, applied, _, path, doc = clinvar
    rc, out, _ = cli(capsys, "verify", applied, "--roundtrip", *P12)
    assert rc == 0 and "round trip: identical" in out
    rc, out, _ = cli(capsys, "verify", applied, "--roundtrip", "--full", path)
    assert rc == 0, out
    rc, out, _ = cli(capsys, "verify", applied, "--bundle-recompute", "--full", path, *P12)
    assert rc == 0, out
    rc, out, _ = cli(capsys, "rebuild", applied, "-o", tmp_path / "r.json")
    assert rc == 0 and "record-identical" in out
    sections = spec_sections(SCHEMA_12, "schema_version")
    assert independent_records_sha256(load(tmp_path / "r.json"), sections) == independent_records_sha256(
        doc, sections
    )


def test_applied_set_records_its_provenance(clinvar):
    base, applied, _, _, _ = clinvar
    b, a = manifest(base), manifest(applied)
    step = {
        "base_records_sha256": b["records_sha256"],
        "base_source_bundle_sha256": sha256(BASE),
        "upserts_sha256": sha256(UPSERTS),
        "upserts_records_sha256": a["inputs"]["upserts"]["records_sha256"],
        "deletes_sha256": sha256(DELETES),
        "records_sha256": a["records_sha256"],
    }
    assert a["applied"] == [step]
    assert a["source_bundle_sha256"] == ""
    assert a["inputs"]["base"]["manifest_sha256"] == sha256(base / "manifest.json")
    assert a["inputs"]["base"]["files"] == b["files"]
    assert a["inputs"]["deletes"]["sections"] == {s: len(k) for s, k in sorted(load(DELETES).items())}
    for f in applied.glob("*.parquet"):
        kv = kv_of(f)
        assert json.loads(kv["applied"]) == [step] and kv["source_bundle_sha256"] == ""


def test_apply_is_deterministic(clinvar, tmp_path):
    base, applied, _, _, _ = clinvar
    argv = ["apply", base, "--upserts", UPSERTS, "--deletes", DELETES, *P12, "--out", tmp_path / "again"]
    assert main([str(a) for a in argv]) == 0
    assert manifest(tmp_path / "again")["files"] == manifest(applied)["files"]


# --- CIViC: another producer, root values, a chain --------------------------------------------------------------


def test_civic_apply_twice_equals_two_replays(tmp_path, capsys):
    """CIViC's bundles have no version, a pretty-printed layout and root values. Apply 251 to 9, then 9 back over
    that, each with deletes of real keys; each step equals a build of its replay, and the chain links them."""
    sections = spec_sections(CIVIC / "schema-set", None)
    b9, b251 = load(CIVIC_9), load(CIVIC_251)
    d1 = {"assertion": sorted(b9["assertion"]), "evidence": sorted(b9["evidence"])[:1]}
    d2 = {"proposition": sorted(b251["proposition"])}
    (tmp_path / "d1.json").write_text(json.dumps(d1))
    (tmp_path / "d2.json").write_text(json.dumps(d2))
    assert main(["build", "--full", str(CIVIC_9), *map(str, PC), "--out", str(tmp_path / "base")]) == 0
    steps = [(CIVIC_251, tmp_path / "d1.json", d1), (CIVIC_9, tmp_path / "d2.json", d2)]
    doc, prev = b9, tmp_path / "base"
    for i, (upserts, deletes, d) in enumerate(steps, 1):
        out = tmp_path / f"applied{i}"
        rc, _, err = cli(capsys, "apply", prev, "--upserts", upserts, "--deletes", deletes, *PC, "--out", out)
        assert rc == 0, err
        doc = replay(doc, load(upserts), d, sections)
        path = tmp_path / f"replayed{i}.json"
        path.write_text(json.dumps(doc, indent=2))
        assert main(["build", "--full", str(path), *map(str, PC), "--out", str(tmp_path / f"replayed{i}")]) == 0
        assert table_differences(out, tmp_path / f"replayed{i}") == []
        assert manifest(out)["records_sha256"] == manifest(tmp_path / f"replayed{i}")["records_sha256"]
        rc, stdout, _ = cli(capsys, "verify", out, "--roundtrip", "--full", path)
        assert rc == 0, stdout
        prev = out
    chain = manifest(prev)["applied"]
    assert [s["upserts_sha256"] for s in chain] == [sha256(CIVIC_251), sha256(CIVIC_9)]
    assert chain[1]["base_records_sha256"] == chain[0]["records_sha256"]
    assert chain[0]["base_source_bundle_sha256"] == sha256(CIVIC_9) and chain[1]["base_source_bundle_sha256"] == ""


# --- refusals ---------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def base_12(clinvar):
    return clinvar[0]


def _apply(capsys, base, out, *extra, upserts=UPSERTS, pairing=P12):
    return cli(capsys, "apply", base, "--upserts", upserts, *pairing, "--out", out, *extra)


def test_a_pairing_other_than_the_bases_is_refused(base_12, tmp_path, capsys):
    rc, _, err = _apply(capsys, base_12, tmp_path / "o", pairing=["--bundle-schema", SCHEMA_11])
    assert rc == 2 and "doesn't compile to the spec that wrote" in err and "schema_set_sha256" in err
    assert not (tmp_path / "o").exists()


def test_an_upserts_bundle_of_another_version_is_refused(base_12, tmp_path, capsys):
    """The line-change trap: a bundle of the next schema line starts from the same release date as the last of the
    previous line, but it is not a delta of a set of the other version."""
    rc, _, err = _apply(capsys, base_12, tmp_path / "o", upserts=REAL_11)
    assert rc == 2 and "schema_version 1.1.0, but the base set holds version 1.2.0" in err


@pytest.mark.parametrize(
    ("deletes", "message"),
    [
        ({"scv": ["clinvar.submission:SCV000000000.1"]}, "key(s) not in the base set"),
        ({"no-such-section": ["x"]}, "is not a section of the set's spec"),
        ({"scv": ["a", "a"]}, "listed twice"),
        ({"scv": "a"}, "must be a list of keys"),
        (["scv"], "must be a JSON object"),
    ],
)
def test_a_bad_deletes_file_is_refused_and_leaves_nothing(base_12, tmp_path, capsys, deletes, message):
    f = tmp_path / "deletes.json"
    f.write_text(json.dumps(deletes))
    rc, _, err = _apply(capsys, base_12, tmp_path / "o", "--deletes", f)
    assert rc == 2 and message in err
    assert not list((tmp_path / "o").glob("*")) if (tmp_path / "o").exists() else True


def test_a_selection_or_a_full_output_directory_is_refused(base_12, tmp_path, capsys):
    sel = tmp_path / "sel"
    key = sorted(load(BASE)["scv"])[0]
    assert (
        main(["build", "--full", str(BASE), *map(str, P12), "--out", str(sel), "--driver", "scv", "--ids", key])
        == 0
    )
    rc, _, err = _apply(capsys, sel, tmp_path / "o")
    assert rc == 2 and "a selective build" in err
    rc, _, err = _apply(capsys, base_12, base_12)
    assert rc == 2 and "is not empty" in err


def test_a_projection_applies_with_its_own_column_set(tmp_path, capsys):
    """A projection is applied with its own column set: the result is the projection of the replayed bundle."""
    cols = CIVIC / "civic.columns.yaml"
    pairing = [*PC, "--column-set", cols]
    _, source = compile_pairing(str(CIVIC / "schema-set"), str(cols))
    assert source.column_set_kind == "projection"
    assert main(["build", "--full", str(CIVIC_9), *map(str, pairing), "--out", str(tmp_path / "base")]) == 0
    rc, _, err = _apply(capsys, tmp_path / "base", tmp_path / "o", upserts=CIVIC_251, pairing=pairing)
    assert rc == 0, err
    rc, _, err = _apply(capsys, tmp_path / "base", tmp_path / "o2", upserts=CIVIC_251, pairing=PC)
    assert rc == 2 and "column_set_sha256" in err
    doc = replay(load(CIVIC_9), load(CIVIC_251), {}, spec_sections(CIVIC / "schema-set", None))
    (tmp_path / "r.json").write_text(json.dumps(doc))
    assert (
        main(["build", "--full", str(tmp_path / "r.json"), *map(str, pairing), "--out", str(tmp_path / "r")]) == 0
    )
    assert table_differences(tmp_path / "o", tmp_path / "r") == []
    assert manifest(tmp_path / "o")["records_sha256"] == manifest(tmp_path / "r")["records_sha256"]
