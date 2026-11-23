"""The round trip (ADR 0005, amended by ADR 0008): a row back to its bundle record, and a lossless build back to a
bundle with the same records.

A table with a `json` block (spec.py) states how each row is written back as its bundle record: which column is
which property, where pointers point. `record_sql(table)` turns that into one SQL expression over the table's
columns, which writes the properties in column order. Whether two records are the same is decided by their
canonical forms (canonical.py; docs/round-trip.md): key order, array order, string escapes, number spelling and
whitespace don't count. It serves three times:

- The build's check: `source_digest` hashes every record of a section as read, then `check_rows` writes every row
  of each table written back as a record, whose sha256 must equal its source's, or the build stops naming the key
  and the first character where the two canonical forms differ. So a record the columns can't reproduce (a JSON
  null, an open Range, a pointer to another section, a property no column holds) never makes it into a build. A
  lossless build records each section's records sha256 in its file's key-value metadata (`section_records_sha256`)
  and the bundle's records sha256 in the manifest (`records_sha256`).
- `rebuild`: a full build of a `lossless` spec (sections) back to a bundle in the assembler's line
  layout (bundle.py): the root members in the source's order, each section's records in `ordinal` order. It
  recomputes every section's records sha256 and the bundle's, and compares them with the recorded ones. Anything
  else stops it: a spec that is not lossless, a selection, deltas, a missing table or manifest, ordinals that are
  not 0..n-1, a pointer section outside the column's targets, a records sha256 that differs.
- `verify --roundtrip`: `rebuild` without writing; with `--full`, also against that bundle's own records sha256
  (and, unless `apply` wrote the set, ADR 0011, its file sha256 against the recorded `source_bundle_sha256`).

A set built by tool 1.x records the sha256s of its text (`section_text_sha256`, `source_text_sha256`) and no
records sha256. Its text sha256s are ignored: `rebuild` writes the set and reports the records sha256 it
recomputed, unchecked, and `verify --roundtrip` checks it only with `--full`, against that bundle's records sha256.

The JSON text: strings by `cgp_jstr` (DuckDB's to_json with lower-case \\u escapes), integers as their decimal
text, booleans as true/false. An object leaves out NULL members, so NULL is absent; a NULL list element is `null`.
A single-target pointer is `#/<target>/<id>`; a multi-target one takes its section from the pointer-section column
of the same path.

DuckDB pastes macro arguments into the macro body, so the expression is built here in Python and every macro it
calls gets column references, struct fields or lambda variables only (a nested expression passed to a macro that
uses its argument twice doubles at every level).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from . import canonical
from .bundle import ROOT_VALUES_TABLE
from .canonical import EMPTY_SHA256, Hasher, failure
from .compiler import encoding_parts, object_fields
from .errors import BundleFormatError, RoundTripError, SpecError
from .model import InputFile
from .spec import RANGE_KINDS, Column, Spec, Table

_FETCH = 5_000  # rows per fetch, and per batch hashed
_WRITE = 1 << 20  # text buffered before each write
_PROPERTY_PATH = re.compile(r"\$\.(\w+)(?:\[\*\])?")
_INTEGERS = ("bigint", "integer", "smallint", "tinyint", "hugeint", "ubigint", "uinteger", "usmallint", "utinyint")
_MIN_CSV_BUFFER = 1 << 20
RECORD_IDENTICAL = "record-identical"  # what a lossless build promises (manifest round_trip.promise)


def _ident(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


# --- one row back to its record --------------------------------------------------------------------------------


class _Writer:
    """Builds one table's record expression; `ref(column)` is how a column is read."""

    def __init__(self, table: Table, ref: Callable[[str], str]):
        if table.json is None:
            raise SpecError(f"table {table.name}: no json block: its rows can't be written back as records")
        self.table = table
        self.ref = ref
        self.n = 0
        keys = table.columns_of("key")
        if len(keys) != 1:
            raise SpecError(f"table {table.name}: the round trip needs one key column")
        self.key = ref(keys[0].name)

    def var(self) -> str:
        self.n += 1
        return f"__e{self.n}"

    def ctx(self, c: Column) -> str:
        return _lit(f"table {self.table.name}: column {c.name}")

    # values -----------------------------------------------------------------------------------------------------

    def obj(self, parts: list[tuple[str, str]]) -> str:
        names = [n for n, _ in parts]
        if len(set(names)) != len(names):
            raise SpecError(f"table {self.table.name}: two members named alike in {names}")
        members = ", ".join(f"{_lit(json.dumps(n, ensure_ascii=False) + ':')} || {v}" for n, v in parts)
        return f"'{{' || concat_ws(',', {members}) || '}}'"

    def value(self, x: str, t, rel: str, c: Column, ptrs: dict[str, tuple[str, ...]], parts=None) -> str:
        """JSON text of x, of DuckDB type t, at position rel of column c's value (NULL stays NULL). `parts`: the
        column's encoded positions ([] for [*]); None for a spec of tool 1.x, which states none."""
        tid = t.id
        enc = (parts or {}).get(rel)
        if enc is not None and enc["form"] != "extension":
            if tid == "struct":  # a list element with derived parts
                return self.encode(enc, {f: f"{x}.{_ident(f)}" for f, _ in t.children}, c)
            return self.encode(enc, {encoding_parts(enc)[0][0]: x}, c)
        if tid == "struct":
            return f"CASE WHEN {x} IS NULL THEN NULL ELSE {self.struct(x, t, rel, c, ptrs, parts)} END"
        if tid == "list":
            e = self.var()
            elem = self.value(e, t.children[0][1], f"{rel}[]", c, ptrs, parts)
            return (
                f"CASE WHEN {x} IS NULL THEN NULL ELSE '[' || array_to_string(list_transform({x}, "
                f"lambda {e}: coalesce({elem}, 'null')), ',') || ']' END"
            )
        if rel in ptrs:
            targets = ptrs[rel]
            if tid != "varchar" or len(targets) != 1:
                raise SpecError(
                    f"table {self.table.name}: column {c.name}: pointer at {rel or '(column)'} needs VARCHAR and "
                    f"one target section (the round trip can't place a multi-target pointer inside a value)"
                )
            return f"cgp_jstr({_lit('#/' + targets[0] + '/')} || {x})"
        if tid == "varchar":
            return f"cgp_jstr({x})"
        if tid in _INTEGERS:
            return f"CAST({x} AS VARCHAR)"
        if tid == "boolean":
            return f"CASE WHEN {x} THEN 'true' WHEN NOT {x} THEN 'false' END"
        if tid == "double":  # the shortest text that reads back, as Python's and the check confirms it per row
            return f"CAST({x} AS VARCHAR)"
        raise SpecError(
            f"table {self.table.name}: column {c.name}: no exact JSON text for type {t} at {rel or '(column)'} "
            "(the round trip writes VARCHAR, integers, DOUBLE and BOOLEAN)"
        )

    def struct(self, x: str, t, rel: str, c: Column, ptrs, parts=None) -> str:
        fields = list(t.children)
        names = [f for f, _ in fields]
        if parts is not None:
            is_ext = parts.get(rel, {}).get("form") == "extension"
        else:
            is_ext = "name" in names and any(f.startswith("value_") for f in names)
        slots = [(f, ft) for f, ft in fields if f.startswith("value_")] if is_ext else []
        types = dict(fields)
        members: list[tuple[str, str]] = []
        done: set[str] = set()
        for f, ft in fields:
            if f in done:
                continue
            if is_ext and f.startswith("value_"):
                if f != slots[0][0]:
                    continue
                if self.table.extension_slots.get(c.name) != "json-type":
                    raise SpecError(
                        f"table {self.table.name}: column {c.name}: the round trip needs json-type extension "
                        "slots (with slots by name, the value's JSON kind is not known)"
                    )
                values = [self.value(f"{x}.{_ident(s)}", st, f"{rel}.{s}", c, ptrs, parts) for s, st in slots]
                # Every slot NULL: the value was [] (an empty array fills no slot; a null or absent value stops
                # the build), possible only where the struct has an array slot.
                if any(st.id == "list" for _, st in slots):
                    values.append("'[]'")
                members.append(("value", f"coalesce({', '.join(values)})"))
                continue
            enc = (parts or {}).get(f"{rel}.{f}")
            if enc is not None and enc["form"] != "extension":
                named = object_fields(f, enc)
                missing = [n for n, _, _ in named if n not in types]
                if missing:
                    raise SpecError(
                        f"table {self.table.name}: column {c.name}: encoded {f} at {rel or '(column)'} has no "
                        f"field(s) {missing}"
                    )
                done.update(n for n, _, _ in named)
                members.append((f, self.encode(enc, {p: f"{x}.{_ident(n)}" for n, p, _ in named}, c)))
                continue
            members.append((f, self.value(f"{x}.{_ident(f)}", ft, f"{rel}.{f}", c, ptrs, parts)))
        return self.obj(members)

    def encode(self, enc: dict, ps: dict[str, str], c: Column) -> str:
        """JSON text of a value from its parts (`ps`: part -> SQL), by its encoding; NULL when every part is NULL
        (the value was absent, or a JSON null list element)."""
        form, ctx = enc["form"], self.ctx(c)
        targets = enc.get("targets") or []
        tl = "[" + ", ".join(_lit(t) for t in targets) + "]"

        def string(reading: str) -> str:
            if reading == "plain":
                return f"cgp_jstr({ps['string']})"
            if reading == "pointer":
                ident = ps["id"]
                if len(targets) == 1:
                    return f"cgp_jstr({_lit('#/' + targets[0] + '/')} || {ident})"
                sec = f"cgp_ptr_section({ps['section']}, {ident}, {tl}, {ctx}, {self.key})"
                return f"cgp_jstr('#/' || {sec} || '/' || {ident})"
            return f"cgp_jstr(cgp_iri_text({ps['section']}, {ps['id']}, {tl}, {ctx}, {self.key}))"

        if form == "json":
            return ps["json"]
        if form == "range":
            ex, lo, hi = ps["integer"], ps["min"], ps["max"]
            return (
                f"CASE WHEN {ex} IS NOT NULL THEN CAST({ex} AS VARCHAR) "
                f"WHEN {lo} IS NULL AND {hi} IS NULL THEN NULL "
                f"ELSE '[' || coalesce(CAST({lo} AS VARCHAR), 'null') || ',' || coalesce(CAST({hi} AS VARCHAR), "
                "'null') || ']' END"
            )
        if form == "string-or-object":
            return f"coalesce({string(enc['string'])}, {ps['json']})"
        return string(form)

    # the record -------------------------------------------------------------------------------------------------

    def record(self) -> str:
        from .sqltemplate import _parse_type

        table, layout = self.table, self.table.json
        sections = {c.derivation.path: c for c in table.columns_of("pointer-section")}
        used_sections: set[str] = set()
        ranges: dict[str, dict[str, Column]] = {}
        for c in table.columns:
            if c.derivation.kind in RANGE_KINDS:
                ranges.setdefault(c.derivation.path, {})[c.derivation.kind] = c
        parts: list[tuple[str, str]] = []
        encoded: dict[str, dict[str, str]] = {}  # path -> part -> SQL, of part columns
        order: list[tuple[int, str, Column]] = []
        for c in table.columns:
            kind, path = c.derivation.kind, c.derivation.path
            where = f"table {table.name}: column {c.name}"
            if kind == "key":
                if layout.record_id:
                    parts.append(("id", f"cgp_jstr({self.ref(c.name)})"))
                continue
            if kind in ("ordinal", "pointer-section"):
                continue
            if kind == "id-present":
                parts.append(("id", f"CASE WHEN {self.ref(c.name)} THEN cgp_jstr({self.key}) END"))
                continue
            m = _PROPERTY_PATH.fullmatch(path or "")
            if not m:
                raise SpecError(f"{where}: the round trip needs a top-level path ($.<field>), not {path}")
            prop = m.group(1)
            t = _parse_type(c.type)
            if kind in RANGE_KINDS:
                group = ranges[path]
                if c is not next(iter(group.values())):
                    continue
                if set(group) != set(RANGE_KINDS):
                    raise SpecError(
                        f"{where}: path {path} needs all of {', '.join(RANGE_KINDS)} to be written back"
                    )
                ex, lo, hi = (self.ref(group[k].name) for k in RANGE_KINDS)
                parts.append(
                    (
                        prop,
                        f"CASE WHEN {ex} IS NOT NULL THEN CAST({ex} AS VARCHAR) "
                        f"WHEN {lo} IS NULL AND {hi} IS NULL THEN NULL "
                        f"ELSE '[' || coalesce(CAST({lo} AS VARCHAR), 'null') || ',' || "
                        f"coalesce(CAST({hi} AS VARCHAR), 'null') || ']' END",
                    )
                )
                continue
            if c.nulls != "when-absent":
                raise SpecError(f"{where}: the round trip needs nulls: when-absent (NULL = absent)")
            if kind == "scalar-path":
                parts.append((prop, self.value(self.ref(c.name), t, "", c, {})))
            elif kind == "json-text":
                parts.append((prop, self.ref(c.name)))
            elif kind == "bare-id":
                parts.append((prop, self.bare_id(c, t, sections, used_sections)))
            elif kind in ("typed-copy", "extension-slot"):
                d = c.derivation
                if d.pointers and not d.pointer_targets:
                    raise SpecError(f"{where}: pointers need their target sections ({{position: [sections]}})")
                ptrs = {p.replace("[*]", "[]"): ts for p, ts in zip(d.pointers, d.pointer_targets, strict=True)}
                encs = None if d.parts is None else {p.replace("[*]", "[]"): e for p, e in d.parts.items()}
                parts.append((prop, self.value(self.ref(c.name), t, "", c, ptrs, encs)))
            elif kind == "part":
                group = encoded.setdefault(path, {})
                if not group:
                    parts.append((prop, None))  # placed here; written once the group is complete
                    order.append((len(parts) - 1, path, c))
                group[c.derivation.part] = self.ref(c.name)
            else:
                raise SpecError(f"{where}: the round trip can't write back a {kind} column")
        for i, path, c in order:
            enc = c.derivation.encoding
            want = {p for p, _ in encoding_parts(enc)}
            if set(encoded[path]) != want:
                raise SpecError(
                    f"table {table.name}: path {path} needs the part columns {sorted(want)} to be written back, "
                    f"has {sorted(encoded[path])}"
                )
            parts[i] = (parts[i][0], self.encode(enc, encoded[path], c))
        unused = sorted(set(sections) - used_sections)
        if unused:
            raise SpecError(
                f"table {table.name}: pointer-section column(s) of path(s) {unused} have no multi-target bare-id "
                "column of the same path"
            )
        return self.obj(parts)

    def bare_id(self, c: Column, t, sections: dict[str, Column], used: set[str]) -> str:
        targets = c.derivation.targets
        where = f"table {self.table.name}: column {c.name}"
        if not targets:
            raise SpecError(f"{where}: a bare-id column needs its target section(s) (targets) to be written back")
        x = self.ref(c.name)
        is_list = t.id == "list"
        if len(targets) == 1:
            prefix = _lit(f"#/{targets[0]}/")
            if not is_list:
                return f"cgp_jstr({prefix} || {x})"
            e = self.var()
            return (
                f"CASE WHEN {x} IS NULL THEN NULL ELSE '[' || array_to_string(list_transform({x}, "
                f"lambda {e}: coalesce(cgp_jstr({prefix} || {e}), 'null')), ',') || ']' END"
            )
        sc = sections.get(c.derivation.path)
        if sc is None or sc.type != c.type:
            raise SpecError(
                f"{where}: a pointer with several targets ({', '.join(targets)}) needs a pointer-section column "
                f"of the same path and type ({c.derivation.path}, {c.type})"
            )
        if sc.derivation.targets and set(sc.derivation.targets) != set(targets):
            raise SpecError(f"{where}: pointer-section column {sc.name} names other targets")
        used.add(c.derivation.path)
        s = self.ref(sc.name)
        tl = "[" + ", ".join(_lit(x) for x in targets) + "]"
        ctx = self.ctx(c)
        if not is_list:
            sec = f"cgp_ptr_section({s}, {x}, {tl}, {ctx}, {self.key})"
            return f"cgp_jstr('#/' || {sec} || '/' || {x})"
        e, i = self.var(), self.var()
        sec = f"cgp_ptr_section({s}[{i}], {e}, {tl}, {ctx}, {self.key})"
        return (
            f"CASE WHEN {x} IS NULL THEN NULL "
            f"WHEN len({x}) IS DISTINCT FROM len({s}) THEN cgp_fail({ctx}, {self.key}, "
            f"{_lit(f'{c.name} and {sc.name} differ in length')}) "
            f"ELSE '[' || array_to_string(list_transform({x}, "
            f"lambda {e}, {i}: coalesce(cgp_jstr('#/' || {sec} || '/' || {e}), 'null')), ',') || ']' END"
        )


