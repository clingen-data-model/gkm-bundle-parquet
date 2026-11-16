"""verify: the check of a built directory, the bundle recompute and the round trip (ADR 0005) (design plan §8.2).

Owned by the build/verify work package. Exit codes (CLI): 0 = pass, 1 = differences, 2 = usage or input error.

Every comparison runs in DuckDB over the Parquet files (and, for the recompute, the `bundle_<section>` tables,
one section at a time, streamed by the build's section pipeline), one table at a time, so memory stays bounded
by DuckDB's `memory_limit` (it spills to the work directory).
Python sees only counts, samples and, for the `data` comparison, the rows whose bytes differ, streamed in batches.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from . import sources
from .build import (
    KV_KEYS,
    KV_ORDER,
    KV_PER_FILE,
    KV_PREFIX,
    MANIFEST_NAME,
    SectionPipeline,
    default_work_dir,
    describe_file,
    file_sha256,
    key_column,
    record_column,
    resolve_build_inputs,
    sql_ident,
    sql_str,
    table_groups,
)
from .bundle import ROOT_VALUES_TABLE
from .compiled import compile_pairing, for_sorted_input, is_recorded, recorded_spec
from .errors import InputError, SpecError
from .model import BuildInputs
from .spec import SPEC_KV_KEYS, Spec

SAMPLES = 5
_TRUNCATE = 300
_BATCH = 10_000


def _trunc(v) -> str | None:
    if v is None:
        return None
    s = v if isinstance(v, str) else str(v)
    return s if len(s) <= _TRUNCATE else s[:_TRUNCATE] + "..."


def _pq(path: Path) -> str:
    return f"read_parquet({sql_str(str(path))})"


def read_kv(con: duckdb.DuckDBPyConnection, path: Path) -> dict[str, str]:
    """The clinvar_gkm_parquet.* key-value metadata of one file, prefix removed."""
    rows = con.execute(f"SELECT key, value FROM parquet_kv_metadata({sql_str(str(path))})").fetchall()
    out = {}
    for k, v in rows:
        k = k.decode() if isinstance(k, bytes) else k
        v = v.decode() if isinstance(v, bytes) else v
        if k.startswith(KV_PREFIX):
            out[k[len(KV_PREFIX) :]] = v
    return out


def dir_kv(con: duckdb.DuckDBPyConnection, out_dir: Path) -> dict[str, dict[str, str]]:
    """file name -> its clinvar_gkm_parquet.* key-value metadata, for every .parquet in the directory."""
    return {p.name: read_kv(con, p) for p in sorted(Path(out_dir).glob("*.parquet"))}


def spec_for_dir(con: duckdb.DuckDBPyConnection, out_dir: Path) -> Spec:
    """The spec that wrote the files: the compiled spec a build wrote beside its manifest, checked against the hash
    every file records (compiled.recorded_spec); every file must name the same one. Sets written by 1.x with the
    sections spec are read the same way. A set of another table spec (1.x's other built-in specs, or a
    tables.yaml, retired in 2.0.0) is refused."""
    kvs = dir_kv(con, out_dir)
    named = {
        tuple(kv.get(k) for k in SPEC_KV_KEYS) for kv in kvs.values() if kv.get("table_spec_version") is not None
    }
    if not named:
        raise SpecError(f"{out_dir}: no Parquet file of this tool names the table spec that wrote it")
    if len(named) > 1:
        labels = ", ".join(sorted(f"{n or '(unnamed)'} {v}" for n, v, *_ in named))
        raise SpecError(f"the files were written by different table specs: {labels}")
    kv = {k: v for k, v in zip(SPEC_KV_KEYS, named.pop(), strict=True) if v is not None}
    if not is_recorded(kv):
        name = f"{kv.get('table_spec', '(unnamed)')} {kv.get('table_spec_version', '?')}"
        raise SpecError(
            f"the files were written by table spec {name}, not a compiled spec; this "
            "tool reads only compiled specs since 2.0.0 (ADR 0008): check them with the 1.x tool that wrote them"
        )
    tools = {f.get("tool_version") for f in kvs.values()} - {None}
    kv["tool_version"] = ", ".join(sorted(tools)) or "?"
    return recorded_spec(out_dir, kv)


def pairing_problems(out_dir: Path, bundle_schema: str, column_set: str) -> list[str]:
    """Whether a pairing compiles to the spec that wrote the files: its table_spec_sha256, schema_set_sha256 and
    column_set_sha256 against the ones every file records. Empty: it does. A bundle the general reader normalized
    was written with the key-sorted form of the pairing's spec (compiled.for_sorted_input), which matches too."""
    spec, source = compile_pairing(str(bundle_schema), str(column_set))
    table_spec = {source.sha256, for_sorted_input(spec, source)[1].sha256}
    con = duckdb.connect()
    try:
        kvs = dir_kv(con, out_dir)
    finally:
        con.close()
    out = []
    for key, got in (
        ("table_spec_sha256", table_spec),
        ("schema_set_sha256", {source.schema_set_sha256}),
        ("column_set_sha256", {source.column_set_sha256}),
    ):
        recorded = sorted({kv.get(key) or "(none)" for kv in kvs.values()})
        if len(recorded) != 1 or recorded[0] not in got:
            out.append(
                f"--bundle-schema and --column-set compile to {key} {' or '.join(sorted(got))}; the files record "
                f"{', '.join(recorded)}"
            )
    return out


