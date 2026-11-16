"""Data passed between the stages: inputs, streamed and staged bundles, build results.

Stage interfaces (each module owns its half):

- sources.py  resolves a path or URL into an InputFile and finds its expected sha256 (explicit, or the
              `<file>.sha256` sidecar); transport.py opens byte streams.
- bundle.py   streams one bundle once: hashes the compressed bytes, reads the version, classifies every line
              (design plan §4.2) and hands each record to a sink -> StreamedBundle.
- build.py    the section pipeline (design plan §6.2): streams the bundle once, stages only the current section
              as chunk files, and hands each closed section to a worker thread that loads it into DuckDB as
              `bundle_<section>(key VARCHAR, record VARCHAR)`, runs the SQL of every table that reads it
              (sqltemplate.py renders it from the spec), writes `<table>.parquet` with the spec's writer settings
              and key-value metadata, and drops it. Then the manifest.
- verify.py   checks a built directory against the spec and the bundle (recompute, on the same pipeline).

Staging format: gzip text, one record per line, `<key as JSON string>\\t<record>\\n`. `<record>` is the exact
bytes from the bundle line (the trailing `,` removed), so it equals the bundle record byte for byte. JSON
text never holds a raw tab or newline, so the split is unambiguous. The build writes a section as chunk files
(`<section>.<nnnn>.tsv.gz`) holding the section's records in bundle order; each of its lines starts with
`<ordinal>\\t`, the record's 0-based position in the section (the `ordinal` derivation kind). `stage_bundle` writes
one file per section, without the ordinal.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InputFile:
    """One input bundle as resolved before reading."""

    source: str  # local path or URL
    name: str  # the file name
    expected_sha256: str | None  # hex, or None when no checksum was given and there is no sidecar
    sha256_source: str | None  # "explicit" | "sidecar" | "selection" | None
    size: int | None = None


@dataclass(frozen=True)
class StreamedBundle:
    """What one pass over a bundle found."""

    input: InputFile
    sha256: str  # computed over the compressed bytes while streaming
    size: int  # compressed bytes read
    schema_version: str
    counts: dict[str, int]  # section -> records, for the sections the bundle has
    text_sha256: str | None = None  # of the uncompressed text, when asked for (stream_bundle text_digests)
    text_size: int | None = None  # uncompressed bytes, likewise
    layout: str = "line"  # the reader the file took: line | json (bundle.py)
    root: tuple[str, ...] = ()  # the root's members, in the order of the text


@dataclass(frozen=True)
class StagedSection:
    name: str
    path: Path  # staging file (format above)
    records: int
    max_line_bytes: int = 0  # longest staging line, to size DuckDB's read_csv max_line_size


@dataclass(frozen=True)
class StagedBundle:
    """A whole bundle staged to one file per section (bundle.stage_bundle)."""

    input: InputFile
    sha256: str  # computed over the compressed bytes while streaming
    size: int  # compressed bytes read
    schema_version: str
    sections: dict[str, StagedSection]


@dataclass(frozen=True)
class SectionChunks:
    """One section's final state, staged by the pipeline and handed to the worker."""

    section: str
    paths: tuple[Path, ...]  # chunk files (format above); empty when the final state has no records
    records: int
    max_line_bytes: int = 0


@dataclass(frozen=True)
class BuildInputs:
    """What a build reads: one bundle."""

    full: InputFile


@dataclass(frozen=True)
class TableResult:
    table: str
    file: Path
    rows: int
    sha256: str
    size: int


@dataclass(frozen=True)
class BuildResult:
    out_dir: Path
    tables: tuple[TableResult, ...]
    manifest_path: Path