def record_sql(table: Table, ref: Callable[[str], str] = _ident) -> str:
    """The SQL of a row's bundle record (its JSON text) from the table's columns (read as `ref(name)`)."""
    return _Writer(table, ref).record()


def lossless_problems(spec: Spec) -> list[str]:
    """What keeps the spec's tables from being written back (every table with a json block), and, for a
    `lossless` spec, from encoding every record of the bundle (spec.py). Empty: nothing."""
    out: list[str] = []
    for t in spec.tables:
        if t.json is None:
            continue
        try:
            record_sql(t)
        except SpecError as e:
            out.append(str(e))
    if not spec.lossless:
        return out
    have = [s for t in spec.tables for s in t.sections]
    repeated = sorted({s for s in have if have.count(s) > 1})
    if repeated or len(have) != len(spec.tables):
        out.append(f"lossless: the tables must read one section each, each section once (repeated: {repeated})")
    for t in spec.tables:
        tw = f"lossless: table {t.name}"
        if t.json is None:
            out.append(f"{tw}: needs a json block")
        if t.row_filter is not None:
            out.append(f"{tw}: has a row filter")
        if not t.known_fields:
            out.append(f"{tw}: needs known_fields: true")
        ords = t.columns_of("ordinal")
        if len(ords) > 1 or (ords and t.columns[-1] is not ords[0]):
            out.append(f"{tw}: an ordinal column (tool 1.x) is one, the last column")
    return out


