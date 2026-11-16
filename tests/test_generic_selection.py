"""Selection on a pairing (ADR 0008 decision 4.1, ADR 0009 decision 5): `select` and `build --driver` take
`--bundle-schema` and `--column-set`, and read the bundle's root by the pairing's rules (its version property, if
any, and its sections); since 2.0.0 a pairing is required (ADR 0009).

Two real producers: the ClinVar-GKM excerpt with its 1.1.0 schema set and column set (a fixture) as the pairing,
whose selection is records of the bundle byte for byte; and the CIViC bundles (tests/fixtures/civic/), which have
no version, a pretty-printed layout, empty collections and root values. Expectations come from the source files
parsed with stdlib json, not from the tool's reader.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import duckdb
import pytest
from _fixture import FIXTURE, FIXTURES
from _fixture import PAIRING as CLINVAR_PAIRING

from gkm_bundle_parquet.build import KV_PREFIX
from gkm_bundle_parquet.cli import main

CIVIC = FIXTURES / "civic"
CIVIC_PAIRING = ("--bundle-schema", CIVIC / "schema-set", "--column-set", CIVIC / "civic.columns.yaml")
CIVIC_BUNDLES = sorted(CIVIC.glob("civic-assertion-*-bundle.json"))


def cli(capsys, *args) -> tuple[int, str, str]:
    rc = main([str(a) for a in args])
    got = capsys.readouterr()
    return rc, got.out, got.err


def selection_of(stdout: str) -> dict:
    (line,) = [x for x in stdout.splitlines() if x.startswith("selection: ")]
    return json.loads(line.removeprefix("selection: "))


def kv_of(path: Path) -> dict[str, str]:
    rows = duckdb.connect().execute(f"SELECT key, value FROM parquet_kv_metadata('{path}')").fetchall()
    return {k.decode()[len(KV_PREFIX) :]: v.decode() for k, v in rows if k.decode().startswith(KV_PREFIX)}


# --- ClinVar-GKM, by its pairing --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "opts",
    [
        ["--driver", "allele", "--where", "name LIKE 'NC_000002%'"],
        [
            "--driver",
            "submitter",
            "--follow",
            "in",
            "--max-depth",
            "1",
            "--sections",
            "submitter,scv,evidenceLine",
        ],
    ],
    ids=["out", "in"],
)
def test_a_pairing_selects_records_of_the_bundle_byte_for_byte(tmp_path, capsys, opts):
    rc, paired_out, err = cli(
        capsys, "select", "--full", FIXTURE, *CLINVAR_PAIRING, "--out", tmp_path / "p.json.gz", *opts
    )
    assert rc == 0, err
    text = gzip.decompress((tmp_path / "p.json.gz").read_bytes())
    assert text.startswith(b'{\n  "schema_version": "1.1.0",\n')

    def records(data: bytes) -> set[bytes]:
        return {ln.rstrip(b",") for ln in data.split(b"\n") if ln.startswith(b"    ")}

    picked = records(text)
    assert picked and picked <= records(gzip.decompress(FIXTURE.read_bytes()))
    assert selection_of(paired_out)["selected_records"] == len(picked)


def test_build_driver_takes_the_pairing(tmp_path, capsys):
    opts = ["--driver", "allele", "--where", "name LIKE 'NC_000002%'"]
    rc, stdout, err = cli(capsys, "build", "--full", FIXTURE, *CLINVAR_PAIRING, "--out", tmp_path / "b", *opts)
    assert rc == 0, err
    rc, sel_out, err = cli(
        capsys, "select", "--full", FIXTURE, *CLINVAR_PAIRING, "--out", tmp_path / "s.json.gz", *opts
    )
    assert rc == 0, err
    kv = kv_of(tmp_path / "b" / "allele.parquet")
    assert json.loads(kv["selection"]) == selection_of(sel_out)
    assert kv["bundle_schema_version"] == "1.1.0"
    rc, stdout, err = cli(capsys, "verify", tmp_path / "b", "--bundle-recompute", "--full", tmp_path / "s.json.gz")
    assert rc == 0, stdout + err


# --- CIViC: no version, pretty-printed, empty collections, root values -------------------------------------------


@pytest.mark.parametrize("source", CIVIC_BUNDLES, ids=lambda p: p.stem)
def test_civic_selection_has_no_version_and_keeps_its_root_values(tmp_path, capsys, source):
    out = tmp_path / "s.json.gz"
    rc, stdout, err = cli(
        capsys, "select", "--full", source, *CIVIC_PAIRING, "--driver", "assertion", "--out", out
    )
    assert rc == 0, err
    doc = json.loads(source.read_text())
    text = gzip.decompress(out.read_bytes())
    mini = json.loads(text)
    assert "schema_version" not in mini and text.startswith(b'{\n  "')
    # The root values, verbatim and last; every selected record equal to its source; the driver records all there.
    values = [k for k, v in doc.items() if k in ("metadata", "failedAssertionIds")]
    assert list(mini)[-len(values) :] == values
    assert all(mini[k] == doc[k] for k in values)
    sections = [k for k in mini if k not in values]
    assert sections and all(mini[s][key] == doc[s][key] for s in sections for key in mini[s])
    assert mini["assertion"] == doc["assertion"]
    selection = selection_of(stdout)
    assert selection["selected_records"] == sum(len(mini[s]) for s in sections)
    # Every record is on its own line: the mini-bundle is in the line layout.
    assert sum(1 for line in text.splitlines() if line.startswith(b'    "')) == selection["selected_records"]


@pytest.mark.parametrize("source", CIVIC_BUNDLES, ids=lambda p: p.stem)
def test_civic_build_driver_builds_and_verifies_against_the_mini_bundle(tmp_path, capsys, source):
    mini = tmp_path / "s.json.gz"
    opts = ["--driver", "assertion"]
    rc, sel_out, err = cli(capsys, "select", "--full", source, *CIVIC_PAIRING, "--out", mini, *opts)
    assert rc == 0, err
    rc, _, err = cli(capsys, "build", "--full", source, *CIVIC_PAIRING, "--out", tmp_path / "b", *opts)
    assert rc == 0, err
    kv = kv_of(tmp_path / "b" / "assertion.parquet")
    assert json.loads(kv["selection"]) == selection_of(sel_out)
    assert kv["bundle_schema_version"] == ""
    assert (tmp_path / "b" / "root_values.parquet").is_file()
    rc, stdout, err = cli(capsys, "verify", tmp_path / "b", "--bundle-recompute", "--full", mini)
    assert rc == 0, stdout + err


def test_a_selection_without_a_pairing_is_refused(tmp_path, capsys):
    rc, _, err = cli(
        capsys, "select", "--full", CIVIC_BUNDLES[0], "--driver", "assertion", "--out", tmp_path / "s.json.gz"
    )
    assert rc == 2 and "--bundle-schema and --column-set are required" in err
    assert not (tmp_path / "s.json.gz").exists()
