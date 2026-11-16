"""Sets written by 1.x stay checkable and rebuildable (ADR 0009 decision 3): their files carry the old key-value
prefix `clinvar_gkm_parquet.`, and `verify` and `rebuild` read it as they read `gkm_bundle_parquet.`. The set is
the one tool 1.1.0 wrote (tests/fixtures/sets-1.1.0/README.md); it is copied before each use, so nothing is
written beside it."""

from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path

import duckdb
import pytest
from _fixture import FIXTURE, FIXTURES

from gkm_bundle_parquet.cli import main
from gkm_bundle_parquet.compat import KV_PREFIX, LEGACY_KV_PREFIX

LEGACY = FIXTURES / "sets-1.1.0" / "clinvar-gkm-excerpt"


@pytest.fixture
def legacy(tmp_path) -> Path:
    d = tmp_path / "legacy"
    shutil.copytree(LEGACY, d)
    return d


def cli(capsys, *args) -> tuple[int, str, str]:
    rc = main([str(a) for a in args])
    got = capsys.readouterr()
    return rc, got.out, got.err


def test_the_fixture_is_a_1x_set():
    keys = {
        k.decode()
        for (k,) in duckdb.connect()
        .execute(f"SELECT key FROM parquet_kv_metadata('{LEGACY / 'gene.parquet'}')")
        .fetchall()
    }
    assert f"{LEGACY_KV_PREFIX}tool_version" in keys and not any(k.startswith(KV_PREFIX) for k in keys)
    assert json.loads((LEGACY / "manifest.json").read_text())["builder"] == "clinvar-gkm-parquet 1.1.0"


def test_verify_reads_a_1x_set(legacy, capsys):
    rc, out, err = cli(capsys, "verify", legacy)
    assert rc == 0 and "directory check: 0 problem(s)" in out, out + err


def test_verify_bundle_recompute_reads_a_1x_set(legacy, tmp_path, capsys):
    rc, out, err = cli(
        capsys, "verify", legacy, "--bundle-recompute", "--full", FIXTURE, "--work-dir", tmp_path / "w"
    )
    assert rc == 0 and "bundle recompute: 0 group(s) differ" in out, out + err


def test_verify_roundtrip_reads_a_1x_set(legacy, tmp_path, capsys):
    rc, out, err = cli(capsys, "verify", legacy, "--roundtrip", "--full", FIXTURE, "--work-dir", tmp_path)
    assert rc == 0 and "round trip: identical" in out, out + err


def test_rebuild_writes_a_1x_set_back_byte_for_byte(legacy, tmp_path, capsys):
    rebuilt = tmp_path / "rebuilt.json"
    rc, out, err = cli(capsys, "rebuild", legacy, "-o", rebuilt, "--work-dir", tmp_path)
    assert rc == 0, err
    assert rebuilt.read_bytes() == gzip.decompress(FIXTURE.read_bytes())