# --- records sha256s -------------------------------------------------------------------------------------------


class _Lines:
    """A section's lines `<key> TAB <record sha256>` (canonical.key_line), written unsorted to a file in `work`.
    `finish` sorts them (DuckDB, spilling to disk) and hashes them in that order, which it checks: a key twice, or
    an order that isn't UTF-8 byte order, stops it."""

    def __init__(self, work: Path, where: str):
        work.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="records-", suffix=".tsv", dir=work)
        self.path = Path(name)
        self._fh = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        self.where = where
        self.n = 0
        self.max_line = 0

    def add(self, key: str, sha: str) -> None:
        line = canonical.key_line(key, sha) + "\n"
        self._fh.write(line)
        self.n += 1
        if len(line) > self.max_line:
            self.max_line = len(line)

    def source(self) -> str:
        max_line = 4 * self.max_line + 1024  # bytes; a character is at most 4
        return (
            f"read_csv({_lit(str(self.path))}, delim='\t', quote='', escape='', header=false, auto_detect=false, "
            f"new_line='\\n', strict_mode=true, columns={{'k': 'VARCHAR', 'sha': 'VARCHAR'}}, "
            f"max_line_size={max_line}, buffer_size={max(_MIN_CSV_BUFFER, 2 * max_line)})"
        )

    def finish(self, con: duckdb.DuckDBPyConnection, table: str | None = None) -> str:
        """The records sha256 of the lines; with `table`, also a temporary table of (key, sha, k), the key decoded
        and `k` its canonical string form, as in the line."""
        self._fh.close()
        if not self.n:
            if table is not None:
                con.execute(f"CREATE OR REPLACE TEMP TABLE {_ident(table)} (key VARCHAR, sha VARCHAR, k VARCHAR)")
            return EMPTY_SHA256
        sha, seen = lines_sha256(con, f"SELECT k, sha FROM {self.source()}", self.where)
        try:
            if table is not None:
                con.execute(
                    f"CREATE OR REPLACE TEMP TABLE {_ident(table)} AS "
                    f"SELECT json_extract_string(k, '$') AS key, sha, k FROM {self.source()}"
                )
        except duckdb.Error as e:
            raise RoundTripError(f"{self.where}: the records sha256: {e}") from e
        if seen != self.n:
            raise RoundTripError(f"{self.where}: {self.n} lines written, {seen} read back")
        return sha

    def discard(self) -> None:
        self._fh.close()
        self.path.unlink(missing_ok=True)


