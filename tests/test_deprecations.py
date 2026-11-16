"""The 1.1.0 deprecations (ADR 0009 decision 5, ADR 0008 decision 2): what 2.0.0 removes is still there and works
as before, and each use says so in one line on stderr, naming the replacement. Nothing on stdout, no exit code and
no written file changes (the byte-identity check of the release is in the PR; here the stdout and exit codes)."""

from __future__ import annotations

import pytest
from _fixture import FIXTURE, FIXTURES

from gkm_bundle_parquet.cli import _parser, _warn_deprecations, main

PREFIX = "gkm-bundle-parquet: deprecated: "
CIVIC = FIXTURES / "civic"
CIVIC_PAIRING = ["--bundle-schema", str(CIVIC / "schema-set"), "--column-set", str(CIVIC / "civic.columns.yaml")]
CUSTOM_SPEC = str(FIXTURES / "custom-spec" / "tables.yaml")
F = ["--full", "b.json.gz", "--out", "o"]

RELEASE = (
    "--{} is removed in 2.0.0; resolve the release yourself and pass one bundle with --full and --sha256, or a "
    "<file>.sha256 sidecar"
)
VALIDATE_INDEX = (
    "--index is removed in 2.0.0; resolve the release yourself and pass the bundle and --sha256, or a "
    "<file>.sha256 sidecar"
)
SPEC = (
    "--spec {} ({}) is removed in 2.0.0 (ADR 0008); use the default sections spec with --bundle-schema and "
    "--column-set, and a column set for other tables"
)
AGAINST = (
    "verify --against (the oracle comparison) is removed in 2.0.0 (ADR 0008); check a set with verify "
    "--bundle-recompute and verify --roundtrip"
)
COMPILE = (
    "compile-spec <schema_version> (the built-in default bundle) is removed in 2.0.0; use compile-spec "
    "--bundle-schema <schema> --column-set <column set>"
)
DEFAULT = (
    "reading a bundle without --bundle-schema (the built-in default bundle) is removed in 2.0.0; give the "
    "bundle's schema with {}"
)
PAIR = "--bundle-schema and --column-set"


def warnings(capsys, argv: list[str]) -> list[str]:
    """The deprecation lines the parsed arguments give (the name's is printed by main, before parsing)."""
    _warn_deprecations(_parser().parse_args(argv))
    err = capsys.readouterr().err
    assert all(line.startswith(PREFIX) for line in err.splitlines()), err
    return [line.removeprefix(PREFIX) for line in err.splitlines()]


@pytest.mark.parametrize(
    "argv, want",
    [
        (["version"], []),
        (["version", "--json"], []),
        (["build", *F], [DEFAULT.format(PAIR)]),
        (["build", *F, "--spec", "sections"], [DEFAULT.format(PAIR)]),
        (["build", *F, *CIVIC_PAIRING], []),
        (["build", *F, *CIVIC_PAIRING, "--driver", "assertion"], []),
        (["select", *F, *CIVIC_PAIRING, "--driver", "assertion"], []),
        (["select", *F, "--driver", "gene"], [DEFAULT.format(PAIR)]),
        (["build", *F, "--spec", "baseline"], [SPEC.format("baseline", "id, refs and data per section")]),
        (["build", *F, "--spec", "baseline-1.0"], [SPEC.format("baseline-1.0", "id, refs and data per section")]),
        (["build", *F, "--spec", "clinvar-gkm"], [SPEC.format("clinvar-gkm", "the 22-table set")]),
        (["build", *F, "--spec", "clinvar-gkm-1.0"], [SPEC.format("clinvar-gkm-1.0", "the 22-table set")]),
        (
            ["select", *F, "--spec", "clinvar-gkm@1.0", "--driver", "vcv"],
            [SPEC.format("clinvar-gkm@1.0", "the 22-table set")],
        ),
        (["build", *F, "--spec", CUSTOM_SPEC], [SPEC.format(CUSTOM_SPEC, "a tables.yaml")]),
        (["rebuild", "d", "-o", "x.json", "--spec", CUSTOM_SPEC], [SPEC.format(CUSTOM_SPEC, "a tables.yaml")]),
        (["verify", "d", "--spec", "baseline"], [SPEC.format("baseline", "id, refs and data per section")]),
        (["verify", "d", "--against", "ref"], [AGAINST]),
        (["verify", "d"], []),
        (["verify", "d", "--roundtrip", "--full", "b.json.gz"], []),
        (
            ["build", "--release", "latest", "--index", "i.json", "--out", "o"],
            [RELEASE.format("release"), RELEASE.format("index"), DEFAULT.format(PAIR)],
        ),
        (
            ["build", *F, "--delta", "d.json.gz", "--manifest", "m.json", "--full-source-release", "2026-10-18"],
            [
                RELEASE.format("delta"),
                RELEASE.format("manifest"),
                RELEASE.format("full-source-release"),
                DEFAULT.format(PAIR),
            ],
        ),
        (
            ["verify", "d", "--bundle-recompute", "--full", "b.json.gz", "--delta", "d.json.gz", "--index", "i"],
            [RELEASE.format("index"), RELEASE.format("delta")],
        ),
        (["compile-spec", "1.1.0"], [COMPILE]),
        (["compile-spec", *CIVIC_PAIRING], []),
        (["validate", "b.json.gz"], [DEFAULT.format("--bundle-schema")]),
        (["validate", "b.json.gz", "--bundle-schema", "s"], []),
        (["validate", "b.json.gz", "--bundle-schema", "s", "--index", "i"], [VALIDATE_INDEX]),
    ],
)
def test_each_deprecated_use_says_so_once(capsys, argv, want):
    assert warnings(capsys, argv) == want


def test_a_deprecated_build_still_builds_with_the_same_exit_code_and_stdout(tmp_path, capsys):
    """The warnings come before the work and change nothing after it: the default build of the excerpt, with a
    deprecated option, prints what it printed before (the files themselves are compared in the release PR)."""
    out = tmp_path / "o"
    rc = main(["build", "--full", str(FIXTURE), "--full-source-release", "2026-10-31", "--out", str(out)])
    got = capsys.readouterr()
    assert rc == 0
    assert got.out.startswith("built 20 table(s) of spec sections 1.0") and PREFIX not in got.out
    assert [line for line in got.err.splitlines() if line.startswith(PREFIX)] == [
        PREFIX + RELEASE.format("full-source-release"),
        PREFIX + DEFAULT.format(PAIR),
    ]
