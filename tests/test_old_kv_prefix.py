"""Sets written under the tool's former name stay checkable and rebuildable (ADR 0009 decision 3): their files
carry the key-value prefix `clinvar_gkm_parquet.`, and `verify` and `rebuild` read it as they read
`gkm_bundle_parquet.`. A build writes only the new prefix.

The old-prefix sets here are the real excerpt's build with every key-value entry moved to the old prefix (and the
manifest's file hashes and builder updated to match): the files as 1.x would have labelled them. One of them is
also given the text sha256s 1.x recorded in place of the records sha256s.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import duckdb
import pytest
from _fixture import FIXTURE, PAIRING

from gkm_bundle_parquet.cli import main
from gkm_bundle_parquet.compat import KV_PREFIX, LEGACY_KV_PREFIX
from gkm_bundle_parquet.roundtrip import rebuild

OLD_BUILDER = "clinvar-gkm-parquet 1.1.0"


def cli(capsys, *args) -> tuple[int, str, str]:
    rc = main([str(a) for a in args])
    got = capsys.readouterr()
    return rc, got.out, got.err


def kv_keys(path: Path) -> list[str]:
    return [
        k.decode()
        for (k,) in duckdb.connect().execute(f"SELECT key FROM parquet_kv_metadata('{path}')").fetchall()
    ]


def relabel(d: Path, *, text_sha256: bool = False, keep_new: tuple[str, ...] = ()) -> None:
    """Move every key-value entry of the set in `d` to the old prefix; with `text_sha256`, as tool 1.x recorded it
    (section_text_sha256 in place of section_records_sha256, no records_sha256 in the manifest). Entries named in
    `keep_new` are also kept under the new prefix. The manifest's file hashes follow the files."""
    con = duckdb.connect()
    for path in sorted(d.glob("*.parquet")):
        kv = con.execute(f"SELECT key, value FROM parquet_kv_metadata('{path}')").fetchall()
        items = []
        for k, v in ((k.decode(), v.decode()) for k, v in kv):
            if k.startswith(KV_PREFIX):
                name = k[len(KV_PREFIX) :]
                if name in keep_new:
                    items.append((k, v))
                if text_sha256 and name == "section_records_sha256":
                    name = "section_text_sha256"
                k = LEGACY_KV_PREFIX + name
            items.append((k, v))
        meta = ", ".join(f"'{k}': '{v.replace(chr(39), chr(39) * 2)}'" for k, v in items)
        tmp = path.with_name(path.name + ".new")
        con.execute(
            f"COPY (SELECT * FROM read_parquet('{path}')) TO '{tmp}' (FORMAT parquet, KV_METADATA {{{meta}}})"
        )
        tmp.replace(path)
    con.close()
    m = json.loads((d / "manifest.json").read_text())
    m["builder"] = OLD_BUILDER
    if text_sha256:
        m.pop("records_sha256")
    for f in m["files"]:
        data = (d / f["name"]).read_bytes()
        f.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        if f["name"].endswith(".parquet"):
            m["tables"][f["name"][: -len(".parquet")]]["sha256"] = f["sha256"]
    (d / "manifest.json").write_text(json.dumps(m))


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("prefix") / "sections"
    assert main(["build", "--full", str(FIXTURE), *PAIRING, "--out", str(out)]) == 0
    return out


@pytest.fixture
def old(built, tmp_path) -> Path:
    d = tmp_path / "old"
    shutil.copytree(built, d)
    relabel(d)
    return d


def test_a_build_writes_only_the_new_prefix(built):
    for path in sorted(built.glob("*.parquet")):
        keys = kv_keys(path)
        assert f"{KV_PREFIX}tool_version" in keys, path.name
        assert not any(k.startswith(LEGACY_KV_PREFIX) for k in keys), path.name


def test_the_relabelled_set_carries_only_the_old_prefix(old):
    for path in sorted(old.glob("*.parquet")):
        keys = kv_keys(path)
        assert f"{LEGACY_KV_PREFIX}tool_version" in keys and not any(k.startswith(KV_PREFIX) for k in keys)


def test_verify_reads_the_old_prefix(old, tmp_path, capsys):
    rc, out, err = cli(
        capsys, "verify", old, "--bundle-recompute", "--roundtrip", "--full", FIXTURE, "--work-dir", tmp_path / "w"
    )
    assert rc == 0, out + err
    assert "directory check: 0 problem(s)" in out and "bundle recompute: 0 group(s) differ" in out, out
    assert "round trip: identical" in out, out


def test_rebuild_reads_the_old_prefix(old, built, tmp_path, capsys):
    want = rebuild(built, None).records_sha256
    got = rebuild(old, tmp_path / "b.json")
    assert got.checked and got.records_sha256 == want
    rc, out, err = cli(capsys, "rebuild", old, "-o", tmp_path / "c.json.gz", "--work-dir", tmp_path / "w")
    assert rc == 0 and want in out, out + err


def test_a_1x_set_with_text_sha256s_reads_under_the_old_prefix(built, tmp_path, capsys):
    d = tmp_path / "text"
    shutil.copytree(built, d)
    relabel(d, text_sha256=True)
    result = rebuild(d, tmp_path / "b.json")
    assert not result.checked and result.records_sha256 == rebuild(built, None).records_sha256
    rc, _, err = cli(capsys, "verify", d, "--roundtrip")
    assert rc == 2 and "no records sha256" in err, err
    rc, out, err = cli(capsys, "verify", d, "--roundtrip", "--full", FIXTURE, "--work-dir", tmp_path / "w")
    assert rc == 0 and "round trip: identical" in out, out + err


def test_a_file_with_both_prefixes_is_refused(built, tmp_path, capsys):
    d = tmp_path / "both"
    shutil.copytree(built, d)
    relabel(d, keep_new=("tool_version",))
    rc, _, err = cli(capsys, "verify", d)
    assert rc == 2 and f"key-value metadata under both {KV_PREFIX} and {LEGACY_KV_PREFIX}" in err, err
    rc, _, err = cli(capsys, "rebuild", d, "-o", tmp_path / "b.json")
    assert rc == 2 and f"key-value metadata under both {KV_PREFIX} and {LEGACY_KV_PREFIX}" in err, err