def lines_sha256(con: duckdb.DuckDBPyConnection, sql: str, where: str) -> tuple[str, int]:
    """The records sha256 of the lines `k TAB sha` that `sql` gives (k: a key in canonical string form), sorted
    here (DuckDB, spilling to disk), and how many. A key twice, or an order that isn't UTF-8 byte order, stops
    it."""
    h = hashlib.sha256()
    prev, seen = None, 0
    try:
        rel = con.sql(f"SELECT k, sha FROM ({sql}) ORDER BY k")
        while batch := rel.fetchmany(_FETCH):
            for k, sha in batch:
                if prev is not None and k <= prev:
                    raise RoundTripError(f"{where}: key {k} {'twice' if k == prev else 'out of order'}")
                h.update(f"{k}\t{sha}\n".encode())
                prev = k
            seen += len(batch)
    except duckdb.Error as e:
        raise RoundTripError(f"{where}: the records sha256: {e}") from e
    return h.hexdigest(), seen


def _hashed(
    con: duckdb.DuckDBPyConnection, sql: str, hasher: Hasher, text_at: int, where: str
) -> Iterator[tuple[tuple, str]]:
    """(row, the record sha256 of its column `text_at`) for each row of `sql`, in order; the sha256 is a
    canonical.failure mark for a text with no canonical form. A query that fails raises RoundTripError."""
    try:
        rel = con.sql(sql)
    except duckdb.Error as e:
        raise RoundTripError(f"{where}: {e}") from e

    def batches():
        while True:
            try:
                rows = rel.fetchmany(_FETCH)
            except duckdb.Error as e:
                raise RoundTripError(f"{where}: {e}") from e
            if not rows:
                return
            yield rows, [r[text_at] for r in rows]

    for rows, shas in hasher.map(batches()):
        yield from zip(rows, shas, strict=True)


@dataclass
class SectionDigest:
    section: str
    sha256: str  # the section's records sha256
    records: int
    table: str | None  # a temporary table of (key, record sha256), for check_rows


def source_digest(
    con: duckdb.DuckDBPyConnection, section: str, hasher: Hasher, work: Path, *, keep: bool
) -> SectionDigest:
    """Every record of `bundle_<section>` (build.load_section) hashed: the section's records sha256 and, with
    `keep`, a temporary table of each key's record sha256 for check_rows. A record with no canonical form stops
    it."""
    from .build import bundle_table

    where = f"section {section}"
    lines = _Lines(work, where)
    try:
        sql = f"SELECT key, record FROM {bundle_table(section)}"
        for (key, _), sha in _hashed(con, sql, hasher, 1, where):
            why = failure(sha)
            if why is not None:
                raise BundleFormatError(f"{where}, key {key}: the record has no canonical form: {why}")
            lines.add(key, sha)
        table = f"__records_{section}" if keep else None
        return SectionDigest(section, lines.finish(con, table), lines.n, table)
    finally:
        lines.discard()