def dir_selection(kvs: dict[str, dict[str, str]]) -> str | None:
    """The files' clinvar_gkm_parquet.selection entry (ADR 0004): None for a full build. Files that disagree
    (some with one, some without, or different ones) are an error: one directory holds one set."""
    values = {kv.get("selection") for kv in kvs.values()}
    if len(values) > 1:
        raise SpecError("the files disagree on clinvar_gkm_parquet.selection: they are not one set")
    return values.pop() if values else None


# --- verify_dir ------------------------------------------------------------------------------------------------


def verify_dir(out_dir: Path, spec: Spec | None = None) -> list[str]:
    """Problems found in a built directory on its own (every spec table present, metadata consistent, DESCRIBE
    equals the spec, the manifest matches the files). Empty list = pass."""
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        raise InputError(f"{out_dir}: not a directory")
    con = duckdb.connect()
    problems: list[str] = []
    files = {p.name[: -len(".parquet")]: p for p in sorted(out_dir.glob("*.parquet"))}
    if spec is None:
        try:
            spec = spec_for_dir(con, out_dir)
        except SpecError as e:
            return [f"can't load the table spec the files name: {e}"]
    spec_names = [t.name for t in spec.tables]
    for name in spec_names:
        if name not in files:
            problems.append(f"{name}: {name}.parquet is missing")
    for name in sorted(set(files) - set(spec_names) - {ROOT_VALUES_TABLE}):
        problems.append(f"{name}: {name}.parquet is not a table of spec {spec.label}")

    kvs: dict[str, dict[str, str]] = {}
    for name in spec_names:
        if name not in files:
            continue
        path = files[name]
        got = describe_file(con, path)
        want = spec.table(name).describe()
        if got != want:
            problems.extend(f"{name}: schema: {d}" for d in _schema_diffs(got, want, "file", "spec"))
        kv = read_kv(con, path)
        kvs[name] = kv
        missing = [k for k in KV_KEYS if k not in kv]
        if missing:
            problems.append(f"{name}: key-value metadata lacks {', '.join(KV_PREFIX + k for k in missing)}")
        if kv.get("table", name) != name:
            problems.append(f"{name}: key-value metadata names table {kv['table']!r}")
        if "table_spec_version" in kv and kv["table_spec_version"] != spec.spec_version:
            problems.append(f"{name}: table_spec_version {kv['table_spec_version']} != spec {spec.spec_version}")
        if spec.name and kv.get("table_spec", spec.name) != spec.name:
            problems.append(f"{name}: table_spec {kv['table_spec']} != spec {spec.name}")
        versions = spec.bundle_schema_versions
        if versions and "bundle_schema_version" in kv and kv["bundle_schema_version"] not in versions:
            problems.append(
                f"{name}: bundle_schema_version {kv['bundle_schema_version']} is not one spec "
                f"{spec.spec_version} reads"
            )
        if "source_deltas" in kv:
            try:
                if not isinstance(json.loads(kv["source_deltas"]), list):
                    raise ValueError
            except ValueError:
                problems.append(f"{name}: source_deltas is not a JSON array: {_trunc(kv['source_deltas'])}")
    for k in KV_ORDER:
        if k in KV_PER_FILE:
            continue
        # A required key missing from a file is reported above; an optional one (the spec's name, a custom
        # spec's keys, the selection) must be on every file or on none.
        values = {kv[k] for kv in kvs.values() if k in kv} if k in KV_KEYS else {kv.get(k) for kv in kvs.values()}
        if len(values) > 1:
            per = ", ".join(f"{n}={_trunc(kv.get(k))}" for n, kv in sorted(kvs.items()))
            problems.append(f"key-value metadata {KV_PREFIX}{k} differs across files: {per}")

    manifest_path = out_dir / MANIFEST_NAME
    if manifest_path.exists():
        selections = {kv.get("selection") for kv in kvs.values()}
        selection = selections.pop() if len(selections) == 1 else None
        problems.extend(_check_manifest(con, manifest_path, files, selection))
    con.close()
    return problems


