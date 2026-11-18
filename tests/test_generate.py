"""`generate` and the generated default (ADR 0007): the CLI writes a schema's default column set and reports what
the policy settled, `--check` lists a column set's differences, and a build with no column set takes the default.

Offline: the schema sets are test fixtures (ClinVar-GKM's published 1.1.0 set and its narrowed profiles,
tests/fixtures/schema-sets/), and the bundles are real excerpts.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from _fixture import CLINVAR_COLUMNS, CLINVAR_SCHEMA, FIXTURE, FIXTURES

from clinvar_gkm_parquet.build import KV_PREFIX
from clinvar_gkm_parquet.cli import main
from clinvar_gkm_parquet.compiled import compile_default, compile_pairing
from clinvar_gkm_parquet.compiler import RULES_VERSION

NARROWED = FIXTURES / "schema-sets" / "clinvar-gkm-narrowed-a956b5a"
EXCERPT_1031 = FIXTURES / "real-1.1.0-1031" / "clinvar-gkm_excerpt.json.gz"


def cli(capsys, *args) -> tuple[int, str, str]:
    rc = main([str(a) for a in args])
    got = capsys.readouterr()
    return rc, got.out, got.err


def kv_of(path: Path) -> dict[str, str]:
    rows = duckdb.connect().execute(f"SELECT key, value FROM parquet_kv_metadata('{path}')").fetchall()
    return {k.decode()[len(KV_PREFIX) :]: v.decode() for k, v in rows if k.decode().startswith(KV_PREFIX)}


@pytest.fixture(scope="module")
def generated(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("gen") / "default.columns.yaml"
    assert (
        main(
            ["generate", "--bundle-schema", str(NARROWED), "--version-property", "schema_version", "-o", str(path)]
        )
        == 0
    )
    return path


def test_generate_writes_the_default_and_reports_what_the_policy_settled(generated, capsys):
    text = generated.read_text()
    assert text.startswith("# ") and "\nschema: " in text and '\nversion_property: "schema_version"\n' in text
    rc, stdout, stderr = cli(
        capsys, "generate", "--bundle-schema", NARROWED, "--version-property", "schema_version"
    )
    assert rc == 0 and stdout == text  # deterministic, to stdout without -o
    assert "IRI position(s) with no pointer pattern" in stderr and "JSON-text position(s)" in stderr
    assert "gene: primaryCoding.iris[*]" in stderr


def test_the_generated_column_set_complies(generated, capsys):
    rc, stdout, _ = cli(capsys, "generate", "--check", "--bundle-schema", NARROWED, "--column-set", generated)
    assert rc == 0 and f"complies: it compiles to the default (policy {RULES_VERSION})" in stdout
    spec, source = compile_pairing(str(NARROWED), str(generated))
    default_spec, default_source = compile_default(str(NARROWED), "schema_version")
    assert source.sha256 == default_source.sha256 and (source.column_set_kind, source.projection) == (
        "default",
        None,
    )


def test_check_lists_the_differences_of_a_projection(capsys):
    rc, stdout, _ = cli(
        capsys, "generate", "--check", "--bundle-schema", CLINVAR_SCHEMA, "--column-set", CLINVAR_COLUMNS
    )
    assert rc == 1 and "differs from the default" in stdout
    assert "table location: column id_present" not in stdout  # both have it: the class allows an id, not requires
    assert "only in the default" in stdout


def test_generate_usage_errors(capsys):
    assert cli(capsys, "generate", "--bundle-schema", NARROWED, "--check")[0] == 2
    assert cli(capsys, "generate", "--bundle-schema", NARROWED, "--column-set", CLINVAR_COLUMNS)[0] == 2


def test_a_projection_is_labelled_with_its_name():
    _, source = compile_pairing(str(CLINVAR_SCHEMA), str(CLINVAR_COLUMNS))
    assert (source.column_set_kind, source.projection) == ("projection", "clinvar-gkm-producer")


# --- the narrowed profiles: pointer patterns are foreign keys ----------------------------------------------------


@pytest.fixture(scope="module")
def narrowed_build(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("narrowed") / "out"
    argv = ["build", "--bundle-schema", str(NARROWED), "--version-property", "schema_version"]
    assert main([*argv, "--full", str(EXCERPT_1031), "--out", str(out)]) == 0
    return out


def test_the_narrowed_default_declares_its_foreign_keys(narrowed_build):
    spec = json.loads((narrowed_build / "table-spec.json").read_text())
    fks = {t["name"]: t["foreign_keys"] for t in spec["tables"]}
    assert {"position": "subject", "targets": ["variation"], "section": None} in fks["propositionPathogenicity"]
    assert {
        "position": "members[*].id",
        "targets": ["allele", "copyNumberChange", "copyNumberCount"],
        "section": "members[*].section",
    } in fks["variation"]
    assert json.loads(kv_of(narrowed_build / "scv.parquet")["foreign_keys"]) == fks["scv"]
    assert sum(map(len, fks.values())) == 26
    # The narrowed profiles leave no top-level property to JSON text.
    assert not [c["name"] for t in spec["tables"] for c in t["columns"] if c["derivation"]["kind"] == "json-text"]
    kv = kv_of(narrowed_build / "scv.parquet")
    assert (kv["policy_version"], kv["column_set_kind"]) == (RULES_VERSION, "default")


def test_the_narrowed_default_round_trips(narrowed_build, capsys):
    rc, stdout, stderr = cli(capsys, "verify", narrowed_build, "--roundtrip", "--full", EXCERPT_1031)
    assert rc == 0 and "round trip: identical" in stdout, stdout + stderr
    rc, stdout, stderr = cli(
        capsys, "verify", narrowed_build, "--bundle-schema", NARROWED, "--version-property", "schema_version"
    )
    assert rc == 0 and "pairing" not in stdout, stdout + stderr


def test_the_published_default_round_trips(tmp_path, capsys):
    out = tmp_path / "out"
    rc, _, stderr = cli(capsys, "build", "--bundle-schema", CLINVAR_SCHEMA, "--full", FIXTURE, "--out", out)
    assert rc == 0, stderr
    rc, stdout, stderr = cli(capsys, "verify", out, "--roundtrip", "--full", FIXTURE)
    assert rc == 0 and "round trip: identical" in stdout, stdout + stderr