def check_rows(
    con: duckdb.DuckDBPyConnection, table: Table, path: Path, digest: SectionDigest, hasher: Hasher
) -> None:
    """The build's check of a written file: every row, written back as a record, has its source record's sha256
    (`digest`, kept by source_digest). The first that doesn't stops the build, naming the key."""
    key = _ident(table.columns_of("key")[0].name)
    sql = (
        f"SELECT r.__k, r.__rec, s.sha FROM (SELECT {key} AS __k, {record_sql(table)} AS __rec "
        f"FROM read_parquet({_lit(str(path))})) r LEFT JOIN {_ident(digest.table)} s ON s.key = r.__k"
    )
    for (k, rec, want), got in _hashed(con, sql, hasher, 1, f"table {table.name}: round trip"):
        if got != want:
            raise RoundTripError(_difference(con, table, k, rec, want, got))


def _difference(con, table: Table, key: str, rec: str | None, want: str | None, got: str) -> str:
    from .build import bundle_table

    where = f"table {table.name}: round trip: key {key}"
    if want is None:
        return f"{where}: the row has no bundle record"
    why = failure(got)
    if why is not None:
        return f"{where}: the record written back from the row has no canonical form: {why}"
    src = con.execute(f"SELECT record FROM {bundle_table(table.sections[0])} WHERE key = ?", [key]).fetchone()[0]
    a, b = canonical.canonical_text(rec), canonical.canonical_text(src)
    i = next((i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), min(len(a), len(b)))
    lo = max(i - 60, 0)
    return (
        f"{where}: the record written back from the row differs from the bundle record at character {i + 1} of "
        f"their canonical forms: rebuilt ...{a[lo : i + 60]}... bundle ...{b[lo : i + 60]}..."
    )


def root_members(
    order: list[str] | tuple[str, ...],
    version_property: str | None,
    version: str,
    values: dict[str, str | bytes],
    sections: set[str],
) -> list[tuple[str, str | bytes | None]]:
    """The root's members for canonical.root_sha256, from its order: the version, a section (None) or a value."""
    out: list[tuple[str, str | bytes | None]] = []
    for name in order:
        if name == version_property:
            out.append((name, json.dumps(version, ensure_ascii=False)))
        elif name in sections:
            out.append((name, None))
        elif name in values:
            out.append((name, values[name]))
        else:
            raise RoundTripError(f"root member {name!r}: no value holds it")
    return out


@dataclass
class BundleRecords:
    """A bundle's records sha256s, read from the bundle itself (`verify --roundtrip --full`)."""

    sha256: str  # of the compressed file
    sections: dict[str, str]  # section -> records sha256
    root_sha256: str
    records_sha256: str


def bundle_records(
    inp: InputFile,
    spec: Spec,
    work: Path,
    hasher: Hasher,
    *,
    memory_limit: str | None = None,
    threads: int | None = None,
) -> BundleRecords:
    """Stream a bundle once (build.SectionPipeline) and take its records sha256s, as a build of it records them."""
    from .build import SectionPipeline
    from .model import BuildInputs

    shas: dict[str, str] = {}

    def process(con: duckdb.DuckDBPyConnection, section: str, rows: int) -> None:
        shas[section] = source_digest(con, section, hasher, work / "records", keep=False).sha256

    pipe = SectionPipeline(
        BuildInputs(full=inp),
        spec,
        work,
        process=process,
        needed=None,
        memory_limit=memory_limit,
        threads=threads,
    )
    state = pipe.run()
    members = root_members(
        state.full.root,
        pipe.rules.version_property,
        state.full.schema_version,
        pipe.root_values,
        set(pipe.sections),
    )
    root = canonical.root_sha256(members)
    return BundleRecords(state.full.sha256, shas, root, canonical.records_sha256(shas, root))


# --- a build back to its bundle -------------------------------------------------------------------------------


@dataclass
class RebuildResult:
    out: Path | None  # None: the records were hashed, not written
    schema_version: str
    records_sha256: str
    expected_records_sha256: str | None  # the manifest's; None for a set built by tool 1.x, which records none
    text_size: int
    root_sha256: str = ""
    sections: dict[str, int] = field(default_factory=dict)  # section -> records written
    section_records: dict[str, str] = field(default_factory=dict)  # section -> its records sha256, as rebuilt
    tool_version: str = ""  # of the build

    @property
    def checked(self) -> bool:
        return self.expected_records_sha256 is not None


class RoundTripMismatch(RoundTripError):
    """The rebuilt records differ from the recorded ones: a section's records sha256, or the bundle's."""


class _Out:
    """Where the text goes: a file (gzip when the name ends in .gz), or nowhere; counted either way."""

    def __init__(self, path: Path | None, compresslevel: int, gz: bool):
        self.size = 0
        self._fh = self._gz = None
        if path is not None:
            self._fh = open(path, "wb")
            if gz:
                self._gz = gzip.GzipFile(
                    filename="", mode="wb", fileobj=self._fh, compresslevel=compresslevel, mtime=0
                )

    def write(self, b: bytes) -> None:
        self.size += len(b)
        if self._gz is not None:
            self._gz.write(b)
        elif self._fh is not None:
            self._fh.write(b)

    def close(self) -> None:
        try:
            if self._gz is not None:
                self._gz.close()
        finally:
            if self._fh is not None:
                self._fh.close()


def _shared_kv(kvs: dict[str, dict[str, str]], key: str) -> str | None:
    values = {kv.get(key) for kv in kvs.values()}
    if len(values) != 1:
        raise RoundTripError(f"the files disagree on gkm_bundle_parquet.{key}: they are not one build")
    return values.pop()


def built_by_1x(kvs: dict[str, dict[str, str]]) -> bool:
    """The files are a lossless build by tool 1.x: each carries its section's text sha256 and none its records
    sha256."""
    return bool(kvs) and all(
        "section_text_sha256" in kv and "section_records_sha256" not in kv for kv in kvs.values()
    )


