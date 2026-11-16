"""The default DuckDB memory limit (build.default_memory_limit): half the memory the process may use, physical or
the cgroup limit when lower, set by every run that gives no --memory-limit."""

from __future__ import annotations

import os

import duckdb
import pytest

from clinvar_gkm_parquet import build

GIB = 2**30


def _memory(monkeypatch, tmp_path, physical: int | None, cgroup: dict[str, str]) -> None:
    """Fake physical memory (None: sysconf fails) and the cgroup files (name -> content; others missing)."""
    page = 4096

    def sysconf(name):
        if physical is None:
            raise ValueError(name)
        return {"SC_PAGE_SIZE": page, "SC_PHYS_PAGES": physical // page}[name]

    monkeypatch.setattr(os, "sysconf", sysconf)
    files = []
    for name in ("v2", "v1"):
        path = tmp_path / name
        if name in cgroup:
            path.write_text(cgroup[name] + "\n")
        files.append(path)
    monkeypatch.setattr(build, "CGROUP_MEMORY_FILES", tuple(files))


@pytest.mark.parametrize(
    "physical, cgroup, want",
    [
        (32 * GIB, {}, "16384MiB"),
        (32 * GIB, {"v2": "max"}, "16384MiB"),
        (32 * GIB, {"v2": str(8 * GIB)}, "4096MiB"),
        (32 * GIB, {"v1": str(9223372036854771712)}, "16384MiB"),
        (32 * GIB, {"v1": str(6 * GIB)}, "3072MiB"),
        (32 * GIB, {"v2": "not a number"}, "16384MiB"),
        (None, {"v2": str(4 * GIB)}, "2048MiB"),
        (None, {}, None),
    ],
)
def test_the_default_is_half_the_memory_the_process_may_use(monkeypatch, tmp_path, physical, cgroup, want):
    _memory(monkeypatch, tmp_path, physical, cgroup)
    assert build.default_memory_limit() == want


def _limit(con) -> str:
    return con.execute("SELECT current_setting('memory_limit')").fetchone()[0]


def test_a_run_with_no_memory_limit_sets_the_default(monkeypatch, tmp_path):
    _memory(monkeypatch, tmp_path, 32 * GIB, {"v2": str(2 * GIB)})
    con, want = duckdb.connect(), duckdb.connect()
    build.configure(con)
    want.execute("SET memory_limit = '1024MiB'")
    assert _limit(con) == _limit(want)


def test_a_given_memory_limit_wins(monkeypatch, tmp_path):
    _memory(monkeypatch, tmp_path, 32 * GIB, {})
    con, want = duckdb.connect(), duckdb.connect()
    build.configure(con, memory_limit="3GB")
    want.execute("SET memory_limit = '3GB'")
    assert _limit(con) == _limit(want)


def test_with_nothing_known_duckdb_keeps_its_own_default(monkeypatch, tmp_path):
    con, want = duckdb.connect(), duckdb.connect()
    default = _limit(want)
    _memory(monkeypatch, tmp_path, None, {})
    build.configure(con)
    assert _limit(con) == default