def _check_manifest(con, path: Path, files: dict[str, Path], selection: str | None = None) -> list[str]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        tables = manifest["tables"]
    except (ValueError, KeyError, TypeError) as e:
        return [f"{path.name}: unreadable manifest ({e})"]
    problems = []
    want = json.loads(selection) if selection is not None else None
    if manifest.get("selection") != want:
        problems.append(f"{path.name}: selection {manifest.get('selection')} != the files' {want}")
    recorded = manifest.get("table_spec") if isinstance(manifest.get("table_spec"), dict) else {}
    for key, kv_key in (
        ("sha256", "table_spec_sha256"),
        ("schema_set_sha256", "schema_set_sha256"),
        ("column_set_sha256", "column_set_sha256"),
    ):
        have = {read_kv(con, p).get(kv_key) for p in files.values()} - {None}
        if len(have) == 1 and recorded.get(key) not in have:
            problems.append(
                f"{path.name}: table_spec.{key} {recorded.get(key)} != the files' {kv_key} {have.pop()}"
            )
    for name, entry in sorted(tables.items()):
        if name not in files:
            problems.append(f"{path.name}: lists {name}, but {name}.parquet is missing")
            continue
        rows = con.execute(f"SELECT count(*) FROM {_pq(files[name])}").fetchone()[0]
        if rows != entry.get("rows"):
            problems.append(f"{path.name}: {name} rows {entry.get('rows')} != file {rows}")
        sha = file_sha256(files[name])
        if sha != entry.get("sha256"):
            problems.append(f"{path.name}: {name} sha256 {entry.get('sha256')} != file {sha}")
    return problems


def _schema_diffs(a: list[tuple[str, str]], b: list[tuple[str, str]], an: str, bn: str) -> list[str]:
    da, db = dict(a), dict(b)
    out = []
    for c, _ in a:
        if c not in db:
            out.append(f"column {c} only in {an}")
        elif da[c] != db[c]:
            out.append(f"column {c}: {an} {da[c]} != {bn} {db[c]}")
    out.extend(f"column {c} only in {bn}" for c, _ in b if c not in da)
    common_a = [c for c, _ in a if c in db]
    common_b = [c for c, _ in b if c in da]
    if common_a != common_b:
        out.append(f"column order differs: {an} {common_a} vs {bn} {common_b}")
    return out


# --- bundle recompute ------------------------------------------------------------------------------------------


def recompute(con: duckdb.DuckDBPyConnection, spec: Spec, out_dir: Path, *, samples: int = SAMPLES) -> list[dict]:
    """Second leg of §8.2: per section group (one table, or the tables a row filter splits a section into), the
    id set equals the section keys and every `data` equals its bundle record. `con` holds `bundle_<section>`.

    Byte equality is checked by md5 of `data` against md5 of the record (a 128-bit digest), so the join carries
    two numbers per row, not the records themselves.
    """
    out_dir = Path(out_dir)
    names = [t.name for t in spec.tables]
    groups = table_groups(spec, names)
    seen: set[int] = set()
    results = []
    for name in names:
        g = groups[name]
        if id(g) in seen:
            continue
        seen.add(id(g))
        results.append(_recompute_group(con, spec, g.sections, g.tables, out_dir, samples))
    return results