@dataclass
class LosslessSet:
    """A full lossless build on disk, as `read_lossless_set` checked it: what `rebuild` and `apply` read."""

    spec: Spec
    kvs: dict[str, dict[str, str]]  # file name -> its key-value metadata, the table files only
    files: dict[str, Path]  # table -> its file
    root_values_file: Path | None
    manifest: dict
    manifest_path: Path
    expected: str | None  # the manifest's records_sha256; None for a set built by tool 1.x, which records none
    legacy: bool  # built by tool 1.x
    version: str | None  # the bundle's version (bundle_schema_version)
    root_order: list[str]  # the manifest's source_root

    def section_records(self) -> dict[str, str | None]:
        """section -> the records sha256 its file records (None for a set built by tool 1.x)."""
        return {
            t.sections[0]: None if self.legacy else self.kvs[self.files[t.name].name]["section_records_sha256"]
            for t in self.spec.tables
        }


def read_lossless_set(out_dir: Path) -> LosslessSet:
    """Read and check what makes a directory a full lossless build (module docstring): its spec, its files and
    their key-value metadata, and its manifest. Refuses anything else with RoundTripError."""
    from .build import MANIFEST_NAME
    from .verify import dir_kv, spec_for_dir

    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        raise RoundTripError(f"{out_dir}: not a directory")
    meta = duckdb.connect()
    try:
        spec = spec_for_dir(meta, out_dir)
        kvs = dir_kv(meta, out_dir)
    finally:
        meta.close()
    if not spec.lossless:
        raise RoundTripError(
            f"{out_dir}: written by table spec {spec.label}, which doesn't encode every record of the bundle "
            "(lossless: true); only a full build of such a spec (sections) can be rebuilt"
        )
    files = {name[: -len(".parquet")]: out_dir / name for name in kvs}
    root_values_file = files.pop(ROOT_VALUES_TABLE, None)
    kvs = {name: kv for name, kv in kvs.items() if name != f"{ROOT_VALUES_TABLE}.parquet"}
    missing = [t.name for t in spec.tables if t.name not in files]
    if missing:
        raise RoundTripError(f"{out_dir}: missing table(s) {', '.join(missing)}: a rebuild needs every table")
    extra = sorted(set(files) - {t.name for t in spec.tables})
    if extra:
        raise RoundTripError(f"{out_dir}: file(s) of no table of spec {spec.label}: {', '.join(extra)}")
    if any("selection" in kv for kv in kvs.values()):
        raise RoundTripError(f"{out_dir}: a selective build (gkm_bundle_parquet.selection) is not a whole bundle")
    if _shared_kv(kvs, "source_deltas") != "[]":
        raise RoundTripError(
            f"{out_dir}: built by 1.x with deltas replayed: the tables hold a state no one bundle holds"
        )
    legacy = built_by_1x(kvs)
    unrecorded = [n for n, kv in kvs.items() if "section_records_sha256" not in kv]
    if unrecorded and not legacy:
        raise RoundTripError(
            f"{out_dir}: {', '.join(sorted(unrecorded)[:5])} carry no gkm_bundle_parquet.section_records_sha256: "
            "not a lossless build (a selection or a build with deltas records none)"
        )
    for k in ("tool_version", "table_spec", "table_spec_version", "bundle_schema_version", "source_bundle_sha256"):
        _shared_kv(kvs, k)
    version = _shared_kv(kvs, "bundle_schema_version")
    if spec.bundle_schema_versions and version not in spec.bundle_schema_versions:
        raise RoundTripError(f"{out_dir}: bundle_schema_version {version} is not one spec {spec.label} reads")
    manifest_path = out_dir / MANIFEST_NAME
    if not manifest_path.exists():
        raise RoundTripError(f"{manifest_path}: missing; a rebuild checks the bundle against its records_sha256")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise RoundTripError(f"{manifest_path}: unreadable manifest ({e})") from e
    expected = None if legacy else manifest.get("records_sha256")
    if expected is None and not legacy:
        raise RoundTripError(f"{manifest_path}: no records_sha256, but the files are a lossless build")
    if manifest.get("source_bundle_sha256") != _shared_kv(kvs, "source_bundle_sha256"):
        raise RoundTripError(f"{manifest_path}: source_bundle_sha256 differs from the files'")
    recorded = manifest.get("table_spec") or {}
    for key, kv_key in (
        ("sha256", "table_spec_sha256"),
        ("schema_set_sha256", "schema_set_sha256"),
        ("column_set_sha256", "column_set_sha256"),
    ):
        files_have = next(iter(kvs.values())).get(kv_key)
        if files_have is not None and recorded.get(key) != _shared_kv(kvs, kv_key):
            raise RoundTripError(
                f"{manifest_path}: table_spec.{key} {recorded.get(key)} differs from the files' {kv_key}"
            )

    root_order = manifest.get("source_root")
    if not isinstance(root_order, list) or not isinstance(manifest.get("round_trip"), dict):
        raise RoundTripError(f"{manifest_path}: no source_root and round_trip: not a lossless build's manifest")
    return LosslessSet(
        spec, kvs, files, root_values_file, manifest, manifest_path, expected, legacy, version, root_order
    )


