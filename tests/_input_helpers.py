"""Shared helpers for the input-side tests (sources, bundle).

Records always come from the suite's bundle fixture (tests/_fixture.py: the real 1.1.0 excerpt). Hand-made bytes
are used only for the grammar around them: section lines, commas and the header.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from _fixture import FIXTURE, PROVENANCE_PATH

from gkm_bundle_parquet.model import InputFile

PROVENANCE = PROVENANCE_PATH


def fixture_lines(path: Path = FIXTURE) -> list[bytes]:
    """The fixture's decompressed lines, without newlines."""
    return gzip.decompress(path.read_bytes()).split(b"\n")


def fixture_sections(path: Path = FIXTURE) -> dict[str, list[tuple[bytes, bytes]]]:
    """section -> [(key as JSON bytes, record bytes)], in file order, parsed independently of bundle.py: each
    record line is decoded with the stdlib JSON decoder to find where its key ends."""
    out: dict[str, list[tuple[bytes, bytes]]] = {}
    section = None
    dec = json.JSONDecoder()
    for line in fixture_lines(path):
        if line.startswith(b'  "') and line.endswith(b": {"):
            section = json.loads(line.strip()[:-3])
            out[section] = []
        elif line.startswith(b"    "):
            text = line[4:].decode()
            _, end = dec.raw_decode(text)  # the key
            key = text[:end].encode()
            record = line[4 + len(key) + 2 :]
            if record.endswith(b","):
                record = record[:-1]
            assert json.loads(record) is not None
            out[section].append((key, record))
    return out


def bundle_bytes(sections: dict[str, list[tuple[bytes, bytes]]], schema_version: str = "1.1.0") -> bytes:
    """Decompressed bundle text in the assembler's layout (assemble() in assemble-gkm-dicts.py @ 1.0)."""
    buf = bytearray(b'{\n  "schema_version": "' + schema_version.encode() + b'"')
    for name, recs in sections.items():
        if not recs:
            continue
        buf += b',\n  "' + name.encode() + b'": {\n'
        buf += b",\n".join(b"    " + k + b": " + r for k, r in recs)
        buf += b"\n  }"
    buf += b"\n}\n"
    return bytes(buf)


def write_gz(path: Path, text: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(text, compresslevel=3, mtime=0))
    return path


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local_input(path: Path, *, sha: str | None = None) -> InputFile:
    return InputFile(
        source=str(path),
        name=path.name,
        expected_sha256=sha,
        sha256_source="explicit" if sha else None,
        size=path.stat().st_size,
    )


# --- an independent records sha256 (docs/round-trip.md), written without the tool's code -----------------------


def _canon(v):
    """A value with every array sorted by its elements' canonical text. With ASCII names and no floats,
    json.dumps(sort_keys) of it is RFC 8785's text; it asserts both, so it can't silently differ."""
    if isinstance(v, dict):
        assert all(k.isascii() for k in v), sorted(v)
        return {k: _canon(x) for k, x in v.items()}
    if isinstance(v, list):
        return sorted((_canon(e) for e in v), key=_dumps)
    assert not isinstance(v, float), v
    return v


def _dumps(v) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def independent_section_sha256(records: dict) -> str:
    """The records sha256 of one section, {key: record} (sha256 of no text when it holds none)."""
    return _sha("".join(sorted(f"{_dumps(k)}\t{_sha(_dumps(_canon(r)))}\n" for k, r in records.items())))


def independent_records_sha256(doc: dict, sections: set[str]) -> str:
    """The records sha256 of a bundle read whole with json.load; `sections` names its sections."""
    section_shas, root = {}, {}
    for name, value in doc.items():
        if name in sections:
            root[name] = {}
            if value:
                section_shas[name] = independent_section_sha256(value)
        else:
            root[name] = value
    lines = [f"{_dumps(s)}\t{h}\n" for s, h in section_shas.items()]
    lines.append(f"$\t{_sha(_dumps(_canon(root)))}\n")
    return _sha("".join(sorted(lines)))
