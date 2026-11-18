"""Resolve one input bundle: a local path or URL, with the sha256 it must have (ADR 0009 decision 2).

The expected sha256 is `--sha256` when given, else the file's `<file>.sha256` sidecar when there is one (a
convention any producer can use), else none: the build then hashes the file first. Which release a file is, and
which file to read, is the producer's business; the tool reads the one bundle it is given. Reading bytes is
transport.py. Unit tests never touch the network.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from pathlib import Path

from .errors import InputError
from .model import InputFile
from .transport import fetch_bytes
from .transport import is_url as _is_url

_SHA = re.compile(r"[0-9a-fA-F]{64}")


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def parse_sidecar(text: str, name: str, *, where: str) -> str:
    """The sha256 from a `<file>.sha256` sidecar in `sha256sum` format (`<hex>  <name>` or `<hex> *<name>`;
    a bare `<hex>` is accepted too). A file name, when present, must be `name`."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    m = (
        re.fullmatch(r"([0-9a-fA-F]{64})(?:[ \t]+\*?(\S(?:.*\S)?))?", lines[0].strip())
        if len(lines) == 1
        else None
    )
    if not m:
        raise InputError(f"{where}: not a sha256sum line for one file")
    if m.group(2) and m.group(2) != name:
        raise InputError(f"{where}: names {m.group(2)!r}, not {name!r}")
    return m.group(1).lower()


def _sidecar_sha(source: str, name: str, is_url: bool) -> str | None:
    side = source + ".sha256"
    if is_url:
        data = fetch_bytes(side, allow_404=True)
        if data is None:
            return None
    else:
        if not os.path.isfile(side):
            return None
        data = Path(side).read_bytes()
    return parse_sidecar(data.decode("utf-8", "replace"), name, where=side)


def resolve_input(source: str, *, sha256: str | None = None) -> InputFile:
    """A path or URL -> InputFile, with the expected sha256 from `sha256` (explicit) or else the `<file>.sha256`
    sidecar, when there is one."""
    is_url = _is_url(source)
    url_path = urllib.parse.urlsplit(source).path if is_url else source
    name = _basename(urllib.parse.unquote(url_path) if is_url else url_path)
    size: int | None = None
    if not is_url:
        try:
            size = os.stat(source).st_size
        except OSError as e:
            raise InputError(f"{source}: {e.strerror or e}") from e

    expected, sha_src = None, None
    if sha256 is not None:
        if not _SHA.fullmatch(sha256):
            raise InputError(f"--sha256 {sha256!r} is not a 64-digit hex sha256")
        expected, sha_src = sha256.lower(), "explicit"
    else:
        side = _sidecar_sha(source, name, is_url)
        if side:
            expected, sha_src = side, "sidecar"
    return InputFile(source=source, name=name, expected_sha256=expected, sha256_source=sha_src, size=size)