def rebuild(
    out_dir: Path,
    out: Path | None = None,
    *,
    memory_limit: str | None = None,
    threads: int | None = None,
    work_dir: Path | None = None,
    compresslevel: int = 6,
    workers: int | None = None,
    log: Callable[[str], None] | None = None,
) -> RebuildResult:
    """Write the bundle a full lossless build was made from (`out`, gzip when it ends in .gz; None: hash only) and
    check its records sha256s against the recorded ones (module docstring). Returns what was written; raises
    RoundTripError (RoundTripMismatch for differing records) and leaves no output file on any failure."""
    from .build import configure
    from .sqltemplate import macros_sql

    log = log or (lambda m: None)
    ls = read_lossless_set(out_dir)
    spec, kvs, files, root_values_file = ls.spec, ls.kvs, ls.files, ls.root_values_file
    manifest_path, expected, version, root_order = ls.manifest_path, ls.expected, ls.version, ls.root_order
    sections = tuple(s for t in spec.tables for s in t.sections)

    tmp = None
    if out is not None:
        out = Path(out)
        if out.exists():
            raise RoundTripError(f"{out}: already exists")
        tmp = out.with_name(f".{out.name}.{os.getpid()}.partial")
    if work_dir is not None:
        Path(work_dir).mkdir(parents=True, exist_ok=True)  # as the bundle recompute makes it
    spill = Path(tempfile.mkdtemp(prefix="gkm-bundle-parquet-rebuild-", dir=work_dir))
    con = duckdb.connect()
    hasher = Hasher(workers)
    try:
        configure(con, memory_limit=memory_limit, threads=threads, temp_directory=spill)
        con.execute(macros_sql())
        by_section = {t.sections[0]: t for t in spec.tables}
        rows = {s: _open_table(con, by_section[s], files[by_section[s].name], spec) for s in sections}
        present = [s for s in sections if rows[s]]
        stray = [s for s in present if s not in root_order]
        if stray:
            raise RoundTripError(
                f"{manifest_path}: section(s) {stray} have rows, but source_root doesn't list them"
            )
        values = _root_values(con, root_values_file)
        version_property = spec.root.version_property
        members = root_members(root_order, version_property, version, values, set(by_section))
        unused = sorted(set(values) - set(root_order))
        if unused:
            raise RoundTripError(f"{manifest_path}: root value(s) {unused} are not in source_root")
        want = ls.section_records()
        sink = _Out(tmp, compresslevel, gz=out is not None and out.name.endswith(".gz"))
        ok = False
        try:
            digests = _write(con, sink, members, by_section, rows, want, hasher, spill / "records", log)
            root = canonical.root_sha256(members)
            got = canonical.records_sha256(digests, root)
            if expected is not None and got != expected:
                raise RoundTripMismatch(
                    f"the rebuilt bundle has records sha256 {got}, the manifest records records_sha256 {expected}"
                    " (every section's records sha256 matches, so its root differs)"
                )
            sink.close()
            if tmp is not None:
                os.replace(tmp, out)
            ok = True
        finally:
            if not ok:
                try:
                    sink.close()
                except OSError:
                    pass
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
    finally:
        hasher.close()
        con.close()
        shutil.rmtree(spill, ignore_errors=True)
    return RebuildResult(
        out=out,
        schema_version=version,
        records_sha256=got,
        expected_records_sha256=expected,
        text_size=sink.size,
        root_sha256=root,
        sections={s: rows[s] for s in present},
        section_records=digests,
        tool_version=_shared_kv(kvs, "tool_version") or "?",
    )


