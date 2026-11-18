"""`version`: the compatibility data names no producer's schema_version (ADR 0009, row 0002), and the commands
that read a bundle stop without its schema."""

import json

import pytest

from gkm_bundle_parquet.cli import main
from gkm_bundle_parquet.compiler import RULES_VERSION


def test_version_json(capsys):
    assert main(["version", "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert (info["table_spec"], info["table_spec_version"]) == ("sections", RULES_VERSION)
    assert "no producer's schema_version is built in" in info["schema_version_rule"]
    for gone in ("snapshotted_schema_versions", "accepted_schema_versions", "table_specs", "tables"):
        assert gone not in info
    assert info["policy_version"] == RULES_VERSION
    assert "--column-set" in info["schema_version_rule"] and "generated default" in info["schema_version_rule"]
    assert info["kv_prefix"] == "gkm_bundle_parquet."
    assert info["kv_prefixes_read"] == ["gkm_bundle_parquet.", "clinvar_gkm_parquet."]
    assert info["duckdb_version"] == "1.5.4"
    assert set(info["validate_engines"]) == {"rs", "python"}


def test_version_text(capsys):
    assert main(["version"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("gkm-bundle-parquet ") and f"table spec sections {RULES_VERSION}" in out
    assert f"policy {RULES_VERSION}; duckdb 1.5.4" in out
    assert "key-value prefix gkm_bundle_parquet. (reads also clinvar_gkm_parquet.)" in out


@pytest.mark.parametrize(
    "argv",
    [
        ["build", "--full", "b.json.gz", "--out", "o"],
        ["select", "--full", "b.json.gz", "--out", "o.json.gz", "--driver", "t", "--column-set", "c.yaml"],
        ["compile-spec"],
        ["generate"],
    ],
)
def test_no_schema_is_a_clear_error(capsys, argv):
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert "--bundle-schema is required" in err and "no default bundle" in err


def test_validate_needs_the_schema(capsys):
    assert main(["validate", "b.json.gz"]) == 2
    assert "--bundle-schema is required" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["build", "--full", "b", "--out", "o", "--release", "latest"],
        ["build", "--full", "b", "--out", "o", "--delta", "d"],
        ["build", "--full", "b", "--out", "o", "--spec", "baseline"],
        ["verify", "d", "--against", "r"],
        ["verify", "d", "--index", "i"],
        ["compile-spec", "1.1.0"],
        ["validate", "b", "--index", "i"],
    ],
)
def test_removed_options_are_refused(capsys, argv):
    with pytest.raises(SystemExit) as e:
        main(argv)
    assert e.value.code == 2 and "unrecognized arguments" in capsys.readouterr().err
