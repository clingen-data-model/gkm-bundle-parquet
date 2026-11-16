"""The old name's deprecation (ADR 0009 decision 5): every invocation says, in one line on stderr, that the tool is
renamed gkm-bundle-parquet, naming the command to run instead. Nothing on stdout and no exit code changes. The
rename (2.0.0) removes it."""

from __future__ import annotations

import json

import pytest
from _fixture import FIXTURES

from clinvar_gkm_parquet.cli import main
from clinvar_gkm_parquet.compat import version_info

PREFIX = "clinvar-gkm-parquet: deprecated: "
CIVIC = FIXTURES / "civic"
CIVIC_PAIRING = ["--bundle-schema", str(CIVIC / "schema-set"), "--column-set", str(CIVIC / "civic.columns.yaml")]

NAME = (
    "this tool is renamed gkm-bundle-parquet (https://github.com/clingen-data-model/gkm-bundle-parquet), and the "
    "name clinvar-gkm-parquet is removed in 2.0.0; from 2.0.0, install gkm-bundle-parquet and run `{}`"
)


@pytest.mark.parametrize("command", ["version", "compile-spec"])
def test_every_invocation_names_the_new_tool_first_and_stdout_is_unchanged(capsys, command):
    rc = main(["version", "--json"] if command == "version" else ["compile-spec", *CIVIC_PAIRING])
    got = capsys.readouterr()
    assert rc == 0
    assert got.err.splitlines() == [PREFIX + NAME.format(f"gkm-bundle-parquet {command}")]
    if command == "version":
        assert json.loads(got.out) == json.loads(json.dumps(version_info()))
    else:
        assert got.out.startswith("# Table spec sections")


def test_help_and_a_usage_error_name_the_new_tool_too(capsys):
    for argv, code in ((["--help"], 0), (["build", "--no-such-option"], 2)):
        with pytest.raises(SystemExit) as e:
            main(argv)
        assert e.value.code == code
        run = "gkm-bundle-parquet build" if argv[0] == "build" else "gkm-bundle-parquet"
        assert capsys.readouterr().err.splitlines()[0] == PREFIX + NAME.format(run)