def _root_values(con: duckdb.DuckDBPyConnection, path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    from .build import sql_str

    rows = con.execute(f'SELECT "name", "value" FROM read_parquet({sql_str(str(path))})').fetchall()
    return dict(rows)


def _open_table(con: duckdb.DuckDBPyConnection, t: Table, path: Path, spec: Spec) -> int:
    """A view of the table's file, checked: the spec's columns, distinct non-NULL keys, and (tool 1.x) ordinals
    0..n-1. Returns n."""
    from .build import describe_file, sql_str

    if describe_file(con, path) != t.describe():
        raise RoundTripError(f"{path.name}: its columns are not table {t.name}'s of spec {spec.label}")
    con.execute(f"CREATE VIEW {_ident(t.name)} AS SELECT * FROM read_parquet({sql_str(str(path))})")
    k = _ident(t.columns_of("key")[0].name)
    n, keys = con.execute(f"SELECT count(*), count(DISTINCT {k}) FROM {_ident(t.name)}").fetchone()
    if keys != n:
        raise RoundTripError(f"table {t.name}: {n} rows but {keys} distinct keys: a section can't hold them")
    if not t.columns_of("ordinal"):  # tool 2.0.0 keeps no record order (ADR 0008 decision 1.1)
        return n
    o = _ident(t.columns_of("ordinal")[0].name)
    n, n_o, distinct, lo, hi = con.execute(
        f"SELECT count(*), count({o}), count(DISTINCT {o}), min({o}), max({o}) FROM {_ident(t.name)}"
    ).fetchone()
    if n and (n_o, distinct, lo, hi) != (n, n, 0, n - 1):
        raise RoundTripError(
            f"table {t.name}: the ordinals are not 0..{n - 1} ({n} rows, {n_o} ordinals, {distinct} distinct, "
            f"min {lo}, max {hi}): the record order can't be restored"
        )
    return n


def _write(
    con,
    sink: _Out,
    members: list[tuple],
    by_section: dict[str, Table],
    rows: dict[str, int],
    want: dict[str, str | None],
    hasher: Hasher,
    work: Path,
    log,
) -> dict[str, str]:
    """The bundle text in the line layout (bundle.py), its root members in order; returns each section's records
    sha256, compared with the recorded one (`want`, None: nothing recorded) as soon as the section is written. A
    section with no rows is `{}`."""
    sink.write(b"{\n")
    digests: dict[str, str] = {}
    for i, (name, text) in enumerate(members):
        end = b",\n" if i < len(members) - 1 else b"\n"
        quoted = json.dumps(name, ensure_ascii=False).encode()
        if text is not None:
            sink.write(b"  " + quoted + b": " + (text if isinstance(text, bytes) else text.encode("utf-8")) + end)
            continue
        t, n = by_section[name], rows[name]
        if not n:
            sink.write(b"  " + quoted + b": {}" + end)
            digests[name] = EMPTY_SHA256
        else:
            log(f"section {name}: {n} records")
            sink.write(b"  " + quoted + b": {\n")
            digests[name] = _write_section(con, sink, t, n, hasher, work)
            sink.write(b"\n  }" + end)
        if want.get(name) is not None and digests[name] != want[name]:
            raise RoundTripMismatch(
                f"section {name}: the rebuilt records have records sha256 {digests[name]}, the build recorded "
                f"{want[name]}"
            )
    sink.write(b"}\n")
    return digests


def _write_section(con, sink: _Out, t: Table, n: int, hasher: Hasher, work: Path) -> str:
    """The table's record lines (indented, `,` between them) in ordinal order, each hashed; returns the section's
    records sha256. A failing row (a pointer section outside the column's targets) stops it."""
    key = _ident(t.columns_of("key")[0].name)
    ordinals = t.columns_of("ordinal")
    order = _ident(ordinals[0].name) if ordinals else key  # a set of tool 1.x keeps its record order
    sql = f"SELECT {key}, {record_sql(t)} FROM {_ident(t.name)} ORDER BY {order}"
    lines = _Lines(work, f"table {t.name}")
    buf: list[str] = []
    size, written = 0, 0
    try:
        for (k, rec), sha in _hashed(con, sql, hasher, 1, f"table {t.name}"):
            if k is None or rec is None:
                raise RoundTripError(f"table {t.name}: a row wrote no record (a NULL key?)")
            why = failure(sha)
            if why is not None:
                raise RoundTripError(
                    f"table {t.name}: key {k}: the record written back has no canonical form: {why}"
                )
            lines.add(k, sha)
            line = ("    " if not written else ",\n    ") + canonical.string(k) + ": " + rec
            buf.append(line)
            size += len(line)
            written += 1
            if size >= _WRITE:
                sink.write("".join(buf).encode("utf-8"))
                buf, size = [], 0
        sink.write("".join(buf).encode("utf-8"))
        if written != n:
            raise RoundTripError(f"table {t.name}: wrote {written} records of {n}")
        return lines.finish(con)
    finally:
        lines.discard()


def verify_roundtrip(
    out_dir: Path,
    *,
    full: str | None = None,
    sha256: str | None = None,
    memory_limit: str | None = None,
    threads: int | None = None,
    work_dir: Path | None = None,
    workers: int | None = None,
) -> dict:
    """`verify --roundtrip`: rebuild the records without writing them and compare their records sha256s with the
    recorded ones (a mismatch is a difference, not an error); with `full`, also with that bundle's own (its
    compressed sha256 must be the files' source_bundle_sha256). A set built by tool 1.x records none, so it needs
    `full`. Refusals (not a lossless build) raise."""
    from . import sources
    from .verify import dir_kv, spec_for_dir

    out: dict = {"passed": False, "problems": [], "checked": [], "records_sha256": None, "text_size": None}
    out["promise"] = RECORD_IDENTICAL
    con = duckdb.connect()
    try:
        kvs = dir_kv(con, Path(out_dir))
    finally:
        con.close()
    kvs = {n: kv for n, kv in kvs.items() if n != f"{ROOT_VALUES_TABLE}.parquet"}
    if built_by_1x(kvs) and full is None:
        tool = _shared_kv(kvs, "tool_version")
        raise RoundTripError(
            f"{out_dir}: built by tool {tool}, which records the sha256 of its text and no records sha256: give "
            "--full <the bundle it was built from> to check its round trip"
        )
    try:
        r = rebuild(
            out_dir,
            None,
            memory_limit=memory_limit,
            threads=threads,
            work_dir=work_dir,
            workers=workers,
        )
    except RoundTripMismatch as e:
        out["problems"].append(str(e))
        return out
    out.update(records_sha256=r.records_sha256, text_size=r.text_size, sections=r.sections)
    if r.checked:
        out["checked"] += ["every section's section_records_sha256", "the manifest's records_sha256"]
    if full is not None:
        recorded = _shared_kv(kvs, "source_bundle_sha256")
        # A set `apply` wrote (ADR 0011) has no source file: any bundle holding the same records matches it.
        applied = _shared_kv(kvs, "applied") is not None
        inp = sources.resolve_input(full, sha256=sha256)
        con = duckdb.connect()
        try:
            spec = spec_for_dir(con, Path(out_dir))
        finally:
            con.close()
        work = Path(tempfile.mkdtemp(prefix="gkm-bundle-parquet-roundtrip-", dir=work_dir))
        try:
            with Hasher(workers) as hasher:
                src = bundle_records(inp, spec, work, hasher, memory_limit=memory_limit, threads=threads)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if not applied and src.sha256 != recorded:
            raise RoundTripError(
                f"--full {full} has sha256 {src.sha256}, but the files were built from {recorded}"
            )
        if src.records_sha256 != r.records_sha256:
            names = set(src.sections) | set(r.section_records)
            differ = sorted(
                s for s in names if src.sections.get(s, EMPTY_SHA256) != r.section_records.get(s, EMPTY_SHA256)
            )
            if src.root_sha256 != r.root_sha256:
                differ.append("the root")
            out["problems"].append(
                f"the rebuilt records have records sha256 {r.records_sha256}, --full {inp.name}'s "
                f"{src.records_sha256} (they differ in {', '.join(differ)})"
            )
            return out
        out["checked"].append(
            f"the records of {inp.name}"
            + (" (an applied set: by its records, as no one file holds it)" if applied else "")
        )
    out["passed"] = True
    return out


# --- CLI -------------------------------------------------------------------------------------------------------


def run_rebuild_command(args) -> int:
    """`gkm-bundle-parquet rebuild`: write the bundle back; prints its records sha256."""
    result = rebuild(
        args.dir,
        args.out,
        memory_limit=args.memory_limit,
        threads=args.threads,
        work_dir=args.work_dir,
        log=lambda m: print(f"gkm-bundle-parquet: {m}", file=sys.stderr, flush=True),
    )
    total = sum(result.sections.values())
    if result.checked:
        check = (
            "matches the manifest's records_sha256 and every section's section_records_sha256: record-identical "
            "to the source"
        )
    else:
        check = (
            f"not checked: tool {result.tool_version} built the set and recorded no records sha256; check it with "
            "verify --roundtrip --full <the bundle>"
        )
    print(
        f"rebuilt {result.out}: bundle schema {result.schema_version or '(none stated)'}, "
        f"{len(result.sections)} section(s), {total} records, {result.text_size} bytes of text, records sha256 "
        f"{result.records_sha256} ({check})"
    )
    return 0