def _recompute_group(con, spec: Spec, sections, table_names, out_dir: Path, samples: int) -> dict:
    tables = [spec.table(n) for n in table_names]
    res: dict = {"tables": list(table_names), "sections": list(sections), "problems": []}
    missing_files = [t.name for t in tables if not (out_dir / t.file_name).exists()]
    if missing_files:
        res["problems"].append(f"not checked: {', '.join(missing_files)} missing")
        return res
    no_key = [t.name for t in tables if key_column(t) is None]
    if no_key:
        res["problems"].append(f"not checked: {', '.join(no_key)} have no key column")
        return res
    with_record = all(record_column(t) for t in tables)
    union_t = " UNION ALL ".join(
        f"SELECT {sql_ident(key_column(t))} AS key, "
        f"{'md5_number(' + sql_ident(record_column(t)) + ')' if with_record else 'NULL::HUGEINT'} AS h, "
        f"{sql_str(t.name)} AS tbl FROM {_pq(out_dir / t.file_name)}"
        for t in tables
    )
    union_s = " UNION ALL ".join(
        f"SELECT key, {'md5_number(record)' if with_record else 'NULL::HUGEINT'} AS h FROM "
        f"{sql_ident('bundle_' + s)}"
        for s in sections
    )
    res["bundle_keys"] = con.execute(f"SELECT count(*) FROM ({union_s})").fetchone()[0]
    res["rows"] = dict(con.execute(f"SELECT tbl, count(*) FROM ({union_t}) GROUP BY tbl ORDER BY tbl").fetchall())
    for t in tables:
        res["rows"].setdefault(t.name, 0)
    con.execute(
        f"""CREATE OR REPLACE TEMP TABLE _rc_diff AS
            SELECT coalesce(t.key, s.key) AS key, t.tbl,
                   CASE WHEN s.key IS NULL THEN 'not_in_bundle' WHEN t.key IS NULL THEN 'missing'
                        ELSE 'data_differs' END AS kind
            FROM ({union_t}) t FULL OUTER JOIN ({union_s}) s ON t.key = s.key
            WHERE s.key IS NULL OR t.key IS NULL OR t.h IS DISTINCT FROM s.h"""
    )
    for kind in ("not_in_bundle", "missing", "data_differs"):
        n = con.execute("SELECT count(*) FROM _rc_diff WHERE kind = ?", [kind]).fetchone()[0]
        rows = con.execute(
            f"SELECT key, tbl FROM _rc_diff WHERE kind = ? ORDER BY key, tbl LIMIT {int(samples)}", [kind]
        ).fetchall()
        res[kind] = {"count": n, "samples": [{"key": k, "table": tb} for k, tb in rows]}
    con.execute("DROP TABLE _rc_diff")
    dup = con.execute(
        f"""SELECT key, list(tbl ORDER BY tbl), count(*) OVER () FROM ({union_t})
            GROUP BY key HAVING count(*) > 1 ORDER BY key LIMIT {int(samples)}"""
    ).fetchall()
    res["duplicates"] = {
        "count": dup[0][2] if dup else 0,
        "samples": [{"key": k, "tables": tb} for k, tb, _ in dup],
    }
    if not with_record:
        res["problems"].append("data not compared: a table of the group has no record column")
    return res


def recompute_failed(group: dict) -> bool:
    if group.get("problems") and "bundle_keys" not in group:
        return True
    return any(group.get(k, {}).get("count") for k in ("not_in_bundle", "missing", "data_differs", "duplicates"))


