"""apply: bring a full lossless set up to date from an upserts bundle and a list of deleted keys (ADR 0011).

`apply` writes a new set: the base set's records, less the deleted keys and the keys the upserts bundle holds,
plus every record of the upserts bundle, section by section (deletes first, then upserts). The result is the set a
build of the *replayed bundle* writes: the base set's bundle with those records removed and added, its root values
replaced by name by the upserts bundle's, its root members in the base's order followed by any the upserts bundle
adds.

The inputs are generic: a set this tool wrote (a full lossless build, or an earlier `apply`); an ordinary bundle of
the same pairing and version, read by the build; and a JSON object `{"<section>": ["<key>", ...]}` of the keys to
remove. Which records a producer's release adds, changes and removes, and how its files say so, is the producer's.

Every table holds one row per record of one section, keyed by the record's key (spec.check_compiled), so a record's
row is replaced whole. A section neither input touches is copied unchanged with its recorded records sha256. A
touched one is written, then every row of it is hashed as `rebuild` hashes it (roundtrip.py), and:

- the rows that come from the base, with the base rows removed, must give the base file's `section_records_sha256`;
- the rows that come from the upserts bundle must give that bundle's own records sha256 for the section;
- all the rows give the result's `section_records_sha256`, and with the root the manifest's `records_sha256`.

So a set `apply` writes records the records sha256s of the replayed bundle, derived from the base's recorded ones
and the upserts bundle, and `rebuild` and `verify --roundtrip` check it as they check a build. Its files record no
source bundle (`source_bundle_sha256` is empty: no one file holds the state) and carry the chain of applications
(`applied`), which the manifest repeats with each input's sha256.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import duckdb

from . import canonical, compiled, sources
from .build import (
    MANIFEST_NAME,
    _check_table,
    _copy_sql,
    _now,
    build,
    configure,
    default_work_dir,
    file_sha256,
    kv_metadata,
    resolve_pairing,
    spec_kv,
    sql_ident,
    sql_str,
    write_root_values,
)
from .bundle import ROOT_VALUES_TABLE, peek_version
from .errors import InputError, RoundTripError
from .model import BuildInputs, InputFile, TableResult
from .roundtrip import (
    RECORD_IDENTICAL,
    _hashed,
    _Lines,
    _root_values,
    _shared_kv,
    failure,
    read_lossless_set,
    record_sql,
    root_members,
)
from .spec import Spec, SpecSource, Table
from .sqltemplate import macros_sql
from .transport import fetch_bytes, is_url

APPLIED_NOTE = (
    "Parquet tables derived from the bundle a replay gives: the base set's records, less the deleted keys and the "
    "upserts bundle's keys, plus the upserts bundle's records (inputs). No one file holds it; rebuild writes it."
)


@dataclass(frozen=True)
class Deletes:
    """The keys to remove, per section, and the file they were read from."""

    source: str
    name: str
    sha256: str
    size: int
    keys: dict[str, tuple[str, ...]]

    def record(self) -> dict:
        return {
            "source": self.source,
            "name": self.name,
            "sha256": self.sha256,
            "size": self.size,
            "sections": {s: len(k) for s, k in sorted(self.keys.items())},
        }


def read_deletes(source: str, sections: set[str]) -> Deletes:
    """A deletes file: a JSON object mapping a bundle section to the list of its keys to remove. A section the spec
    doesn't have, a key that isn't a string, or a key listed twice is refused."""
    name = source.rstrip("/").rsplit("/", 1)[-1]
    if is_url(source):
        data = fetch_bytes(source)
    else:
        try:
            data = Path(source).read_bytes()
        except OSError as e:
            raise InputError(f"--deletes {source}: {e.strerror or e}") from e
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise InputError(f"--deletes {source}: not JSON: {e}") from e
    if not isinstance(doc, dict):
        raise InputError(f'--deletes {source}: must be a JSON object {{"<section>": ["<key>", ...]}}')
    keys: dict[str, tuple[str, ...]] = {}
    for section, listed in doc.items():
        if section not in sections:
            raise InputError(f"--deletes {source}: {section!r} is not a section of the set's spec")
        if not isinstance(listed, list) or not all(isinstance(k, str) for k in listed):
            raise InputError(f"--deletes {source}: {section}: must be a list of keys (strings)")
        twice = sorted(k for k, n in Counter(listed).items() if n > 1)
        if twice:
            raise InputError(f"--deletes {source}: {section}: key(s) listed twice: {', '.join(twice[:5])}")
        if listed:
            keys[section] = tuple(listed)
    return Deletes(source, name, hashlib.sha256(data).hexdigest(), len(data), keys)


@dataclass(frozen=True)
class ApplyResult:
    out_dir: Path
    tables: tuple[TableResult, ...]
    manifest_path: Path
    records_sha256: str
    touched: tuple[str, ...]  # the sections the deletes or the upserts changed


def _pairing_problems(base_kv: dict[str, str], source: SpecSource) -> list[str]:
    out = []
    for key, got in (
        ("table_spec_sha256", source.sha256),
        ("schema_set_sha256", source.schema_set_sha256),
        ("column_set_sha256", source.column_set_sha256),
    ):
        if base_kv.get(key) != got:
            out.append(f"{key} {got}, the base set records {base_kv.get(key)}")
    return out


def apply(
    base_dir: Path,
    upserts: InputFile,
    deletes: Deletes | None,
    out_dir: Path,
    *,
    spec: Spec,
    spec_source: SpecSource,
    work_dir: Path | None = None,
    memory_limit: str | None = None,
    threads: int | None = None,
    workers: int | None = None,
    log: Callable[[str], None] | None = None,
) -> ApplyResult:
    """Write the set of the replayed bundle (module docstring) into `out_dir`, which must be new or empty. `spec`
    and `spec_source` are the pairing's (build.resolve_pairing); they must compile to the base set's spec. Leaves
    no partial set on failure."""
    log = log or (lambda m: None)
    base_dir, out_dir = Path(base_dir), Path(out_dir)
    base = read_lossless_set(base_dir)
    if base.legacy:
        raise RoundTripError(
            f"{base_dir}: built by tool {_shared_kv(base.kvs, 'tool_version')}, which records no records sha256: "
            "build it again with this tool to apply to it"
        )
    problems = _pairing_problems(next(iter(base.kvs.values())), spec_source)
    if problems:
        raise InputError(
            "the pairing given (--bundle-schema, --column-set, --version-property) doesn't compile to the spec "
            f"that wrote {base_dir}: it gives {'; '.join(problems)}. apply reads only the base set's pairing"
        )
    version = base.version or ""
    vprop = spec.root.version_property if spec.root else None
    if vprop:
        got = peek_version(upserts, vprop) or ""
        if got != version:
            raise InputError(
                f"--upserts {upserts.name}: {vprop} {got or '(none)'}, but the base set holds version {version}: "
                "apply reads only a bundle of the base set's version"
            )
    sections = {t.sections[0] for t in spec.tables}
    deleted = deletes.keys if deletes is not None else {}
    unknown = sorted(set(deleted) - sections)
    if unknown:
        raise InputError(f"--deletes: {', '.join(unknown)} are not sections of the base set")
    if out_dir.exists() and any(out_dir.iterdir()):
        raise InputError(f"output directory {out_dir} is not empty; apply into an empty or new directory")
    # The base's own records sha256 from its files' and its root: the manifest must agree (they are one build).
    base_root = canonical.root_sha256(
        root_members(base.root_order, vprop, base.version, _root_values_of(base.root_values_file), sections)
    )
    base_sections = base.section_records()
    if canonical.records_sha256(base_sections, base_root) != base.expected:
        raise RoundTripError(
            f"{base.manifest_path}: records_sha256 {base.expected} is not the one its files and root give"
        )

    started_at = _now()
    out_dir.mkdir(parents=True, exist_ok=True)
    work_root = Path(work_dir) if work_dir else default_work_dir(out_dir)
    created_root = not work_root.exists()
    work = work_root / f"apply-{os.getpid()}"
    if work.exists():
        shutil.rmtree(work)
    written: list[Path] = []
    con = duckdb.connect()
    hasher = canonical.Hasher(workers)
    try:
        log(f"building the upserts bundle {upserts.name}")
        up_dir = work / "upserts"
        up = build(
            BuildInputs(full=upserts),
            up_dir,
            work_dir=work / "upserts-work",
            memory_limit=memory_limit,
            threads=threads,
            spec=spec,
            spec_source=spec_source,
            log=log,
        )
        up_manifest = json.loads(up.manifest_path.read_text(encoding="utf-8"))
        if (up_manifest.get("bundle_schema_version") or "") != version:
            raise InputError(
                f"--upserts {upserts.name}: version {up_manifest.get('bundle_schema_version') or '(none)'}, but "
                f"the base set holds version {version or '(none)'}"
            )
        up_rows = {t: e["rows"] for t, e in up_manifest["tables"].items()}
        configure(con, memory_limit=memory_limit, threads=threads, temp_directory=work / "duckdb-tmp")
        con.execute(macros_sql())

        # 1. Each touched table, merged into the work directory and hashed (module docstring).
        merged: dict[str, Path] = {}
        result_sections: dict[str, str] = {}
        counts: dict[str, int] = {}
        touched: list[str] = []
        for t in spec.tables:
            s = t.sections[0]
            b_path = base.files[t.name]
            if not deleted.get(s) and not up_rows.get(t.name):
                result_sections[s] = base_sections[s]
                counts[s] = _rows(con, b_path)
                continue
            touched.append(s)
            log(f"table {t.name}: {len(deleted.get(s, ()))} deleted, {up_rows.get(t.name, 0)} upserted")
            u_path = up_dir / t.file_name
            want_u = _file_kv(con, u_path)["section_records_sha256"]
            merged[t.name], result_sections[s], counts[s] = _merge(
                con, t, b_path, u_path, deleted.get(s, ()), base_sections[s], want_u, work, hasher
            )

        # 2. The root: the base's members and values, the upserts bundle's values replacing them by name.
        up_values = _root_values_of(up_dir / f"{ROOT_VALUES_TABLE}.parquet")
        values = {**_root_values_of(base.root_values_file), **up_values}
        up_order = up_manifest.get("source_root") or []
        order = list(base.root_order) + [m for m in up_order if m not in base.root_order]
        missing = sorted(s for s, n in counts.items() if n and s not in order)
        if missing:
            raise RoundTripError(f"section(s) {missing} have records, but no root member names them")
        root = canonical.root_sha256(root_members(order, vprop, version, values, sections))
        records_sha256 = canonical.records_sha256(result_sections, root)

        # 3. The files, with the chain of applications.
        step = {
            "base_records_sha256": base.expected,
            "base_source_bundle_sha256": _shared_kv(base.kvs, "source_bundle_sha256"),
            "upserts_sha256": up_manifest["source_bundle_sha256"],
            "upserts_records_sha256": up_manifest["records_sha256"],
            "deletes_sha256": deletes.sha256 if deletes is not None else None,
            "records_sha256": records_sha256,
        }
        chain = json.loads(_shared_kv(base.kvs, "applied") or "[]") + [step]
        kv = kv_metadata(
            table_spec_version=spec.spec_version,
            bundle_schema_version=version,
            source_bundle_sha256="",
            table_spec=spec_kv(spec, spec_source),
        )
        kv["applied"] = json.dumps(chain, separators=(",", ":"))
        results: list[TableResult] = []
        for t in spec.tables:
            s = t.sections[0]
            src = merged.get(t.name, base.files[t.name])
            final = out_dir / t.file_name
            written.append(final)
            kv_t = {**kv, "section_records_sha256": result_sections[s]}
            con.execute(_copy_sql(spec, t, f"SELECT * FROM read_parquet({sql_str(str(src))})", final, kv_t))
            n = _rows(con, final)
            if n != counts[s]:
                raise RoundTripError(f"table {t.name}: wrote {n} rows, expected {counts[s]}")
            results.append(TableResult(t.name, final, n, file_sha256(final), final.stat().st_size))
        if values:
            written.append(out_dir / f"{ROOT_VALUES_TABLE}.parquet")
            results.append(write_root_values({k: v.encode("utf-8") for k, v in values.items()}, spec, out_dir, kv))
        written.append(compiled.write_compiled(spec_source, out_dir))
        manifest_path = out_dir / MANIFEST_NAME
        written.append(manifest_path)
        _write_manifest(
            manifest_path,
            kv=kv,
            spec=spec,
            base_dir=base_dir,
            base=base,
            upserts=up_manifest["inputs"]["full"],
            up_records=up_manifest["records_sha256"],
            up_counts=up_manifest["sections"],
            deletes=deletes,
            chain=chain,
            counts=counts,
            results=results,
            records_sha256=records_sha256,
            order=order,
            started_at=started_at,
        )
        return ApplyResult(out_dir, tuple(results), manifest_path, records_sha256, tuple(touched))
    except BaseException:
        for p in written:
            p.unlink(missing_ok=True)
        raise
    finally:
        hasher.close()
        con.close()
        shutil.rmtree(work, ignore_errors=True)
        if created_root:
            try:
                work_root.rmdir()
            except OSError:
                pass


def _rows(con: duckdb.DuckDBPyConnection, path: Path) -> int:
    return con.execute(f"SELECT count(*) FROM read_parquet({sql_str(str(path))})").fetchone()[0]


def _file_kv(con: duckdb.DuckDBPyConnection, path: Path) -> dict[str, str]:
    from .verify import read_kv

    return read_kv(con, path)


def _root_values_of(path: Path | None) -> dict[str, str]:
    if path is None or not Path(path).exists():
        return {}
    con = duckdb.connect()
    try:
        return _root_values(con, Path(path))
    finally:
        con.close()


def _merge(
    con: duckdb.DuckDBPyConnection,
    t: Table,
    b_path: Path,
    u_path: Path,
    deleted: tuple[str, ...],
    want_base: str,
    want_upserts: str,
    work: Path,
    hasher: canonical.Hasher,
) -> tuple[Path, str, int]:
    """One touched table: the base rows less the removed keys, plus the upserts rows, written to the work
    directory in sort-key order and checked (module docstring). Returns the file, the section's records sha256 and
    its rows."""
    key = sql_ident(t.columns_of("key")[0].name)
    base_rel, up_rel = f"read_parquet({sql_str(str(b_path))})", f"read_parquet({sql_str(str(u_path))})"
    con.execute("CREATE OR REPLACE TEMP TABLE __deleted (__dk VARCHAR)")
    if deleted:
        con.execute("INSERT INTO __deleted SELECT unnest(?::VARCHAR[])", [list(deleted)])
    absent = con.execute(
        f"SELECT __dk FROM __deleted ANTI JOIN (SELECT {key} AS __bk FROM {base_rel}) b ON __dk = __bk "
        "ORDER BY 1 LIMIT 5"
    ).fetchall()
    if absent:
        raise InputError(
            f"--deletes: section {t.sections[0]}: key(s) not in the base set: {', '.join(r[0] for r in absent)}; "
            "is the base the release the deletes were listed against?"
        )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE __removed AS "
        f"SELECT __dk AS __rk FROM __deleted UNION SELECT {key} FROM {up_rel}"
    )
    tmp = work / "merged" / t.file_name
    tmp.parent.mkdir(parents=True, exist_ok=True)
    body = (
        f"SELECT b.* FROM {base_rel} b ANTI JOIN __removed r ON b.{key} = r.__rk\nUNION ALL SELECT * FROM {up_rel}"
    )
    con.execute(
        f"COPY (SELECT * FROM (\n{body}\n) ORDER BY {sql_ident(t.sort_key)}) "
        f"TO {sql_str(str(tmp))} (FORMAT parquet)"
    )
    _check_table(con, t, tmp)

    rec = record_sql(t)
    every, from_base, from_up = (
        _Lines(work / "records", f"table {t.name}: {w}") for w in ("all", "base", "upserts")
    )
    try:
        sql = (
            f"SELECT o.{key}, {rec}, u.__k IS NOT NULL FROM read_parquet({sql_str(str(tmp))}) o "
            f"LEFT JOIN (SELECT {key} AS __k FROM {up_rel}) u ON o.{key} = u.__k"
        )
        for (k, _, is_up), sha in _hashed(con, sql, hasher, 1, f"table {t.name}"):
            why = failure(sha)
            if why is not None:
                raise RoundTripError(
                    f"table {t.name}: key {k}: the record written back has no canonical form: {why}"
                )
            every.add(k, sha)
            (from_up if is_up else from_base).add(k, sha)
        removed = 0
        sql = f"SELECT b.{key}, {rec} FROM {base_rel} b SEMI JOIN __removed r ON b.{key} = r.__rk"
        for (k, _), sha in _hashed(con, sql, hasher, 1, f"table {t.name}: base"):
            why = failure(sha)
            if why is not None:
                raise RoundTripError(f"table {t.name}: key {k}: the base row has no canonical form: {why}")
            from_base.add(k, sha)
            removed += 1
        n = every.n
        got_base = from_base.finish(con)
        if got_base != want_base:
            raise RoundTripError(
                f"table {t.name}: the base set's rows give records sha256 {got_base}, but its file records "
                f"{want_base}; check the base set with verify --roundtrip"
            )
        got_up = from_up.finish(con)
        if got_up != want_upserts:
            raise RoundTripError(
                f"table {t.name}: the upserted rows give records sha256 {got_up}, the upserts bundle "
                f"{want_upserts}"
            )
        sha = every.finish(con)
        expected = (
            len(deleted)
            + con.execute(
                f"SELECT count(*) FROM {base_rel} b "
                f"SEMI JOIN (SELECT {key} AS __k FROM {up_rel}) u ON b.{key} = u.__k"
            ).fetchone()[0]
        )
        if removed != expected:
            raise RoundTripError(f"table {t.name}: removed {removed} base rows, expected {expected}")
        return tmp, sha, n
    finally:
        for lines in (every, from_base, from_up):
            lines.discard()
        con.execute("DROP TABLE IF EXISTS __deleted")
        con.execute("DROP TABLE IF EXISTS __removed")


def _write_manifest(
    path: Path,
    *,
    kv: dict[str, str],
    spec: Spec,
    base_dir: Path,
    base,
    upserts: dict,
    up_records: str,
    up_counts: dict,
    deletes: Deletes | None,
    chain: list[dict],
    counts: dict[str, int],
    results: list[TableResult],
    records_sha256: str,
    order: list[str],
    started_at: str,
) -> None:
    """The manifest of an applied set: a build's (build.write_manifest), with no source bundle, its inputs (the
    base set, the upserts bundle, the deletes file) and the chain of applications."""
    ended_at = _now()
    base_manifest = base.manifest
    manifest = {
        "label": base_manifest.get("label"),
        "derived": True,
        "note": APPLIED_NOTE,
        "build_date": ended_at[:10],
        "source_release": None,
        "release": None,
        "builder": f"gkm-bundle-parquet {kv['tool_version']}",
        "dataset": None,
        "files": [
            {"name": r.file.name, "size": r.size, "sha256": r.sha256}
            for r in sorted(results, key=lambda r: r.file.name)
        ],
        "tool_version": kv["tool_version"],
        "table_spec": base_manifest.get("table_spec"),
        "table_spec_version": kv["table_spec_version"],
        "table_spec_status": spec.status,
        "bundle_schema_version": kv["bundle_schema_version"],
        "schema_version": kv["bundle_schema_version"],
        "duckdb_version": kv["duckdb_version"],
        "source_bundle_sha256": "",
        "inputs": {
            "base": {
                "dir": str(base_dir),
                "manifest_sha256": file_sha256(base.manifest_path),
                "records_sha256": base.expected,
                "source_bundle_sha256": base_manifest.get("source_bundle_sha256"),
                "files": base_manifest.get("files"),
            },
            "upserts": {**upserts, "records_sha256": up_records, "sections": up_counts},
            "deletes": deletes.record() if deletes is not None else None,
        },
        "applied": chain,
        "sections": dict(sorted(counts.items())),
        "tables": {
            r.table: {"file": r.file.name, "rows": r.rows, "sha256": r.sha256, "size": r.size}
            for r in sorted(results, key=lambda r: r.table)
        },
        "started_at": started_at,
        "ended_at": ended_at,
        "generated_at": ended_at,
        "records_sha256": records_sha256,
        "source_root": order,
        "round_trip": {"promise": RECORD_IDENTICAL, "source_layout": None},
    }
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


# --- CLI -------------------------------------------------------------------------------------------------------


def run_apply_command(args) -> int:
    """`gkm-bundle-parquet apply`: compile the pairing, apply, print a summary."""
    spec, source = resolve_pairing(args)  # a bad pairing is reported before anything is read
    upserts = sources.resolve_input(args.upserts, sha256=args.sha256)
    sections = {t.sections[0] for t in spec.tables}
    deletes = read_deletes(args.deletes, sections) if args.deletes else None
    result = apply(
        args.base,
        upserts,
        deletes,
        args.out,
        spec=spec,
        spec_source=source,
        work_dir=args.work_dir,
        memory_limit=args.memory_limit,
        threads=args.threads,
        log=lambda m: print(f"gkm-bundle-parquet: {m}", file=sys.stderr, flush=True),
    )
    total = sum(t.rows for t in result.tables)
    print(
        f"applied {upserts.name}{' and ' + deletes.name if deletes else ''} to {args.base}: "
        f"{len(result.tables)} table(s), {total} rows, {len(result.touched)} section(s) changed, records sha256 "
        f"{result.records_sha256}"
    )
    for t in result.tables:
        print(f"  {t.table:<36} {t.rows:>12}  {t.sha256[:12]}")
    print(f"manifest: {result.manifest_path}")
    return 0
