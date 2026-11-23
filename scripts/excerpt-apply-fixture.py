#!/usr/bin/env python3
"""Write a small, consistent `apply` fixture from two real line-layout bundles and a deletes file (ADR 0011).

From a base bundle, an upserts bundle and a deletes file (`{"<section>": ["<key>", ...]}`), it picks, per section
and in key order, up to N keys of each kind:

- kept: in the base, not deleted, not upserted;
- replaced: in both bundles;
- deleted: in the base and listed in the deletes file;
- added: in the upserts bundle only;

and writes the base excerpt (kept, replaced and deleted records, from the base), the upserts excerpt (replaced and
added records, from the upserts bundle) and the deletes excerpt (the deleted keys). Records are copied byte for
byte from their source lines; each excerpt keeps its source's root order and version line and leaves out sections
it has no record of. `--omit-from-base S` leaves section S out of the base excerpt (so the upserts bundle adds a
root member); `--take S=kept,replaced,deleted,added` sets the counts for one section. Gzip level 9, mtime 0, no
file name.

Usage:
    uv run python scripts/excerpt-apply-fixture.py BASE.json.gz UPSERTS.json.gz DELETES.json OUT_DIR \\
        [--per-kind N] [--omit-from-base S ...] [--take S=k,r,d,a ...]

It writes `base.json.gz`, `upserts.json.gz`, `deletes.json` and `selection.json` (what was picked) into OUT_DIR;
the fixture's README and PROVENANCE.json record the sources.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

KINDS = ("kept", "replaced", "deleted", "added")


def read_lines(path: Path) -> tuple[list[tuple[str, str]], dict[str, dict[str, str]]]:
    """(root members in order as (name, line text after the name), {section: {key: record text}}), from the line
    layout: one `"<key>": <record>` per line inside a section."""
    dec = json.JSONDecoder()
    root: list[tuple[str, str]] = []
    sections: dict[str, dict[str, str]] = {}
    section = None
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if line in ("{", "}"):
                continue
            if line.startswith("    "):
                text = line[4:]
                key, end = dec.raw_decode(text)
                record = text[end + 2 :].removesuffix(",")
                sections[section][key] = record
            elif line.startswith("  }"):
                section = None
            elif line.startswith('  "'):
                name, end = dec.raw_decode(line[2:])
                rest = line[2 + end + 2 :].removesuffix(",")
                root.append((name, rest))
                if rest in ("{", "{}"):
                    sections[name] = {}
                    if rest == "{":
                        section = name
            else:
                raise SystemExit(f"{path}: not the line layout: {line[:80]!r}")
    return root, sections


def write_bundle(path: Path, root: list[tuple[str, str]], records: dict[str, list[tuple[str, str]]]) -> None:
    parts = []
    for name, rest in root:
        if rest in ("{", "{}"):
            recs = records.get(name)
            if not recs:
                continue
            body = ",\n".join(f"    {json.dumps(k, ensure_ascii=False)}: {r}" for k, r in recs)
            parts.append(f"  {json.dumps(name, ensure_ascii=False)}: {{\n{body}\n  }}")
        else:
            parts.append(f"  {json.dumps(name, ensure_ascii=False)}: {rest}")
    text = ("{\n" + ",\n".join(parts) + "\n}\n").encode("utf-8")
    with open(path, "wb") as fh, gzip.GzipFile(filename="", mode="wb", fileobj=fh, compresslevel=9, mtime=0) as gz:
        gz.write(text)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("base", type=Path)
    p.add_argument("upserts", type=Path)
    p.add_argument("deletes", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--per-kind", type=int, default=2)
    p.add_argument("--omit-from-base", action="append", default=[])
    p.add_argument("--take", action="append", default=[])
    args = p.parse_args()

    take = {}
    for t in args.take:
        section, counts = t.split("=", 1)
        take[section] = dict(zip(KINDS, map(int, counts.split(",")), strict=True))
    base_root, base = read_lines(args.base)
    up_root, up = read_lines(args.upserts)
    deletes = json.loads(args.deletes.read_text(encoding="utf-8"))

    picked: dict[str, dict[str, list[str]]] = {}
    for section in dict.fromkeys([n for n, _ in base_root] + [n for n, _ in up_root]):
        if section not in base and section not in up:
            continue
        b, u = base.get(section, {}), up.get(section, {})
        d = set(deletes.get(section, ())) & set(b)
        pools = {
            "kept": sorted(set(b) - set(u) - d),
            "replaced": sorted(set(b) & set(u) - d),
            "deleted": sorted(d - set(u)),
            "added": sorted(set(u) - set(b)),
        }
        n = take.get(section, dict.fromkeys(KINDS, args.per_kind))
        if section in args.omit_from_base:
            n = {**n, "kept": 0, "replaced": 0, "deleted": 0}
        picked[section] = {k: pools[k][: n[k]] for k in KINDS}

    args.out.mkdir(parents=True, exist_ok=True)
    base_recs = {
        s: [(k, base[s][k]) for k in sorted(p["kept"] + p["replaced"] + p["deleted"])] for s, p in picked.items()
    }
    up_recs = {s: [(k, up[s][k]) for k in sorted(p["replaced"] + p["added"])] for s, p in picked.items()}
    write_bundle(args.out / "base.json.gz", base_root, base_recs)
    write_bundle(args.out / "upserts.json.gz", up_root, up_recs)
    dels = {s: p["deleted"] for s, p in picked.items() if p["deleted"]}
    (args.out / "deletes.json").write_text(json.dumps(dels, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    selection = {s: {k: v for k, v in p.items() if v} for s, p in picked.items() if any(p.values())}
    (args.out / "selection.json").write_text(json.dumps(selection, indent=1, ensure_ascii=False) + "\n", "utf-8")
    for name in ("base.json.gz", "upserts.json.gz", "deletes.json"):
        data = (args.out / name).read_bytes()
        print(f"{name}: {len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