def bundle_recompute(
    out_dir: Path,
    inputs: BuildInputs,
    *,
    spec: Spec,
    work_dir: Path | None = None,
    memory_limit: str | None = None,
    threads: int | None = None,
    log=None,
    samples: int = SAMPLES,
) -> list[dict]:
    """Stream the bundle as a build does (build.SectionPipeline) and, per section, compare the section's
    (key, record) set with the built tables' (id, data): `recompute`, one section at a time, with no
    state database."""
    out_dir = Path(out_dir)
    names = [t.name for t in spec.tables]
    groups: list = []
    for g in table_groups(spec, names).values():
        if not any(g is x for x in groups):
            groups.append(g)
    by_section: dict[str, list] = {}
    for g in groups:
        if len(g.sections) != 1:
            raise SpecError(f"table(s) {', '.join(g.tables)} read sections {list(g.sections)}; one is expected")
        by_section.setdefault(g.sections[0], []).append(g)
    found: dict[int, dict] = {}
    root = Path(work_dir) if work_dir else default_work_dir(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="verify-", dir=root))

    def process(con: duckdb.DuckDBPyConnection, section: str, rows: int) -> None:
        for g in by_section.get(section, []):
            found[id(g)] = _recompute_group(con, spec, g.sections, g.tables, out_dir, samples)

    pipe = SectionPipeline(
        inputs,
        spec,
        work,
        process=process,
        needed=set(by_section),
        memory_limit=memory_limit,
        threads=threads,
        log=log,
    )
    try:
        pipe.run()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    out = []
    for g in groups:
        res = found.get(id(g))
        if res is None:  # a spec section that is not a bundle section
            res = {
                "tables": list(g.tables),
                "sections": list(g.sections),
                "problems": [f"not checked: section(s) {', '.join(g.sections)} are not bundle sections"],
            }
        out.append(res)
    return out


# --- report ----------------------------------------------------------------------------------------------------


@dataclass
class VerifyReport:
    dir: str
    spec_version: str
    spec_problems: list[str] = field(default_factory=list)
    recompute: list[dict] | None = None
    spec_name: str | None = None
    selection: dict | None = None  # the files' selection entry (a subset), or None for a full build
    roundtrip: dict | None = None  # roundtrip.verify_roundtrip

    @property
    def passed(self) -> bool:
        if self.spec_problems:
            return False
        if self.roundtrip and not self.roundtrip["passed"]:
            return False
        if self.recompute and any(recompute_failed(g) for g in self.recompute):
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "dir": self.dir,
            "table_spec": self.spec_name,
            "table_spec_version": self.spec_version,
            "selection": self.selection,
            "passed": self.passed,
            "spec_check": {"problems": self.spec_problems},
            "bundle_recompute": self.recompute,
            "roundtrip": self.roundtrip,
        }

    def to_markdown(self) -> str:
        lines = [f"# clinvar-gkm-parquet verify: {self.dir}", ""]
        spec = f"{self.spec_name} {self.spec_version}" if self.spec_name else self.spec_version
        subset = ""
        if self.selection is not None:
            subset = (
                f" A selection: driver {self.selection.get('driver')}, {self.selection.get('selected_records')} "
                f"records, mini-bundle sha256 {self.selection.get('selected_sha256')}."
            )
        lines += [f"Table spec {spec}.{subset} Result: **{'pass' if self.passed else 'differences'}**.", ""]
        lines += ["## Directory check", ""]
        lines += [f"- {p}" for p in self.spec_problems] or ["No problems."]
        lines.append("")
        if self.recompute is not None:
            lines += [
                "## Bundle recompute",
                "",
                "| Tables | Bundle keys | Rows | Not in bundle | Missing | data differs | Duplicates |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
            for g in self.recompute:
                rows = sum(g.get("rows", {}).values())
                cells = [
                    str(g.get(k, {}).get("count", "-"))
                    for k in ("not_in_bundle", "missing", "data_differs", "duplicates")
                ]
                lines.append(
                    f"| {', '.join(g['tables'])} | {g.get('bundle_keys', '-')} | {rows} | "
                    + " | ".join(cells)
                    + " |"
                )
            lines.append("")
            for g in self.recompute:
                if not recompute_failed(g) and not g.get("problems"):
                    continue
                lines += [f"### {', '.join(g['tables'])}", ""]
                lines += [f"- {p}" for p in g.get("problems", [])]
                for k in ("not_in_bundle", "missing", "data_differs", "duplicates"):
                    e = g.get(k)
                    if e and e["count"]:
                        lines.append(f"- {k}: {e['count']}; e.g. {', '.join(json.dumps(s) for s in e['samples'])}")
                lines.append("")
        if self.roundtrip is not None:
            rt = self.roundtrip
            lines += ["## Round trip", ""]
            lines += [f"- {p}" for p in rt["problems"]] or [
                f"The rebuilt text ({rt['text_size']} bytes, sha256 `{rt['text_sha256']}`) matches "
                + ", ".join(rt["checked"])
                + "."
            ]
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def write_report(report: VerifyReport, path: Path) -> tuple[Path, Path]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.to_markdown(), encoding="utf-8")
    jpath = path.with_suffix(".json")
    jpath.write_text(json.dumps(report.to_dict(), indent=2, default=str) + "\n", encoding="utf-8")
    return path, jpath


# --- CLI -------------------------------------------------------------------------------------------------------


def _usage(msg: str) -> int:
    print(f"clinvar-gkm-parquet verify: error: {msg}", file=sys.stderr)
    return 2


def run_verify_command(args) -> int:
    """`clinvar-gkm-parquet verify`: returns the exit code."""
    out_dir = Path(args.dir)
    if not out_dir.is_dir():
        return _usage(f"{out_dir}: not a directory")
    if args.bundle_recompute and not args.full:
        return _usage("--bundle-recompute needs --full")
    roundtrip = getattr(args, "roundtrip", False)
    if args.full and not (args.bundle_recompute or roundtrip):
        return _usage("--full is only read with --bundle-recompute or --roundtrip")
    pairing = getattr(args, "bundle_schema", None), getattr(args, "column_set", None)
    if (pairing[0] is None) != (pairing[1] is None):
        return _usage("--bundle-schema and --column-set go together: a schema and the column set it pairs with")

    meta = duckdb.connect()
    spec = spec_for_dir(meta, out_dir)
    kvs = dir_kv(meta, out_dir)
    meta.close()
    selection = dir_selection(kvs)
    params = json.loads(selection) if selection is not None else None
    report = VerifyReport(dir=str(out_dir), spec_version=spec.spec_version, spec_name=spec.name, selection=params)
    report.spec_problems = verify_dir(out_dir, spec)
    if pairing[0] is not None:
        report.spec_problems += pairing_problems(out_dir, *pairing)
    if args.bundle_recompute:
        if params is None:
            inputs = resolve_build_inputs(args)
        else:
            # A selection is recomputed against its mini-bundle (ADR 0004): `--full` names it, and its sha256
            # must be the recorded selected_sha256. `clinvar-gkm-parquet select` with the recorded options
            # regenerates it byte for byte.
            expected = params["selected_sha256"]
            if args.sha256 and args.sha256.lower() != expected:
                return _usage(f"--sha256 {args.sha256} is not the selection's selected_sha256 {expected}")
            inputs = BuildInputs(full=sources.resolve_input(args.full, sha256=expected))
        try:
            report.recompute = bundle_recompute(
                out_dir,
                inputs,
                spec=spec,
                work_dir=args.work_dir,
                memory_limit=args.memory_limit,
                threads=args.threads,
                log=lambda m: print(f"clinvar-gkm-parquet: {m}", file=sys.stderr, flush=True),
            )
        except InputError as e:
            if params is not None and "sha256 mismatch" in str(e):
                raise InputError(
                    f"--full {args.full} is not the mini-bundle these files were built from (selected_sha256 "
                    f"{params['selected_sha256']}): {e}"
                ) from e
            raise
    if roundtrip:
        from .roundtrip import verify_roundtrip

        report.roundtrip = verify_roundtrip(
            out_dir,
            full=args.full,
            sha256=args.sha256,
            memory_limit=args.memory_limit,
            threads=args.threads,
            work_dir=args.work_dir,
        )
    if args.report:
        md, js = write_report(report, args.report)
        print(f"report: {md} and {js}")
    print(_summary(report))
    return 0 if report.passed else 1


def _summary(r: VerifyReport) -> str:
    parts = [f"directory check: {len(r.spec_problems)} problem(s)"]
    if r.recompute is not None:
        parts.append(f"bundle recompute: {sum(recompute_failed(g) for g in r.recompute)} group(s) differ")
    if r.roundtrip is not None:
        word = "JSON-equal" if r.roundtrip.get("promise") == "json-equal" else "identical"
        parts.append(f"round trip: {word if r.roundtrip['passed'] else 'DIFFERS'}")
    lines = [f"verify {'pass' if r.passed else 'DIFFERENCES'}: " + "; ".join(parts)]
    lines += [f"  {p}" for p in r.spec_problems[:20]]
    if r.roundtrip is not None:
        lines += [f"  {p}" for p in r.roundtrip["problems"]]
    return "\n".join(lines)
